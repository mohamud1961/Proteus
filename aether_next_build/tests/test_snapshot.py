"""Tests for the snapshot callback infrastructure in AetherNextKernel."""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel, KernelResult
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
) -> EnvMap:
    return EnvMap(
        task_prompt=task_prompt,
        workspace_root=workspace_root,
        capabilities=dict(_CAPS),
    )


def _make_ir() -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="Test summary.",
        solver_identity_prompt="You are a solver.",
        selected_capabilities=("shell", "filesystem"),
        completion_policy=CompletionPolicy(
            require_authoritative_check=False,
            require_all_obligations=False,
            require_recent_progress=False,
            require_clean_integrity=True,
        ),
        refusal_policy=RefusalPolicy(),
        reconfigure_policy=ReconfigurePolicy(),
        check_plan=(),
        forbidden_paths=(),
        inspection_plan=("inspect workspace",),
        proof_plan=("verify output",),
    )


class _FakeHooks:
    def __init__(
        self,
        ir: RuntimeConfigIR,
        turns: list[SolverTurn],
    ) -> None:
        self._ir = ir
        self._turns = list(turns)

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        return self._ir

    def solve(self, messages: list[dict[str, str]], compiled: CompiledRuntime) -> SolverTurn:
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


def _act_turn(action_id: str, command: str) -> SolverTurn:
    return SolverTurn(
        kind="act",
        summary=f"run {command}",
        actions=(
            ActionRequest(
                action_id=action_id,
                kind="run_command",
                capability_id="shell",
                arguments={"command": command},
                intent="test",
                expected_observation="ok",
                if_fail_next="retry",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSnapshotCallbackInvoked:
    def test_snapshot_callback_invoked(self) -> None:
        """Kernel with snapshot_callback and snapshot_steps={1} calls the
        callback with step=1 during a 3-step run."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        def noop_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=0, stdout="ok")

        executor.register_command("echo step0", noop_handler)
        executor.register_command("echo step1", noop_handler)
        executor.register_command("echo step2", noop_handler)

        ir = _make_ir()
        turns = [
            _act_turn("a-0", "echo step0"),
            _act_turn("a-1", "echo step1"),
            _act_turn("a-2", "echo step2"),
            SolverTurn(kind="submit_outcome", summary="done"),
        ]
        hooks = _FakeHooks(ir, turns)

        captured_steps: list[int] = []

        def snapshot_cb(step: int) -> None:
            captured_steps.append(step)

        kernel = AetherNextKernel(
            max_steps=10,
            snapshot_callback=snapshot_cb,
            snapshot_steps=(1,),
        )
        kernel.run(envmap, executor, hooks)

        assert 1 in captured_steps, (
            f"Expected snapshot callback called with step=1 but got: {captured_steps}"
        )
        # Step 0 and 2 should NOT appear since only step 1 was requested.
        assert 0 not in captured_steps
        assert 2 not in captured_steps


class TestSnapshotCallbackNotCalledWithoutFlag:
    def test_snapshot_callback_not_called_without_flag(self) -> None:
        """Kernel without snapshot_callback runs normally with no error."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        def noop_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=0, stdout="ok")

        executor.register_command("echo hello", noop_handler)

        ir = _make_ir()
        turns = [
            _act_turn("a-0", "echo hello"),
            SolverTurn(kind="submit_outcome", summary="done"),
        ]
        hooks = _FakeHooks(ir, turns)

        # No snapshot_callback, no snapshot_steps -- baseline unchanged.
        kernel = AetherNextKernel(max_steps=10)
        result = kernel.run(envmap, executor, hooks)

        assert result is not None
        assert isinstance(result, KernelResult)
        # The run should complete normally.
        assert result.step >= 1


class TestSnapshotMultipleSteps:
    def test_snapshot_callback_multiple_steps(self) -> None:
        """Snapshot callback fires for each requested step that is reached."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        def noop_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=0, stdout="ok")

        for i in range(5):
            executor.register_command(f"echo s{i}", noop_handler)

        ir = _make_ir()
        turns = [_act_turn(f"a-{i}", f"echo s{i}") for i in range(5)]
        turns.append(SolverTurn(kind="submit_outcome", summary="done"))
        hooks = _FakeHooks(ir, turns)

        captured: list[int] = []
        kernel = AetherNextKernel(
            max_steps=10,
            snapshot_callback=lambda s: captured.append(s),
            snapshot_steps=(0, 2, 4),
        )
        kernel.run(envmap, executor, hooks)

        assert 0 in captured, f"step 0 missing from {captured}"
        assert 2 in captured, f"step 2 missing from {captured}"
        assert 4 in captured, f"step 4 missing from {captured}"
        assert 1 not in captured
        assert 3 not in captured
