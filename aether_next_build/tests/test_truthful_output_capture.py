"""Truthful full-output capture: the substrate must never destroy command
output.  Full streams are kept inline up to the inline cap; beyond it the
COMPLETE stream is spooled to disk and retrievable by handle; timeouts
preserve partial output.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping

from aether_next.execution import MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.real_executor import SubprocessExecutor, _INLINE_STREAM_CAP
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CompiledRuntime,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)


def test_output_between_old_cap_and_inline_cap_is_kept_verbatim() -> None:
    # 100k chars: destroyed under the old 20k executor cap; now full inline.
    with tempfile.TemporaryDirectory() as root:
        executor = SubprocessExecutor(root)
        result = executor.run_command(
            "python3 -c \"import sys; sys.stdout.write('x' * 100000 + 'FINAL_MARKER')\"",
            timeout_s=60,
        )
    assert result.exit_code == 0
    assert result.stdout_overflow_path == ""
    assert len(result.stdout) == 100_000 + len("FINAL_MARKER")
    assert result.stdout.endswith("FINAL_MARKER")
    assert result.stdout_bytes_total == len(result.stdout)


def test_output_beyond_inline_cap_is_spooled_completely() -> None:
    total = _INLINE_STREAM_CAP + 200_000
    with tempfile.TemporaryDirectory() as root:
        executor = SubprocessExecutor(root)
        result = executor.run_command(
            f"python3 -c \"import sys; sys.stdout.write('a' * {total} + 'TAIL_MARKER')\"",
            timeout_s=120,
        )
    assert result.exit_code == 0
    assert result.stdout_overflow_path, "oversized stream must be spooled"
    spooled = Path(result.stdout_overflow_path).read_text(encoding="utf-8")
    assert len(spooled) == total + len("TAIL_MARKER")
    assert spooled.endswith("TAIL_MARKER")
    assert result.stdout_bytes_total == len(spooled)
    # Inline text is a marked head+tail, not silent truncation.
    assert "spooled to" in result.stdout
    assert result.stdout.endswith("TAIL_MARKER")


def test_timeout_preserves_partial_output() -> None:
    with tempfile.TemporaryDirectory() as root:
        executor = SubprocessExecutor(root)
        result = executor.run_command(
            "python3 -u -c \"import sys, time; print('PARTIAL_BEFORE_TIMEOUT', flush=True); time.sleep(30)\"",
            timeout_s=2,
        )
    assert result.exit_code == 124
    assert result.timed_out is True
    assert "PARTIAL_BEFORE_TIMEOUT" in result.stdout
    assert "timed out" in result.stderr


class _ReadBackHooks:
    """Solver: run a big command, then read the full stream back by handle."""

    def __init__(self, command: str) -> None:
        self._command = command
        self._step = 0
        self.read_output_payloads: list[dict] = []

    def architect(self, request: Mapping[str, object]) -> RuntimeConfigIR:
        return RuntimeConfigIR(
            architect_summary="capture test",
            solver_identity_prompt="solver",
            verifier_identity_prompt="verifier",
            selected_capabilities=("shell", "filesystem"),
        )

    def solve(self, messages: list[dict[str, str]], compiled: CompiledRuntime) -> SolverTurn:
        self._step += 1
        if self._step == 1:
            return SolverTurn(kind="act", summary="run big command", actions=(ActionRequest(
                action_id="a-big", kind="run_command", capability_id="shell",
                arguments={"command": self._command, "timeout_s": 120},
                intent="produce oversized output", expected_observation="lots of output",
                if_fail_next="report blocker",
            ),))
        if self._step == 2:
            return SolverTurn(kind="act", summary="page and grep the captured output", actions=(ActionRequest(
                action_id="a-observe-output", kind="observe_batch", capability_id="shell",
                arguments={"operations": [
                    {"request_id": "a-read", "kind": "read_output", "arguments": {"handle": "0:a-big:stdout", "offset": 10, "span": 20000}},
                    {"request_id": "a-grep", "kind": "grep_output", "arguments": {"handle": "0:a-big:stdout", "pattern": "TAIL_MARKER"}},
                ]},
                intent="read and grep one immutable captured stream", expected_observation="tail bytes and marker",
                if_fail_next="report blocker",
            ),))
        return SolverTurn(kind="submit_outcome", summary="done")


def test_kernel_handles_page_and_grep_across_spooled_output(tmp_path: Path) -> None:
    total = _INLINE_STREAM_CAP + 100_000
    command = f"python3 -c \"import sys; sys.stdout.write('b' * {total} + 'TAIL_MARKER')\""
    env = EnvMap(
        task_prompt="Run the big command.",
        workspace_root=str(tmp_path),
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands"),
            "filesystem": CapabilityDescriptor("filesystem", "Files"),
        },
    )
    hooks = _ReadBackHooks(command)
    executor = SubprocessExecutor(str(tmp_path))
    result = AetherNextKernel(max_steps=3).run(env, executor, hooks)

    cmd = next(r for r in result.receipts if r.kind == "run_command")
    assert cmd.payload["stdout_overflow_path"]
    assert cmd.payload["stdout_bytes"] == total + len("TAIL_MARKER")

    read = next(r for r in result.receipts if r.kind == "read_output")
    assert read.success
    assert read.payload["bytes"] == total + len("TAIL_MARKER"), (
        "read_output must page over the FULL spooled stream, not the inline excerpt"
    )
    assert read.payload["chunk"] == "b" * 20000

    grep = next(r for r in result.receipts if r.kind == "grep_output")
    assert grep.success
    assert grep.payload["matches"] == 1
    assert "TAIL_MARKER" in grep.payload["chunk"]
