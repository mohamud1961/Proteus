"""Regression tests for HarborExecutor.read_text_file missing-file handling.

Validates that RuntimeError from Harbor's download_file is translated to
FileNotFoundError when the message indicates a missing file, and that genuine
infra errors propagate unchanged.  Also tests the integration with
ExecutionContext.read_file which catches FileNotFoundError gracefully.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.aether2.control.execution_context import ExecutionContext
from harness.aether2.runtime.harbor_backend import HarborExecutor, _build_run_decision_markdown
from harness.aether2.runtime.jobs import JobRegistry
from harness.aether2.runtime.sessions import SessionRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeEnvironmentForRead:
    """Minimal async environment stub focused on download_file behaviour."""

    def __init__(self, *, download_side_effect: BaseException | None = None,
                 download_content: str | None = None) -> None:
        self._download_side_effect = download_side_effect
        self._download_content = download_content

    async def exec(self, command: str, cwd: str | None = None,
                   env: dict[str, str] | None = None,
                   timeout_sec: int | None = None) -> SimpleNamespace:
        return SimpleNamespace(stdout="/app\n", stderr="", return_code=0)

    async def download_file(self, source_path: str, target_path: str | Path) -> None:
        if self._download_side_effect is not None:
            raise self._download_side_effect
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._download_content or "", encoding="utf-8")


def _build_executor(tmp_path: Path, environment: _FakeEnvironmentForRead) -> HarborExecutor:
    """Build a HarborExecutor with minimal stubs; skip snapshot sync."""
    mirror = tmp_path / "mirror"
    scratch = tmp_path / "scratch"
    mirror.mkdir(parents=True, exist_ok=True)
    return HarborExecutor(
        environment=environment,
        remote_workspace_root="/app",
        local_mirror_root=mirror,
        scratch_root=scratch,
        sync_on_init=False,
    )


def _build_context(tmp_path: Path, executor: HarborExecutor) -> ExecutionContext:
    """Build an ExecutionContext backed by the given executor."""
    state_dir = tmp_path / "state"
    raw_log_dir = tmp_path / "raw_logs"
    return ExecutionContext(
        executor=executor,
        job_registry=JobRegistry(state_dir, backend=executor.backend,
                                 container_path_fn=executor.to_container_path),
        session_registry=SessionRegistry(state_dir, backend=executor.backend),
        raw_log_dir=raw_log_dir,
    )


# ---------------------------------------------------------------------------
# Test 1: RuntimeError with missing-file marker -> FileNotFoundError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "Could not find the file /app/solution.txt in container abc123",
    "Error: No such file or directory: /app/missing.py",
    "The requested path does not exist on the remote host",
    "file not found: /app/output.log",
])
def test_missing_file_runtime_error_becomes_file_not_found(tmp_path: Path, message: str) -> None:
    env = _FakeEnvironmentForRead(download_side_effect=RuntimeError(message))
    executor = _build_executor(tmp_path, env)

    with pytest.raises(FileNotFoundError):
        executor.read_text_file("solution.txt")


# ---------------------------------------------------------------------------
# Test 2: Integration — ExecutionContext.read_file graceful envelope
# ---------------------------------------------------------------------------

class _FakeExecutorWithMissingFile:
    """Minimal executor stub that raises FileNotFoundError from read_text_file.

    This verifies that ExecutionContext.read_file catches FileNotFoundError
    and returns a graceful error envelope rather than letting it propagate.
    """

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self.backend = SimpleNamespace(kind="stub")

    def resolve_workspace_path(self, path: str | Path) -> Path:
        return self.workspace_root / path

    def to_container_path(self, path: str | Path) -> str:
        return f"/app/{Path(path).relative_to(self.workspace_root)}"

    def read_text_file(self, path: str | Path) -> str:
        raise FileNotFoundError(str(path))


def test_execution_context_read_file_returns_graceful_envelope_on_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = _FakeExecutorWithMissingFile(workspace)
    state_dir = tmp_path / "state"
    raw_log_dir = tmp_path / "raw_logs"
    ctx = ExecutionContext(
        executor=executor,
        job_registry=JobRegistry(state_dir, backend=executor.backend,
                                 container_path_fn=executor.to_container_path),
        session_registry=SessionRegistry(state_dir, backend=executor.backend),
        raw_log_dir=raw_log_dir,
    )

    result = ctx.read_file("solution.txt")

    # Must NOT raise; must return an envelope with error info
    assert result.exit_code != 0
    assert "file_not_found" in (result.error.kind if result.error else "")


# ---------------------------------------------------------------------------
# Test 3: Genuine infra error re-raises unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "connection refused",
    "docker daemon is not running",
    "timeout waiting for container response",
    "permission denied accessing container filesystem",
])
def test_genuine_infra_error_propagates_as_runtime_error(tmp_path: Path, message: str) -> None:
    env = _FakeEnvironmentForRead(download_side_effect=RuntimeError(message))
    executor = _build_executor(tmp_path, env)

    with pytest.raises(RuntimeError, match=message):
        executor.read_text_file("solution.txt")


# ---------------------------------------------------------------------------
# Test 4: Happy path — content returned successfully
# ---------------------------------------------------------------------------

def test_happy_path_returns_file_content(tmp_path: Path) -> None:
    env = _FakeEnvironmentForRead(download_content="hello world\nsecond line\n")
    executor = _build_executor(tmp_path, env)

    content = executor.read_text_file("readme.txt")

    assert content == "hello world\nsecond line\n"


def test_run_decision_markdown_includes_terminal_model_limited_classes() -> None:
    rendered = _build_run_decision_markdown(
        task_id="qemu-startup",
        adaptive_profile="receipt_driven_full",
        summary="verification still missing final client proof",
        finalize_reason="task_done",
        verifier_readiness=False,
        grader_reward=0.0,
        telemetry={
            "tokens_cached": 1200,
            "tokens_fresh": 340,
            "latency_sec": 8.25,
            "no_progress_streak": 2,
            "proof_state": "not_ready",
            "proof_state_delta": None,
            "rejected_proxy_evidence": ["helper_output_untrusted"],
            "cost_usd": 0.0042,
        },
        primary_failure_class="MODEL_CAPABILITY",
    )

    assert "# RUN_DECISION" in rendered
    assert "MODEL_CAPABILITY" in rendered
    assert "MODEL_VARIANCE" in rendered
    assert "PERCEPTION_SUBSTRATE" in rendered
    assert "no_progress_streak: 2" in rendered
    assert "proof_state: not_ready" in rendered
    assert "proof_state_delta: None" in rendered
    assert "rejected_proxy_evidence: ['helper_output_untrusted']" in rendered
