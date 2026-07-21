from __future__ import annotations

import tempfile

from aether_next.runners.docker_runner import run_tbench_task


class HonestCallable:
    def preflight_request(self, *, max_output_tokens: int, logical_role: str):
        return {
            "provider": "test-provider",
            "model": "test-model",
            "provider_role": logical_role,
            "effort": "low",
            "max_output_tokens": max_output_tokens,
            "background": True,
        }

    def __call__(self, messages, *, max_output_tokens=8000):
        raise AssertionError("this test must stop before any model call")


class ClampingCallable(HonestCallable):
    def __init__(self) -> None:
        self.called = False

    def preflight_request(self, *, max_output_tokens: int, logical_role: str):
        row = super().preflight_request(
            max_output_tokens=max_output_tokens,
            logical_role=logical_role,
        )
        if logical_role == "solver":
            row["max_output_tokens"] = 1200
        return row

    def __call__(self, messages, *, max_output_tokens=8000):
        self.called = True
        raise AssertionError("model call must not happen")


def test_runner_rejects_solver_output_budget_mismatch_before_docker_or_model() -> None:
    architect = HonestCallable()
    solver = ClampingCallable()
    with tempfile.TemporaryDirectory() as task_dir:
        record = run_tbench_task(
            task_dir=task_dir,
            image="image-that-must-not-be-used",
            architect_model=architect,
            solver_model=solver,
            architect_mode="workbench",
        )
    assert record["status"] == "error"
    assert record["error"] == "model_request_output_budget_mismatch"
    assert "16000" in record["error_detail"]
    assert "1200" in record["error_detail"]
    assert solver.called is False
