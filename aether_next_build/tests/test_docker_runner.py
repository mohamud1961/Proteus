"""Tests for DockerExecExecutor against a real Docker container.

Requires Docker to be available.  Tests are skipped cleanly if
``docker info`` fails.

Also includes lightweight unit tests (no Docker required) for
module-level constants and bootstrap behavior.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Skip the entire module if Docker is not available.
_docker_available = False
try:
    _check = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    _docker_available = _check.returncode == 0
except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
    pass

pytestmark = pytest.mark.skipif(
    not _docker_available,
    reason="Docker daemon not available",
)

from aether_next.runners.docker_runner import DockerExecExecutor, run_tbench_task


@pytest.fixture()
def docker_env():
    """Start an Alpine container with a bind-mounted temp workspace.

    Yields ``(container_id, workspace_dir)`` and tears down on exit.
    """
    # Docker Desktop does not always share the platform tempdir on macOS
    # (often /var/folders/...), while /tmp is part of the benchmark-native
    # workspace contract exercised by the isolation smoke.
    workspace = tempfile.mkdtemp(prefix="test_docker_runner_", dir="/tmp")
    container_id: str | None = None
    try:
        start = subprocess.run(
            [
                "docker", "run", "-d",
                "-v", f"{workspace}:/app",
                "-w", "/app",
                "debian:stable-slim",
                "sleep", "infinity",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert start.returncode == 0, f"docker run failed: {start.stderr}"
        container_id = start.stdout.strip()
        yield container_id, workspace
    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True, text=True, timeout=30,
            )
        if os.path.isdir(workspace):
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)


class TestDockerExecExecutor:
    """Integration tests for DockerExecExecutor with a real Alpine container."""

    def test_write_and_read_file(self, docker_env: tuple[str, str]) -> None:
        cid, workspace = docker_env
        executor = DockerExecExecutor(cid, workspace)

        executor.write_file("hello.txt", "hello world\n")
        content = executor.read_file("hello.txt")
        assert content == "hello world\n"

    def test_exists(self, docker_env: tuple[str, str]) -> None:
        cid, workspace = docker_env
        executor = DockerExecExecutor(cid, workspace)

        assert not executor.exists("nope.txt")
        executor.write_file("yep.txt", "yes")
        assert executor.exists("yep.txt")

    def test_run_command_echo(self, docker_env: tuple[str, str]) -> None:
        cid, workspace = docker_env
        executor = DockerExecExecutor(cid, workspace)

        result = executor.run_command("echo hi", timeout_s=10)
        assert result.exit_code == 0
        assert "hi" in result.stdout

    def test_run_command_failing(self, docker_env: tuple[str, str]) -> None:
        cid, workspace = docker_env
        executor = DockerExecExecutor(cid, workspace)

        result = executor.run_command("exit 42", timeout_s=10)
        assert result.exit_code == 42

    def test_run_command_produces_artifacts(self, docker_env: tuple[str, str]) -> None:
        cid, workspace = docker_env
        executor = DockerExecExecutor(cid, workspace)

        result = executor.run_command("echo artifact > /app/new_file.txt", timeout_s=10)
        assert result.exit_code == 0
        assert "new_file.txt" in result.produced_artifacts

        # Verify the file is visible on the host.
        host_file = Path(workspace) / "new_file.txt"
        assert host_file.exists()
        assert "artifact" in host_file.read_text()

    def test_glob(self, docker_env: tuple[str, str]) -> None:
        cid, workspace = docker_env
        executor = DockerExecExecutor(cid, workspace)

        executor.write_file("a.py", "# a")
        executor.write_file("b.py", "# b")
        executor.write_file("c.txt", "c")

        matches = executor.glob("*.py")
        assert "a.py" in matches
        assert "b.py" in matches
        assert "c.txt" not in matches

    def test_teardown_removes_container(self) -> None:
        """Verify that container removal works (manual lifecycle)."""
        workspace = tempfile.mkdtemp(prefix="test_teardown_")
        try:
            start = subprocess.run(
                ["docker", "run", "-d", "debian:stable-slim", "sleep", "infinity"],
                capture_output=True, text=True, timeout=60,
            )
            assert start.returncode == 0
            cid = start.stdout.strip()

            # Remove.
            rm = subprocess.run(
                ["docker", "rm", "-f", cid],
                capture_output=True, text=True, timeout=30,
            )
            assert rm.returncode == 0

            # Verify gone.
            inspect = subprocess.run(
                ["docker", "inspect", cid],
                capture_output=True, text=True, timeout=10,
            )
            assert inspect.returncode != 0
        finally:
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Unit tests (no Docker required)
# ---------------------------------------------------------------------------

class TestGitSafeDirConstant:
    """Verify the _GIT_SAFE_DIR_CMD module constant is well-formed."""

    def test_constant_contains_safe_directory(self) -> None:
        from aether_next.runners.docker_runner import _GIT_SAFE_DIR_CMD

        assert "safe.directory" in _GIT_SAFE_DIR_CMD
        assert "'*'" in _GIT_SAFE_DIR_CMD
        # The trailing `|| true` ensures non-zero exit is swallowed.
        assert "|| true" in _GIT_SAFE_DIR_CMD


class TestCertifiedArchitectModeQuarantine:
    def test_reference_modes_are_rejected_before_docker_work(self) -> None:
        with tempfile.TemporaryDirectory() as task_dir:
            record = run_tbench_task(
                task_dir=task_dir,
                image="debian:stable-slim",
                architect_model=lambda *_args, **_kwargs: "{}",
                solver_model=lambda *_args, **_kwargs: "{}",
                architect_mode="ir",
            )

        assert record["status"] == "error"
        assert record["error"] == "invalid_architect_mode"
        assert record["architect_mode"] == "ir"
        assert "quarantined in reference_legacy" in record["error_detail"]


class TestEffectiveRunTimeout:
    def test_task_declared_budget_raises_runner_floor(self) -> None:
        from aether_next.runners.docker_runner import _effective_run_timeout_s

        effective, policy = _effective_run_timeout_s(1800, {"agent": {"timeout_sec": 3600}})
        assert effective == 3600
        assert "task_declared=3600" in policy

    def test_missing_budget_keeps_runner_default(self) -> None:
        from aether_next.runners.docker_runner import _effective_run_timeout_s

        effective, policy = _effective_run_timeout_s(1800, {})
        assert effective == 1800
        assert "runner_default" in policy

    def test_declared_budget_is_capped_and_never_lowers_floor(self) -> None:
        from aether_next.runners.docker_runner import (
            _MAX_RUN_TIMEOUT_S,
            _effective_run_timeout_s,
        )

        effective, _ = _effective_run_timeout_s(1800, {"agent": {"timeout_sec": 999999}})
        assert effective == _MAX_RUN_TIMEOUT_S
        effective, _ = _effective_run_timeout_s(1800, {"agent": {"timeout_sec": 60}})
        assert effective == 1800
