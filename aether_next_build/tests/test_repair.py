"""Tests for aether_next.repair — deterministic config repair."""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel, KernelResult
from aether_next.ledger import ExecutionLedger
from aether_next.repair import repair_config
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    CompletionPolicy,
    CompiledRuntime,
    EnvMap,
    ProcessPolicy,
    ReconfigurePolicy,
    RefusalPolicy,
    RuntimeConfigIR,
    SolverTurn,
)


# ---------------------------------------------------------------------------
# Helpers (adapted from test_kernel.py)
# ---------------------------------------------------------------------------

_CAPS_WITH_PROBE = {
    "shell": CapabilityDescriptor(capability_id="shell", summary="Run commands"),
    "filesystem": CapabilityDescriptor(capability_id="filesystem", summary="Read/write files"),
    "service_probe": CapabilityDescriptor(capability_id="service_probe", summary="Probe services"),
}

_CAPS_NO_PROBE = {
    "shell": CapabilityDescriptor(capability_id="shell", summary="Run commands"),
    "filesystem": CapabilityDescriptor(capability_id="filesystem", summary="Read/write files"),
}


def _make_envmap(
    *,
    capabilities: dict[str, CapabilityDescriptor] | None = None,
    task_prompt: str = "Do the task.",
    workspace_root: str = "/app",
) -> EnvMap:
    return EnvMap(
        task_prompt=task_prompt,
        workspace_root=workspace_root,
        capabilities=capabilities or dict(_CAPS_WITH_PROBE),
    )


def _make_ir(
    *,
    selected_capabilities: tuple[str, ...] = ("shell", "filesystem"),
    process_policy: ProcessPolicy | None = None,
    completion_policy: CompletionPolicy | None = None,
    refusal_policy: RefusalPolicy | None = None,
) -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="Test summary.",
        solver_identity_prompt="You are a solver.",
        selected_capabilities=selected_capabilities,
        process_policy=process_policy or ProcessPolicy(),
        completion_policy=completion_policy or CompletionPolicy(
            require_authoritative_check=False,
            require_all_obligations=False,
            require_recent_progress=False,
            require_clean_integrity=True,
        ),
        refusal_policy=refusal_policy or RefusalPolicy(),
        inspection_plan=("inspect workspace",),
        proof_plan=("verify output",),
    )


# ---------------------------------------------------------------------------
# Unit tests for repair_config
# ---------------------------------------------------------------------------


class TestMissingServiceProbeRepaired:
    def test_repair_adds_service_probe_when_available(self) -> None:
        """When service_probe is available in the registry, repair adds it
        to selected_capabilities and the result validates clean."""
        envmap = _make_envmap(capabilities=dict(_CAPS_WITH_PROBE))
        ir = _make_ir(
            selected_capabilities=("shell", "filesystem"),
            process_policy=ProcessPolicy(require_fresh_probe=True),
        )
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))

        # Confirm the IR is fatally invalid before repair.
        pre_issues = compiler.validate(ir, envmap)
        pre_fatal = [i for i in pre_issues if i.fatal]
        assert any(i.code == "missing_service_probe" for i in pre_fatal)

        repaired_ir, codes = repair_config(ir, compiler, envmap)

        assert "added:service_probe" in codes
        assert "service_probe" in repaired_ir.selected_capabilities

        # Must validate clean (no fatal issues).
        post_issues = compiler.validate(repaired_ir, envmap)
        post_fatal = [i for i in post_issues if i.fatal]
        assert not post_fatal, f"Repaired IR still has fatal issues: {post_fatal}"


class TestUnrepairableWhenCapUnavailable:
    def test_clears_require_fresh_probe_when_cap_unavailable(self) -> None:
        """When service_probe is NOT available, repair clears
        require_fresh_probe instead and still validates clean."""
        envmap = _make_envmap(capabilities=dict(_CAPS_NO_PROBE))
        ir = _make_ir(
            selected_capabilities=("shell", "filesystem"),
            process_policy=ProcessPolicy(require_fresh_probe=True),
        )
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))

        repaired_ir, codes = repair_config(ir, compiler, envmap)

        assert "cleared:require_fresh_probe" in codes
        assert not repaired_ir.process_policy.require_fresh_probe

        post_issues = compiler.validate(repaired_ir, envmap)
        post_fatal = [i for i in post_issues if i.fatal]
        assert not post_fatal, f"Repaired IR still has fatal issues: {post_fatal}"


class TestRepairIsNoopOnValidConfig:
    def test_noop_on_valid_config(self) -> None:
        """A clean IR returns unchanged with empty codes."""
        envmap = _make_envmap()
        ir = _make_ir()
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))

        # Confirm already valid.
        pre_issues = compiler.validate(ir, envmap)
        pre_fatal = [i for i in pre_issues if i.fatal]
        assert not pre_fatal

        repaired_ir, codes = repair_config(ir, compiler, envmap)

        assert codes == ()
        assert repaired_ir == ir


# ---------------------------------------------------------------------------
# Kernel integration: repair path produces config_repair receipt
# ---------------------------------------------------------------------------


class FakeHooks:
    """Configurable KernelHooks for tests."""

    def __init__(
        self,
        ir: RuntimeConfigIR,
        turns: list[SolverTurn],
    ) -> None:
        self._ir = ir
        self._turns = list(turns)
        self.architect_called = False
        self.solve_call_count = 0

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        self.architect_called = True
        return self._ir

    def solve(
        self,
        messages: list[dict[str, str]],
        compiled: CompiledRuntime,
    ) -> SolverTurn:
        self.solve_call_count += 1
        if self._turns:
            return self._turns.pop(0)
        return SolverTurn(kind="submit_outcome", summary="fallback submit")

    def reconfigure(
        self,
        request: Mapping[str, Any],
        compiled: CompiledRuntime,
        ledger: ExecutionLedger,
    ) -> RuntimeConfigIR:
        return self._ir


class TestKernelRepairPath:
    def test_repairable_ir_gets_config_repair_receipt(self) -> None:
        """An architect IR that is fatally invalid but repairable results in
        a config_repair receipt (not config_fallback) and does NOT abort."""
        envmap = _make_envmap(capabilities=dict(_CAPS_WITH_PROBE))

        # IR has require_fresh_probe=True but service_probe is NOT selected.
        # service_probe IS available in the registry, so repair can add it.
        bad_ir = _make_ir(
            selected_capabilities=("shell", "filesystem"),
            process_policy=ProcessPolicy(require_fresh_probe=True),
        )

        submit_turn = SolverTurn(kind="submit_outcome", summary="submitting")
        hooks = FakeHooks(bad_ir, [submit_turn])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, MemoryExecutor(workspace_root="/app"), hooks)

        # Run was NOT aborted.
        assert result.status != "config_invalid"

        # A config_repair receipt exists.
        repair_receipts = [r for r in result.receipts if r.kind == "config_repair"]
        assert repair_receipts, (
            f"Expected a config_repair receipt. Got kinds: "
            f"{[r.kind for r in result.receipts]}"
        )

        # No config_fallback receipt — repair succeeded, no fallback needed.
        fallback_receipts = [r for r in result.receipts if r.kind == "config_fallback"]
        assert not fallback_receipts, (
            "config_fallback receipt should NOT exist when repair succeeded."
        )

        # Solver was invoked (proves the run continued past config phase).
        assert hooks.solve_call_count >= 1
