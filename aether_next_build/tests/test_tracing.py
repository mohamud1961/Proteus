"""Tests for opt-in RunTrace capture in the kernel."""
from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel, KernelResult
from aether_next.ledger import ExecutionLedger
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CompletionPolicy,
    CompiledRuntime,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)
from aether_next.tracing import RunTrace


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
        check_plan=check_plan,
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


class TestRunTraceCapturesFullStepDetail:
    def test_run_trace_captures_full_step_detail(self) -> None:
        """A run with a run_command act turn and a submit turn populates
        a complete trace with architect config, steps, and gate decisions."""
        envmap = _make_envmap()
        executor = MemoryExecutor(workspace_root="/app")

        def echo_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0,
                stdout="hello world output",
            )

        executor.register_command("echo hello", echo_handler)

        run_cmd = _action(
            "run_command",
            {"command": "echo hello"},
            action_id="a-run-1",
        )
        ir = _make_ir()
        hooks = FakeHooks(ir, [_act_turn(run_cmd), _submit_turn()])
        kernel = AetherNextKernel(max_steps=5)

        trace = RunTrace()
        result = kernel.run(envmap, executor, hooks, trace=trace)

        td = trace.to_dict()

        # 1. architect_config is a non-empty dict containing selected_capabilities
        assert isinstance(td["architect_config"], dict)
        assert len(td["architect_config"]) > 0
        assert "selected_capabilities" in td["architect_config"]

        # 2. At least 2 steps; each has context_seen, turn, observations
        steps = td["steps"]
        assert len(steps) >= 2, f"Expected >= 2 steps, got {len(steps)}"
        for s in steps:
            assert "context_seen" in s
            assert "turn" in s
            assert "observations" in s

        # 3. At least one step's observations contains a stdout_tail key
        found_stdout_tail = False
        for s in steps:
            for obs in s["observations"]:
                if "stdout_tail" in obs:
                    found_stdout_tail = True
                    break
            if found_stdout_tail:
                break
        assert found_stdout_tail, "No observation with stdout_tail found"

        # 4. gate_decisions is non-empty and has a ready bool
        assert len(td["gate_decisions"]) > 0
        gd = td["gate_decisions"][0]
        assert "ready" in gd
        assert isinstance(gd["ready"], bool)


class _VerifyingFakeHooks(FakeHooks):
    """Like FakeHooks, but with a verify() that always says needs_repair."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.verify_call_count = 0

    def verify(self, packet: Mapping[str, Any], compiled: CompiledRuntime, ledger: Any) -> str:
        self.verify_call_count += 1
        return json.dumps({
            "verdict": "needs_repair",
            "confidence": "high",
            "summary": "not enough evidence yet",
            "findings": [{
                "finding_id": "missing-evidence",
                "summary": "no adequate evidence yet",
                "evidence": ["placeholder"],
                "repair_instruction": "gather more evidence",
                "applies_to": ["out.txt"],
            }],
            "missing_evidence_requests": ["more evidence"],
        })


def test_submit_outcome_verifier_receipts_are_captured_in_trace() -> None:
    """Regression found while auditing the Stage 1 repair-slice VM rerun: the
    submit_outcome branch used to call trace.add_step() BEFORE invoking the
    model verifier, so every verifier receipt from that branch (both the
    ready-but-not-completed and the not-ready/deterministic_failure paths)
    was silently dropped from the trace forever -- neither that step's slice
    (already taken) nor the next step's slice (starts counting after those
    receipts already exist) ever captured them. On the VM this meant a task
    could call the verifier 7 times across a run and the trace would only
    show 1 of them, making "trace-proven" false even after trace writing
    itself was fixed. Prove every verify() call's receipts land in some
    step's observations.
    """
    envmap = _make_envmap()
    executor = MemoryExecutor(workspace_root="/app")
    ir = _make_ir()
    hooks = _VerifyingFakeHooks(ir, [_submit_turn(), _submit_turn(), _submit_turn()])
    kernel = AetherNextKernel(max_steps=5)
    trace = RunTrace()

    kernel.run(envmap, executor, hooks, trace=trace)

    assert hooks.verify_call_count >= 2, "test setup should have triggered multiple verifier calls"
    td = trace.to_dict()
    verifier_result_observations = [
        obs
        for step in td["steps"]
        for obs in step["observations"]
        if obs.get("kind") == "model_verifier_result"
    ]
    assert len(verifier_result_observations) == hooks.verify_call_count, (
        f"expected all {hooks.verify_call_count} verifier calls to appear in the trace, "
        f"found {len(verifier_result_observations)}"
    )


class TestTraceNoneIsNoop:
    def test_trace_none_is_noop(self) -> None:
        """Running the kernel WITHOUT trace returns the same status and
        receipt count as a run WITH trace -- tracing does not change behavior."""
        envmap = _make_envmap()

        def echo_handler(ex: MemoryExecutor, cmd: str) -> CommandResult:
            return CommandResult(command=cmd, exit_code=0, stdout="ok")

        # Run with trace
        executor_traced = MemoryExecutor(workspace_root="/app")
        executor_traced.register_command("echo hello", echo_handler)
        run_cmd = _action("run_command", {"command": "echo hello"}, action_id="a-run-1")
        ir = _make_ir()
        hooks_traced = FakeHooks(ir, [_act_turn(run_cmd), _submit_turn()])
        kernel = AetherNextKernel(max_steps=5)
        trace = RunTrace()
        result_traced = kernel.run(envmap, executor_traced, hooks_traced, trace=trace)

        # Run without trace
        executor_plain = MemoryExecutor(workspace_root="/app")
        executor_plain.register_command("echo hello", echo_handler)
        hooks_plain = FakeHooks(ir, [_act_turn(run_cmd), _submit_turn()])
        result_plain = kernel.run(envmap, executor_plain, hooks_plain)

        # Same status and receipt count
        assert result_traced.status == result_plain.status
        assert len(result_traced.receipts) == len(result_plain.receipts)


def test_write_trace_file_matches_docker_runner_call_signature(tmp_path) -> None:
    """Regression: docker_runner._write_trace_file() calls
    ``run_trace.to_dict(task=..., image=..., reward=..., status=...)``. A prior
    refactor dropped those kwargs from RunTrace.to_dict, so every real VM run
    silently failed to write its trace file (caught by a bare except and
    recorded only as trace_write_error, with the real TypeError discarded).
    Exercise the actual call site, not just to_dict() in isolation, so a
    future signature drift fails loudly here instead of only on a live run.
    """
    from aether_next.runners.docker_runner import _write_trace_file

    trace = RunTrace()
    trace.set_architect(_make_ir(), fallback_codes=(), prefix_messages=[])

    _write_trace_file(
        str(tmp_path), "demo-task", "demo/image:tag",
        reward=1.0, status="completed", run_trace=trace,
    )

    written = json.loads((tmp_path / "demo-task.trace.json").read_text())
    assert written["task"] == "demo-task"
    assert written["image"] == "demo/image:tag"
    assert written["reward"] == 1.0
    assert written["status"] == "completed"


def test_trace_write_helper_records_path_and_error_detail(tmp_path) -> None:
    """Result rows should preserve the trace path and the real write error."""
    from aether_next.runners.docker_runner import _write_trace_file_to_record

    class BrokenTrace:
        def to_dict(self, **kwargs):
            raise TypeError("signature drift")

    record: dict[str, object] = {}

    _write_trace_file_to_record(
        record,
        str(tmp_path),
        "demo-task",
        "demo/image:tag",
        reward=0.0,
        status="error",
        run_trace=BrokenTrace(),
    )

    assert record["trace_path"] == str(tmp_path / "demo-task.trace.json")
    assert record["trace_write_error"] == "failed_to_write_trace_file"
    assert record["trace_write_error_type"] == "TypeError"
    assert record["trace_write_error_detail"] == "signature drift"
