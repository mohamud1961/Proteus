"""Offline demo entrypoint for the canonical harness runtime."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path


def _tool_call(name: str, arguments: dict, call_id: str) -> dict:
    """Build a tool call in the shape the continuity loop dispatches."""
    return {"id": call_id, "type": "function", "name": name, "arguments": json.dumps(arguments)}


def _run_demo() -> None:
    # ------------------------------------------------------------------ #
    # Imports — all from harness, never from runner                        #
    # ------------------------------------------------------------------ #
    from harness.aether2.control.loop import run_aether2_loop
    from harness.aether2.runtime.executor import ContainerExecutor
    from harness.aether2.runtime.model_client import Aether2ModelClient
    from harness.aether2.runtime.model_routes import LocalStubModelClient
    from harness.aether2.runtime.task_spec import TaskSpec

    # ------------------------------------------------------------------ #
    # Stub model client — responds immediately, no credentials required    #
    # ------------------------------------------------------------------ #
    stub = LocalStubModelClient.create(response_text="demo complete")

    # Aether2ModelClient wraps any object with a .complete() method and
    # provides the .call() interface that run_aether2_loop expects.
    # Script the stub to take one real tool step (write the file) and then
    # finalize via task_done, so the demo shows the agent actually acting.
    model_client = Aether2ModelClient(
        stub.route,
        planned_completions=[
            {
                "text": "Writing DONE to result.txt.",
                "tool_calls": [_tool_call("write_file", {"path": "result.txt", "content": "DONE"}, "call-1")],
                "usage": {"cached_input_tokens": 0, "fresh_input_tokens": 6},
                "status": "in_progress",
            },
            {
                "text": "result.txt now contains DONE; task complete.",
                "tool_calls": [
                    _tool_call(
                        "task_done",
                        {"summary": "Wrote DONE to result.txt.", "checks": ["result.txt contains DONE"]},
                        "call-2",
                    )
                ],
                "usage": {"cached_input_tokens": 0, "fresh_input_tokens": 6},
                "status": "completed",
            },
        ],
    )

    # ------------------------------------------------------------------ #
    # Temporary workspace — cleaned up automatically on exit               #
    # ------------------------------------------------------------------ #
    with tempfile.TemporaryDirectory(prefix="aether2_demo_") as tmp:
        workspace_root = Path(tmp) / "workspace"
        task_dir = workspace_root / "task"
        artifacts_dir = workspace_root / "artifacts"
        for d in (workspace_root, task_dir, artifacts_dir):
            d.mkdir(parents=True)

        task = TaskSpec(
            task_id="offline_demo_v1",
            instruction="Write the word DONE to a file called result.txt.",
            task_dir=task_dir,
            workspace_root=workspace_root,
            artifacts_dir=artifacts_dir,
        )

        executor = ContainerExecutor(workspace_root=workspace_root)

        # Give the loop a 15-second wall-clock budget; the stub resolves
        # on the first turn so the loop exits almost immediately.
        deadline_ts = time.monotonic() + 15.0

        result = run_aether2_loop(
            task,
            model_client,
            executor,
            deadline_ts=deadline_ts,
        )

    # ------------------------------------------------------------------ #
    # Print RunResult summary                                              #
    # ------------------------------------------------------------------ #
    print("=== harness.aether2 offline demo ===")
    print(f"task_id        : {task.task_id}")
    print(f"finalize_reason: {result.finalize_reason}")
    print(f"verifier_readiness : {result.verifier_readiness}")
    print(f"steps          : {result.steps}")
    print(f"model_calls    : {result.model_calls}")
    print(f"wall_time_s    : {result.wall_time:.2f}")
    print(f"summary        : {result.summary!r}")


if __name__ == "__main__":
    _run_demo()
    sys.exit(0)
