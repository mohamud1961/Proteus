from __future__ import annotations

from harness.aether2.runtime.executor import ContainerExecutor


def test_run_command_boundary_allows_http_route_literals(tmp_path) -> None:
    executor = ContainerExecutor(tmp_path)

    result = executor.run(
        "python3 - <<'PY'\nprint('/health')\nprint('/echo')\nPY",
        timeout_sec=10,
    )

    assert result.exit_code == 0
    assert result.boundary_violation is False
    assert "/health" in result.stdout
    assert "/echo" in result.stdout


def test_run_command_boundary_blocks_absolute_filesystem_paths(tmp_path) -> None:
    executor = ContainerExecutor(tmp_path)

    result = executor.run("cat /etc/passwd", timeout_sec=10)

    assert result.exit_code == 126
    assert result.boundary_violation is True
    assert result.error is not None
    assert result.error.reason_code == "workspace_boundary_violation"


def test_run_command_boundary_allows_container_tmp_runtime_paths(tmp_path) -> None:
    executor = ContainerExecutor(tmp_path)

    result = executor.run("printf ok > /tmp/aether2-runtime-token", timeout_sec=10)

    assert result.exit_code == 0
    assert result.boundary_violation is False


def test_run_command_boundary_blocks_simple_nested_shell_wrappers(tmp_path) -> None:
    executor = ContainerExecutor(tmp_path)

    result = executor.run('sh -lc "cat /etc/passwd"', timeout_sec=10)

    assert result.exit_code == 126
    assert result.boundary_violation is True
    assert result.error is not None
    assert result.error.reason_code == "workspace_boundary_violation"


def test_run_command_boundary_ignores_heredoc_literal_path_content(tmp_path) -> None:
    executor = ContainerExecutor(tmp_path)

    result = executor.run(
        "python3 - <<'PY'\nprint({'workspace_root': '/workspace/runtime'})\nPY",
        timeout_sec=10,
    )

    assert result.exit_code == 0
    assert result.boundary_violation is False
    assert "/workspace/runtime" in result.stdout


def test_run_command_boundary_blocks_nested_shell_pipeline_path_escape(tmp_path) -> None:
    executor = ContainerExecutor(tmp_path)

    result = executor.run('sh -lc "cat /etc/passwd | head -n 1"', timeout_sec=10)

    assert result.exit_code == 126
    assert result.boundary_violation is True
    assert result.error is not None
    assert result.error.reason_code == "workspace_boundary_violation"
