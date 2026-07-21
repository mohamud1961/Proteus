"""Regression test for RC5 (P0.4b): the kernel loop must honor a monotonic
wall-clock deadline as a backstop, so a run terminates and can be graded even
if the docker runner's one-shot SIGALRM is swallowed inside a broad except.

Falsifiability: pre-fix `AetherNextKernel.run` had no `run_timeout_s` parameter
and no per-iteration deadline check, so a solver that never submits would spin
to `max_steps` and never return status="timeout". This test drives exactly that
solver with a `run_timeout_s` far below `max_steps * per_step_cost`, and asserts
the run stops on the deadline well before max_steps.
"""
from __future__ import annotations

import json
import time
from typing import Any, Mapping

from aether_next.execution import MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    EnvMap,
    SolverTurn,
)
from aether_next.workbench_config import parse_harness_config_ir


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Write /app/out.txt",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
        },
    )


def _config_json() -> str:
    return json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "write output",
        "success_definition": "out.txt is correct.",
        "solver_system_prompt": {
            "role": "solver", "workflow": ["write"], "self_verification": ["check"],
            "memory_use": ["none"], "stop_conditions": ["after write"],
        },
        "verifier_system_prompt": {
            "role": "verifier", "success_criteria": ["correct out.txt"],
            "required_evidence": ["state"], "false_positive_traps": ["presence"],
            "verdict_guidance": ["state"], "feedback_guidance": ["concrete"],
        },
        "evidence_requirements": ["current out.txt state"],
        "false_positive_risks": ["wrong content"],
        "minimum_completion_evidence": ["state"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "run_command"]},
        "context_policy": {"mode": "retrieval_augmented"},
        "model_verifier_policy": {"enabled": True},
    })


class _Workbench:
    def configure(self, request: Mapping[str, Any]):
        return parse_harness_config_ir(_config_json()), []


class _SlowNeverSubmitHooks:
    """Solver that only ever acts (never submits), sleeping each step so the
    run would otherwise churn to max_steps.  The only thing that can stop it is
    the kernel-loop deadline."""

    def __init__(self, sleep_s: float) -> None:
        self._sleep_s = sleep_s
        self.solve_calls = 0

    def architect(self, request: Mapping[str, Any]):
        raise AssertionError("workbench mode must not call hooks.architect")

    def solve(self, messages: list[dict[str, str]], compiled) -> SolverTurn:
        self.solve_calls += 1
        time.sleep(self._sleep_s)
        return SolverTurn(kind="act", summary=f"slow act {self.solve_calls}", actions=(ActionRequest(
            action_id=f"a-{self.solve_calls}", kind="write_file", capability_id="filesystem",
            arguments={"path": "out.txt", "content": f"attempt {self.solve_calls}"},
            intent="keep working", expected_observation="file written", if_fail_next="stop",
        ),))

    def verify(self, packet, compiled, ledger):  # pragma: no cover - never submits
        raise AssertionError("verify should not be reached: solver never submits")


def test_kernel_loop_deadline_terminates_before_max_steps() -> None:
    hooks = _SlowNeverSubmitHooks(sleep_s=0.1)
    # max_steps is high so it cannot be the terminator; the 0.05s deadline is
    # crossed after the first ~0.1s step, so the loop must stop on the deadline.
    result = AetherNextKernel(max_steps=50, workbench_architect=_Workbench()).run(
        _env(), MemoryExecutor(workspace_root="/app"), hooks, run_timeout_s=0.05,
    )
    assert result.status == "timeout", f"expected deadline timeout, got {result.status}"
    assert result.step < 50, "run must terminate on the deadline, not run to max_steps"
    assert hooks.solve_calls < 50, "solver should not have been driven to max_steps"
    deadline_receipts = [r for r in result.receipts if r.kind == "kernel_deadline_exceeded"]
    assert deadline_receipts, "a kernel_deadline_exceeded receipt must be recorded"


def test_kernel_no_deadline_when_timeout_unset() -> None:
    # With run_timeout_s=None (the default), the deadline path is inert: a
    # never-submitting solver runs to max_steps as before (no premature timeout).
    hooks = _SlowNeverSubmitHooks(sleep_s=0.0)
    result = AetherNextKernel(max_steps=3, workbench_architect=_Workbench()).run(
        _env(), MemoryExecutor(workspace_root="/app"), hooks,
    )
    assert result.status != "timeout"
    assert hooks.solve_calls == 3
