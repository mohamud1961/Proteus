"""Tests for Feature 1 (WorkflowPolicy), Feature 2 (model routing), and
Feature 3 (HarnessLimiterClassifier)."""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from aether_next.classifier import HarnessLimiterClassifier, LimiterClassification
from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel, KernelResult
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.runtime_ir import (
    ACTION_SCHEMA,
    MODEL_TIERS,
    WORKFLOW_MODES,
    ActionRequest,
    CapabilityDescriptor,
    CompletionPolicy,
    CompiledRuntime,
    EnvMap,
    ReconfigurePolicy,
    RefusalPolicy,
    RuntimeConfigIR,
    SolverTurn,
    WorkflowPolicy,
)


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_kernel.py patterns)
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
    refusal_policy: RefusalPolicy | None = None,
    reconfigure_policy: ReconfigurePolicy | None = None,
    workflow_policy: WorkflowPolicy | None = None,
    architect_model_tier: str = "strong",
    solver_model_tier: str = "mini",
    verifier_model_tier: str = "mini",
    perception_model_tier: str = "vision",
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
        refusal_policy=refusal_policy or RefusalPolicy(),
        reconfigure_policy=reconfigure_policy or ReconfigurePolicy(),
        workflow_policy=workflow_policy or WorkflowPolicy(),
        architect_model_tier=architect_model_tier,
        solver_model_tier=solver_model_tier,
        verifier_model_tier=verifier_model_tier,
        perception_model_tier=perception_model_tier,
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
        reconfigure_ir: RuntimeConfigIR | None = None,
    ) -> None:
        self._ir = ir
        self._turns = list(turns)
        self._reconfigure_ir = reconfigure_ir or ir
        self.architect_called = False
        self.solve_call_count = 0
        self.reconfigure_call_count = 0

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        self.architect_called = True
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
        self.reconfigure_call_count += 1
        return self._reconfigure_ir


def _submit_turn() -> SolverTurn:
    return SolverTurn(kind="submit_outcome", summary="submitting")


def _act_turn(*actions: ActionRequest) -> SolverTurn:
    return SolverTurn(kind="act", summary="acting", actions=tuple(actions))


def _action(
    kind: str,
    arguments: Mapping[str, Any],
    *,
    action_id: str = "",
    capability_id: str = "shell",
    candidate_id: str = "",
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
        candidate_id=candidate_id,
    )


# ---------------------------------------------------------------------------
# Feature 1: WorkflowPolicy
# ---------------------------------------------------------------------------


class TestWorkflowPolicyValidation:
    def test_bad_workflow_mode_is_config_invalid(self) -> None:
        """An unknown workflow mode fails closed instead of using a safe default."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")
        ir = _make_ir(workflow_policy=WorkflowPolicy(mode="nonexistent_mode"))
        hooks = FakeHooks(ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)
        assert result.status == "config_invalid"
        assert "unknown_workflow_mode" in result.blockers
        assert result.receipts == ()

    def test_valid_workflow_modes_accepted(self) -> None:
        """All defined workflow modes should pass validation."""
        envmap = _make_envmap()
        for mode in WORKFLOW_MODES:
            ir = _make_ir(workflow_policy=WorkflowPolicy(mode=mode))
            compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
            issues = compiler.validate(ir, envmap)
            workflow_issues = [i for i in issues if i.code == "unknown_workflow_mode"]
            assert not workflow_issues, f"Mode '{mode}' should be valid"


class TestWorkflowPolicyCarriedOntoCompiledRuntime:
    def test_workflow_policy_carried(self) -> None:
        """WorkflowPolicy from IR should appear on the CompiledRuntime."""
        envmap = _make_envmap()
        policy = WorkflowPolicy(mode="explore_first", max_explore_steps=7)
        ir = _make_ir(workflow_policy=policy)
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        compiled = compiler.compile(ir, envmap)
        assert compiled.workflow_policy.mode == "explore_first"
        assert compiled.workflow_policy.max_explore_steps == 7

    def test_workflow_mode_in_prefix_sections(self) -> None:
        """The workflow_mode should appear as a stable prefix section."""
        envmap = _make_envmap()
        ir = _make_ir(workflow_policy=WorkflowPolicy(mode="debug_repair"))
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        compiled = compiler.compile(ir, envmap)
        section_names = [name for name, _ in compiled.stable_prefix_sections]
        assert "workflow_mode" in section_names
        mode_section = next(
            body for name, body in compiled.stable_prefix_sections
            if name == "workflow_mode"
        )
        assert mode_section == "debug_repair"

    def test_service_stabilize_adds_service_liveness_monitor(self) -> None:
        """service_stabilize mode should ensure service_liveness monitor is included."""
        envmap = _make_envmap()
        ir = _make_ir(workflow_policy=WorkflowPolicy(mode="service_stabilize"))
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        compiled = compiler.compile(ir, envmap)
        assert "service_liveness" in compiled.enforced_monitors


# ---------------------------------------------------------------------------
# Feature 2: Model routing fields
# ---------------------------------------------------------------------------


class TestModelTierValidation:
    def test_bad_model_tier_is_config_invalid(self) -> None:
        """An unknown model tier fails closed instead of using a safe default."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")
        ir = _make_ir(solver_model_tier="nonexistent_tier")
        hooks = FakeHooks(ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)
        assert result.status == "config_invalid"
        assert "unknown_model_tier" in result.blockers
        assert result.receipts == ()

    def test_all_four_tiers_validated(self) -> None:
        """Each of the four tier fields should be independently validated."""
        envmap = _make_envmap()
        for field_name, kwarg in (
            ("architect_model_tier", {"architect_model_tier": "bad"}),
            ("solver_model_tier", {"solver_model_tier": "bad"}),
            ("verifier_model_tier", {"verifier_model_tier": "bad"}),
            ("perception_model_tier", {"perception_model_tier": "bad"}),
        ):
            ir = _make_ir(**kwarg)
            compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
            issues = compiler.validate(ir, envmap)
            tier_issues = [i for i in issues if i.code == "unknown_model_tier"]
            assert tier_issues, f"{field_name} should trigger unknown_model_tier"


class TestModelTiersCarried:
    def test_tiers_carried_onto_compiled_runtime(self) -> None:
        """All four model tier fields from IR should appear on CompiledRuntime."""
        envmap = _make_envmap()
        ir = _make_ir(
            architect_model_tier="codex",
            solver_model_tier="strong",
            verifier_model_tier="vision",
            perception_model_tier="default",
        )
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        compiled = compiler.compile(ir, envmap)
        assert compiled.architect_model_tier == "codex"
        assert compiled.solver_model_tier == "strong"
        assert compiled.verifier_model_tier == "vision"
        assert compiled.perception_model_tier == "default"


# ---------------------------------------------------------------------------
# Feature 3: HarnessLimiterClassifier
# ---------------------------------------------------------------------------


class TestClassifierSafetyBlock:
    def test_safety_block_classifies_as_safety_policy_failure(self) -> None:
        result = KernelResult(
            status="incomplete",
            step=1,
            reconfigurations=0,
            receipts=(Receipt(
                receipt_id="safety:1",
                step=0,
                kind="safety_block",
                success=False,
                summary="structured external target blocked by enforced scope",
                failure_class="safety_violation",
            ),),
        )
        classification = HarnessLimiterClassifier().classify(result)
        assert classification.label == "safety_policy_failure"
        assert classification.confidence == "high"


class TestClassifierCompleted:
    def test_completed_run_classifies_as_none(self) -> None:
        """A completed run should classify as 'none' (no failure)."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")
        ir = _make_ir()
        hooks = FakeHooks(ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "completed"
        classifier = HarnessLimiterClassifier()
        classification = classifier.classify(result)
        assert classification.label == "none"
        assert classification.detail == "completed"


class TestClassifierModelLimit:
    def test_incomplete_with_progress_classifies_as_model_limit(self) -> None:
        """An incomplete run with genuine diverse real actions (run_command +
        write_file), at least one state change, and no harness blocks should
        classify as model_limit under the stricter evidence rule."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        def echo_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=0, stdout="ok")

        executor.register_command("echo step1", echo_handler)
        executor.register_command("echo step2", echo_handler)

        ir = _make_ir()
        # Script diverse state-changing actions (run_command + write_file) so
        # the classifier sees >=2 distinct real action kinds with progress.
        turns = [
            _act_turn(
                _action("run_command", {"command": "echo step1"}, action_id="a-step-1"),
            ),
            _act_turn(
                _action("write_file", {"path": "out.txt", "content": "data"},
                        action_id="a-write-1", capability_id="filesystem"),
            ),
            # More turns than max_steps to force incomplete.
            _act_turn(
                _action("run_command", {"command": "echo step1"}, action_id="a-step-3"),
            ),
        ]
        hooks = FakeHooks(ir, turns)
        kernel = AetherNextKernel(max_steps=2)
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "incomplete"
        # Verify receipts have >=2 real action kinds (run_command, write_file)
        real_kinds = {
            r.kind for r in result.receipts
            if r.kind in HarnessLimiterClassifier._REAL_ACTION_KINDS
        }
        assert len(real_kinds) >= 2, f"Expected >=2 real action kinds, got {real_kinds}"
        assert any(r.state_change for r in result.receipts), "Need state changes"
        classifier = HarnessLimiterClassifier()
        classification = classifier.classify(result)
        assert classification.label == "model_limit"

    def test_zero_receipts_incomplete_is_harness_context_failure(self) -> None:
        """A run with ZERO receipts and status incomplete must classify as
        harness_context_failure, NOT model_limit -- there is no evidence
        the harness gave the model a working runtime."""
        result = KernelResult(
            status="incomplete",
            step=0,
            reconfigurations=0,
            receipts=(),
        )
        classifier = HarnessLimiterClassifier()
        classification = classifier.classify(result)
        assert classification.label == "harness_context_failure"
        assert classification.confidence == "low"
        assert "insufficient evidence" in classification.detail


class TestClassifierConfigInvalid:
    def test_all_unknown_caps_repairs_when_registry_has_caps(self) -> None:
        """All-unknown architect capabilities can still be genuinely repaired.

        This keeps real generic repair, but no longer permits a fake generic
        safe-default fallback if repair fails.
        """
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")
        bad_ir = _make_ir(selected_capabilities=("nonexistent_cap",))
        hooks = FakeHooks(bad_ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        assert result.status != "config_invalid"
        recovery_receipts = [
            r for r in result.receipts
            if r.kind == "config_repair"
        ]
        assert recovery_receipts, "Expected a genuine config_repair receipt"

    def test_genuine_substrate_failure_causes_config_invalid(self) -> None:
        """When the registry has NO available capabilities, even the guaranteed
        default fails -> genuine config_invalid -> harness_runtime_failure."""
        empty_caps = {
            "broken": CapabilityDescriptor(
                capability_id="broken", summary="Broken", available=False,
            ),
        }
        envmap = _make_envmap(capabilities=empty_caps)
        executor = MemoryExecutor(workspace_root="/app")
        bad_ir = _make_ir(selected_capabilities=("nonexistent_cap",))
        hooks = FakeHooks(bad_ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "config_invalid"
        classifier = HarnessLimiterClassifier()
        classification = classifier.classify(result)
        assert classification.label == "harness_runtime_failure"
        assert classification.confidence == "high"

    def test_partial_unknown_caps_is_recoverable(self) -> None:
        """A mix of valid + unknown capabilities should NOT cause config_invalid.

        Unknown capabilities are dropped (non-fatal warning); the run proceeds
        with the valid ones.
        """
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")
        ir = _make_ir(selected_capabilities=("shell", "filesystem", "nonexistent_cap"))
        hooks = FakeHooks(ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "completed", (
            f"expected completed (unknown cap dropped), got {result.status}"
        )


class TestKernelResultCarriesReceipts:
    def test_completed_result_has_receipts(self) -> None:
        """KernelResult from a completed run should carry receipts."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")
        ir = _make_ir()
        hooks = FakeHooks(ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)
        assert result.status == "completed"
        assert isinstance(result.receipts, tuple)

    def test_incomplete_result_has_receipts(self) -> None:
        """KernelResult from an incomplete run should carry receipts from the ledger."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        def echo_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=0, stdout="ok")

        executor.register_command("echo noop", echo_handler)

        ir = _make_ir()
        turns = [
            SolverTurn(
                kind="act",
                summary=f"noop {i}",
                actions=(
                    ActionRequest(
                        action_id=f"a-noop-{i}",
                        kind="run_command",
                        capability_id="shell",
                        arguments={"command": "echo noop"},
                        intent="busywork",
                        expected_observation="ok",
                        if_fail_next="retry",
                    ),
                ),
            )
            for i in range(5)
        ]
        hooks = FakeHooks(ir, turns)
        kernel = AetherNextKernel(max_steps=3)
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "incomplete"
        assert len(result.receipts) >= 3, (
            f"Expected at least 3 receipts from 3 steps, got {len(result.receipts)}"
        )
