"""Behaviour tests for AetherNextKernel."""
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
# Shared helpers
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
        # Fallback: submit so the kernel doesn't loop forever.
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
# Tests
# ---------------------------------------------------------------------------


class TestRunNeverReturnsNone:
    def test_run_never_returns_none(self) -> None:
        """kernel.run returns a KernelResult, not None, for a trivial submit."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")
        ir = _make_ir()
        hooks = FakeHooks(ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)
        assert result is not None
        assert isinstance(result, KernelResult)


class TestSolverIsInvoked:
    def test_solver_is_invoked(self) -> None:
        """Assert the fake solve hook was actually called."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")
        ir = _make_ir()
        hooks = FakeHooks(ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        kernel.run(envmap, executor, hooks)
        assert hooks.solve_call_count >= 1, "solve hook was never called"


class TestActTurnDispatchesReadWriteRun:
    def test_act_turn_dispatches_read_write_run(self) -> None:
        """Independent state-changing frontiers execute only after observation."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app", files={"src/main.py": "print('hi')"})

        def echo_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=0, stdout="ok")

        executor.register_command("echo hello", echo_handler)

        actions = [
            _action("read_file", {"path": "src/main.py"}, action_id="a-read-1", capability_id="filesystem"),
            _action("write_file", {"path": "output.txt", "content": "result"}, action_id="a-write-1", capability_id="filesystem"),
            _action("run_command", {"command": "echo hello"}, action_id="a-run-1"),
        ]
        ir = _make_ir()
        hooks = FakeHooks(ir, [
            _act_turn(actions[0]),
            _act_turn(actions[1]),
            _act_turn(actions[2]),
            _submit_turn(),
        ])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        # Verify via executor state: write happened.
        assert "output.txt" in executor.files
        assert executor.files["output.txt"] == "result"

        # Verify command was run.
        assert "echo hello" in executor.command_history


class TestSuccessfulAuthoritativeCheck:
    def test_successful_authoritative_check_returns_completed(self) -> None:
        """A visible check that passes -> status='completed' and check_id in used_check_ids."""
        envmap = _make_envmap(
            grader_hints={"verify_commands": ["python test.py"]},
        )

        # Build eval_index to get the actual check_id.
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        _, eval_index = compiler.analyze_envmap(envmap)
        assert eval_index.checks, "should have at least one check from verify_commands"
        check = eval_index.checks[0]

        def pass_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=0, stdout="PASS")

        executor = MemoryExecutor(workspace_root="/app")
        executor.register_command(check.command, pass_handler)

        ir = _make_ir(
            completion_policy=CompletionPolicy(
                require_authoritative_check=True,
                allow_evidence_fallback=False,
                require_all_obligations=False,
                require_recent_progress=False,
                require_clean_integrity=True,
            ),
            check_plan=(check.check_id,),
        )
        hooks = FakeHooks(ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "completed"
        assert check.check_id in result.used_check_ids


class TestFailedAuthoritativeCheckBlocksCompletion:
    def test_failed_authoritative_check_blocks_completion(self) -> None:
        """A planned check that fails -> NOT completed."""
        envmap = _make_envmap(
            grader_hints={"verify_commands": ["python test.py"]},
        )
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        _, eval_index = compiler.analyze_envmap(envmap)
        check = eval_index.checks[0]

        def fail_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=1, stderr="FAIL")

        executor = MemoryExecutor(workspace_root="/app")
        executor.register_command(check.command, fail_handler)

        ir = _make_ir(
            completion_policy=CompletionPolicy(
                require_authoritative_check=True,
                allow_evidence_fallback=False,
                require_all_obligations=False,
                require_recent_progress=False,
                require_clean_integrity=True,
            ),
            check_plan=(check.check_id,),
            # Disable reconfigurations so it doesn't loop trying to fix.
        )
        hooks = FakeHooks(
            ir,
            [_submit_turn()],
        )
        kernel = AetherNextKernel(max_steps=3)
        result = kernel.run(envmap, executor, hooks)

        assert result.status != "completed", f"Expected not completed but got {result.status}"


class TestMissingRequiredArtifactBlocksCompletion:
    def test_missing_required_artifact_blocks_completion(self) -> None:
        """Objective requires an artifact that was never written -> submit does not complete."""
        envmap = _make_envmap(
            grader_hints={"required_artifacts": ["result.json"]},
        )
        executor = MemoryExecutor(workspace_root="/app")
        ir = _make_ir(
            completion_policy=CompletionPolicy(
                require_authoritative_check=False,
                allow_evidence_fallback=True,
                require_all_obligations=True,
                require_recent_progress=False,
                require_clean_integrity=True,
            ),
        )
        hooks = FakeHooks(ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=3)
        result = kernel.run(envmap, executor, hooks)

        assert result.status != "completed", (
            f"Expected not completed (missing artifact) but got {result.status}"
        )


class TestSafetyGuardBlocksExternalTarget:
    def test_safety_guard_blocks_external_target(self) -> None:
        """An action blocked by LocalOnlySafetyGuard is recorded as safety block, not dispatched."""
        envmap = _make_envmap()
        envmap = EnvMap(
            **{
                **envmap.__dict__,
                "network_scope": "loopback_only",
                "capabilities": {
                    **dict(envmap.capabilities),
                    "service_probe": CapabilityDescriptor(
                        "service_probe", "probe services", tool_names=("probe_service",),
                    ),
                },
            }
        )
        executor = MemoryExecutor(workspace_root="/app")

        # A structured external target is rejected before dispatch in isolated scope.
        ir = _make_ir(
            refusal_policy=RefusalPolicy(
                allowed_local_categories=("code_generation",),
                forbid_external_targets=True,
            ),
        )
        external_action = _action(
            "probe_service",
            {"target": "evil.example.com:443"},
            action_id="a-external-1",
            capability_id="service_probe",
        )
        hooks = FakeHooks(ir, [_act_turn(external_action), _submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        # The command should NOT have been dispatched.
        assert executor.command_history == []


class TestIntegrityGuardBlocksProtectedPathWrite:
    def test_integrity_guard_blocks_protected_path_write(self) -> None:
        """A write_file to a protected path is recorded as integrity block, not written."""
        envmap = _make_envmap(
            grader_hints={"immutable_paths": ["config/secret.yaml"]},
        )
        executor = MemoryExecutor(workspace_root="/app")
        ir = _make_ir()
        write_action = _action(
            "write_file",
            {"path": "config/secret.yaml", "content": "hacked"},
            action_id="a-write-protected",
            capability_id="filesystem",
        )
        hooks = FakeHooks(ir, [_act_turn(write_action), _submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        kernel.run(envmap, executor, hooks)

        # The file should NOT have been written.
        assert "config/secret.yaml" not in executor.files


class TestMaxStepsExhaustionReturnsIncomplete:
    def test_max_steps_exhaustion_returns_incomplete(self) -> None:
        """Solver keeps returning non-completing act turns -> after max_steps, status='incomplete'."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        def noop_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=0, stdout="ok")

        executor.register_command("echo noop", noop_handler)

        ir = _make_ir()
        # Generate more act turns than max_steps.
        noop_action = _action("run_command", {"command": "echo noop"}, action_id="a-noop")
        # Each turn needs a unique action_id to avoid receipt deduplication.
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
            for i in range(10)
        ]
        hooks = FakeHooks(ir, turns)
        kernel = AetherNextKernel(max_steps=3)
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "incomplete"
        assert result.step == 3


class TestConfigInvalidReturnsStatusNotNone:
    def test_config_invalid_returns_result_not_none(self) -> None:
        """Unrepaired fatal config returns config_invalid, never a fake default."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        bad_ir = RuntimeConfigIR(
            architect_summary="bad config",
            solver_identity_prompt="solver",
            selected_capabilities=("shell", "filesystem"),
            workflow_policy=WorkflowPolicy(mode="nonexistent_mode"),
        )
        hooks = FakeHooks(bad_ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        assert result is not None
        assert result.status == "config_invalid"
        assert "unknown_workflow_mode" in result.blockers
        assert result.receipts == ()


class TestInvalidArchitectIrRepair:
    def test_invalid_architect_ir_repairs_or_fails_closed(self) -> None:
        """A repairable architect IR can be repaired, but never fake-defaulted."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        bad_ir = _make_ir(selected_capabilities=("does_not_exist",))
        hooks = FakeHooks(bad_ir, [_submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        result = kernel.run(envmap, executor, hooks)

        assert result.status != "config_invalid", (
            f"Expected genuine repair, got config_invalid with "
            f"blockers={result.blockers}"
        )
        assert hooks.solve_call_count >= 1, "solve hook was never called"
        recovery_receipts = [
            r for r in result.receipts
            if r.kind == "config_repair"
        ]
        assert recovery_receipts, f"Expected a config_repair receipt; got {[r.kind for r in result.receipts]}"
        assert not [r for r in result.receipts if r.kind == "config_fallback"]


# ---------------------------------------------------------------------------
# Contract-architect integration
# ---------------------------------------------------------------------------
