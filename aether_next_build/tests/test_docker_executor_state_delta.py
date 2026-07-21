from __future__ import annotations

from pathlib import Path
import subprocess

from aether_next.runners.docker_exec_executor import DockerExecExecutor


def test_docker_command_result_records_removed_asset_without_invented_actor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    critical = tmp_path / "critical.asset"
    critical.write_bytes(b"present before command")

    def fake_run(args, **kwargs):
        del kwargs
        if isinstance(args, list) and "bash" in args and "-lc" in args:
            critical.unlink()
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(
        "aether_next.runners.docker_exec_executor.subprocess.run",
        fake_run,
    )
    executor = DockerExecExecutor("container", str(tmp_path))
    result = executor.run_command("candidate command")

    assert result.success is True
    assert result.removed_paths == ("critical.asset",)
    assert result.state_delta["removed_paths"] == ["critical.asset"]
    assert result.state_delta["mutation_actor_status"] == "mutation_actor_unknown"
    assert "no subprocess actor is asserted" in result.state_delta["mutation_actor_detail"]


def test_docker_command_result_records_content_and_mode_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text("before", encoding="utf-8")
    mode_only = tmp_path / "mode.txt"
    mode_only.write_text("same", encoding="utf-8")
    mode_only.chmod(0o644)

    def fake_run(args, **kwargs):
        del kwargs
        if isinstance(args, list) and "bash" in args and "-lc" in args:
            changed.write_text("after", encoding="utf-8")
            mode_only.chmod(0o600)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "aether_next.runners.docker_exec_executor.subprocess.run",
        fake_run,
    )
    result = DockerExecExecutor("container", str(tmp_path)).run_command("mutate")

    assert result.modified_paths == ("changed.txt", "mode.txt")
    assert result.state_delta["content_changed_paths"] == ["changed.txt"]
    assert result.state_delta["metadata_changed_paths"] == ["mode.txt"]
