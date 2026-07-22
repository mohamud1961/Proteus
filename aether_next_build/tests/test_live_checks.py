"""Tests for the live-check-state feature: probe checks after act turns."""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel, KernelHooks, KernelResult
from aether_next.ledger import ExecutionLedger
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CheckSpec,
    CompletionPolicy,
    CompiledRuntime,
    ContextPolicy,
    DeliverableSpec,
    EnvMap,
    EvalIndex,
    ObjectiveGraph,
    ProofObligation,
    ReconfigurePolicy,
    RefusalPolicy,
    RuntimeConfigIR,
    SolverTurn,
)


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_kernel.py patterns)
# ---------------------------------------------------------------------------

_CAPS = {
    "shell": CapabilityDescriptor(capability_id="shell", summary="Run commands"),
    "filesystem": CapabilityDescriptor(capability_id="filesystem", summary="Read/write files"),
}


def _make_envmap(
    *,
    task_prompt: str = "Do the task.",
    workspace_root: str = "/app",
    grader_hints: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, CapabilityDescriptor] | None = None,
) -> EnvMap:
    return EnvMap(
        task_prompt=task_prompt,
        workspace_root=workspace_root,
        capabilities=capabilities or dict(_CAPS),
        grader_hints=dict(grader_hints or {}),
    )


def _make_ir(
    *,
    selected_capabilities: tuple[str, ...] = ("shell", "filesystem"),
    completion_policy: CompletionPolicy | None = None,
    check_plan: tuple[str, ...] = (),
    forbidden_paths: tuple[str, ...] = (),
) -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="Test summary.",
        solver_identity_prompt="You are a solver.",
        selected_capabilities=selected_capabilities,
        completion_policy=completion_policy or CompletionPolicy(
            require_authoritative_check=False,
            require_all_obligations=False,
            require_recent_progress=False,
            require_clean_integrity=True,
        ),
        refusal_policy=RefusalPolicy(),
        reconfigure_policy=ReconfigurePolicy(),
        check_plan=check_plan,
        forbidden_paths=forbidden_paths,
        inspection_plan=("inspect workspace",),
        proof_plan=("verify output",),
    )


class FakeHooks:
    """Configurable KernelHooks implementation for tests."""

    def __init__(
        self,
        ir: RuntimeConfigIR,
        turns: list[SolverTurn],
    ) -> None:
        self._ir = ir
        self._turns = list(turns)
        self.solve_call_count = 0

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        return self._ir

    def solve(self, messages: list[dict[str, str]], compiled: CompiledRuntime) -> SolverTurn:
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


def _action(
    kind: str,
    arguments: Mapping[str, Any],
    *,
    action_id: str = "",
    capability_id: str = "shell",
) -> ActionRequest:
    aid = action_id or f"a-{kind}-1"
    return ActionRequest(
        action_id=aid,
        kind=kind,
        capability_id=capability_id,
        arguments=arguments,
        intent="test intent",
        expected_observation="test observation",
        if_fail_next="retry",
    )


def _act_turn(*actions: ActionRequest) -> SolverTurn:
    return SolverTurn(kind="act", summary="acting", actions=tuple(actions))


def _submit_turn() -> SolverTurn:
    return SolverTurn(kind="submit_outcome", summary="submitting")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestObligationFlipsSatisfiedAcrossSteps:
    """Run the kernel with a contract-style compiled runtime that has a
    planned existence check for out.txt and an artifact obligation.  The
    solver writes out.txt in step 0 then submits in step 1.  After step 0
    the probe should have run and the obligation should be satisfied BEFORE
    the submit."""

    def test_obligation_flips_satisfied_across_steps(self) -> None:
        executor = MemoryExecutor(workspace_root="/app")

        # Register a handler so `test -e out.txt` passes after the file is
        # created (MemoryExecutor checks its own files dict).
        def existence_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            if "out.txt" in ex.files:
                return CommandResult(command=cmd, exit_code=0, stdout="exists")
            return CommandResult(command=cmd, exit_code=1, stderr="not found")

        executor.register_command("test -e out.txt", existence_handler)

        # Envmap hints declare the deliverable and the existence check, so the
        # baseline analyzer generates the obligation and planned check.
        envmap = _make_envmap(
            grader_hints={
                "required_artifacts": ["out.txt"],
                "verify_commands": ["test -e out.txt"],
            },
        )
        from aether_next.compiler import CapabilityRegistry, ConfigCompiler
        _, eval_index = ConfigCompiler(
            CapabilityRegistry.from_envmap(envmap)
        ).analyze_envmap(envmap)
        check_ids = tuple(check.check_id for check in eval_index.checks)
        assert check_ids, "analyzer should compile the hinted existence check"

        # The solver writes out.txt in step 0, then submits in step 1.
        write_action = _action(
            "write_file",
            {"path": "out.txt", "content": "hello"},
            action_id="a-write-out",
            capability_id="filesystem",
        )
        ir = _make_ir(
            check_plan=check_ids,
            completion_policy=CompletionPolicy(
                require_authoritative_check=True,
                allow_evidence_fallback=False,
                require_all_obligations=True,
                require_recent_progress=False,
                require_clean_integrity=True,
            ),
        )
        hooks = FakeHooks(ir, [_act_turn(write_action), _submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        # 1. A probe check_result receipt should exist from step 0.
        probe_receipts = [
            r for r in result.receipts
            if r.kind == "check_result" and ":probe:" in r.receipt_id
        ]
        assert probe_receipts, (
            f"Expected a probe check_result receipt but found none. "
            f"Receipt IDs: {[r.receipt_id for r in result.receipts]}"
        )

        # The probe should be from step 0 (the act turn that wrote the file).
        assert any(r.step == 0 for r in probe_receipts), (
            f"Expected probe receipt at step 0, got steps: {[r.step for r in probe_receipts]}"
        )

        # 2. The probe should have passed (file was created before probe ran).
        step0_probes = [r for r in probe_receipts if r.step == 0]
        assert all(r.success for r in step0_probes), (
            f"Expected step 0 probe to pass: {[(r.receipt_id, r.success) for r in step0_probes]}"
        )

        # 3. The overall run should complete because the check passed and
        #    the obligation was satisfied by the probe.
        assert result.status == "completed", (
            f"Expected completed, got {result.status}. "
            f"Blockers: {result.blockers}"
        )


class TestNoProbeWhenNoPlannedChecks:
    """A baseline compiled runtime (empty check_plan) should produce no
    probe check_result receipts from act turns.  Only submit may produce
    check receipts (if any)."""

    def test_no_probe_when_no_planned_checks(self) -> None:
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        write_action = _action(
            "write_file",
            {"path": "output.txt", "content": "data"},
            action_id="a-write-1",
            capability_id="filesystem",
        )

        # No check_plan, no contract architect -> baseline path.
        ir = _make_ir(
            completion_policy=CompletionPolicy(
                require_authoritative_check=False,
                require_all_obligations=False,
                require_recent_progress=False,
                require_clean_integrity=True,
            ),
        )
        hooks = FakeHooks(ir, [_act_turn(write_action), _submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        # No probe receipts should exist.
        probe_receipts = [
            r for r in result.receipts
            if r.kind == "check_result" and ":probe:" in r.receipt_id
        ]
        assert not probe_receipts, (
            f"Expected no probe receipts in baseline run, found: "
            f"{[r.receipt_id for r in probe_receipts]}"
        )

        # The run should still complete normally.
        assert result.status == "completed"
