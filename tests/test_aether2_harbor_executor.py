from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from harness.aether2.control.execution_context import ExecutionContext
from harness.aether2.control.execution_context import RunResult
from harness.aether2.runtime.bridge_harbor import _attach_grader_reward, build_harbor_run_manifest
from harness.aether2.runtime.executor import ContainerExecutor
from harness.aether2.runtime.harbor_backend import HarborExecutor, HarborSessionRegistry, probe_harbor_workspace
from harness.aether2.runtime.jobs import JobRegistry
from harness.aether2.runtime.sessions import SessionRegistry
from harness.aether2.runtime.task_spec import TaskSpec


class FakeHarborEnvironment:
    def __init__(self, remote_root_dir: Path, remote_workspace_root: str = "/app") -> None:
        self.remote_root_dir = remote_root_dir
        self.remote_workspace_root = remote_workspace_root.rstrip("/") or "/app"
        self.exec_calls: list[dict[str, object]] = []
        self.upload_file_calls: list[tuple[str, str]] = []
        self.download_file_calls: list[tuple[str, str]] = []
        self.download_dir_calls: list[tuple[str, str]] = []

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> SimpleNamespace:
        rewritten_command = command.replace(self.remote_workspace_root, str(self.remote_root_dir))
        self.exec_calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": dict(env or {}),
                "timeout_sec": timeout_sec,
            }
        )
        completed = subprocess.run(
            ["/bin/sh", "-lc", rewritten_command],
            cwd=str(self._remote_to_local(cwd or self.remote_workspace_root)),
            env={**os.environ, **dict(env or {})},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
        stdout = completed.stdout.replace(str(self.remote_root_dir), self.remote_workspace_root)
        stderr = completed.stderr.replace(str(self.remote_root_dir), self.remote_workspace_root)
        return SimpleNamespace(
            stdout=stdout,
            stderr=stderr,
            return_code=completed.returncode,
        )

    async def upload_file(self, source_path: Path | str, target_path: str):
        source = Path(source_path)
        target = self._remote_to_local(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_text(encoding="utf-8", errors="replace")
        content = content.replace(self.remote_workspace_root, str(self.remote_root_dir))
        target.write_text(content, encoding="utf-8")
        self.upload_file_calls.append((str(source), target_path))

    async def upload_dir(self, source_dir: Path | str, target_dir: str):
        source = Path(source_dir)
        target = self._remote_to_local(target_dir)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

    async def download_file(self, source_path: str, target_path: Path | str):
        source = self._remote_to_local(source_path)
        if not source.exists():
            raise FileNotFoundError(source_path)
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        self.download_file_calls.append((source_path, str(target)))

    async def download_dir(self, source_dir: str, target_dir: Path | str):
        source = self._remote_to_local(source_dir)
        target = Path(target_dir)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        self.download_dir_calls.append((source_dir, str(target)))

    def _remote_to_local(self, remote_path: str) -> Path:
        normalized = remote_path.rstrip("/") or self.remote_workspace_root
        if normalized == self.remote_workspace_root:
            relative = Path(".")
        elif normalized.startswith(f"{self.remote_workspace_root}/"):
            relative = Path(normalized.removeprefix(f"{self.remote_workspace_root}/"))
        elif normalized.startswith("/tmp/"):
            return Path(normalized).resolve(strict=False)
        else:
            raise ValueError(f"unexpected remote path: {remote_path}")
        return (self.remote_root_dir / relative).resolve(strict=False)


class HarborDownloadError(Exception):
    pass


class MissingFileHarborEnvironment(FakeHarborEnvironment):
    async def download_file(self, source_path: str, target_path: Path | str):
        raise HarborDownloadError(
            f"Error response from daemon: Could not find the file {source_path} in container abc123"
        )


def _make_context(tmp_path: Path) -> tuple[ExecutionContext, HarborExecutor, FakeHarborEnvironment, Path]:
    remote_root = tmp_path / "remote_workspace"
    remote_root.mkdir(parents=True)
    environment = FakeHarborEnvironment(remote_root)
    mirror_root = tmp_path / "logs" / "tmp" / "harbor_mirror"
    scratch_root = tmp_path / "logs" / "tmp" / "harbor_staging"
    executor = HarborExecutor(
        environment=environment,
        remote_workspace_root="/app",
        local_mirror_root=mirror_root,
        scratch_root=scratch_root,
    )
    state_dir = tmp_path / "state"
    raw_log_dir = tmp_path / "raw_logs"
    ctx = ExecutionContext(
        executor=executor,
        job_registry=JobRegistry(state_dir, backend=executor.backend, container_path_fn=executor.to_container_path),
        session_registry=SessionRegistry(state_dir, backend=executor.backend),
        raw_log_dir=raw_log_dir,
    )
    return ctx, executor, environment, remote_root


def test_probe_harbor_workspace_prefers_git_root_when_present(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote_workspace"
    nested = remote_root / "subdir"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(remote_root), check=True, capture_output=True, text=True)
    environment = FakeHarborEnvironment(remote_root)

    probe = probe_harbor_workspace(environment)

    assert probe.pwd == "/app"
    assert probe.git_root == "/app"
    assert probe.workspace_root == "/app"
    assert probe.existing_candidates == ("/app",)


def test_run_command_executes_through_harbor_environment_and_refreshes_mirror(tmp_path: Path) -> None:
    ctx, executor, environment, remote_root = _make_context(tmp_path)
    (remote_root / "seed.txt").write_text("remote seed\n", encoding="utf-8")
    executor.prepare_snapshot()

    result = ctx.run_command("pwd && printf harbor-run > command.txt", timeout_sec=10)

    assert result.exit_code == 0
    assert result.stdout_head.startswith("/app")
    assert environment.exec_calls[-1]["cwd"] == "/app"
    assert (remote_root / "command.txt").read_text(encoding="utf-8") == "harbor-run"
    assert (executor.workspace_root / "command.txt").read_text(encoding="utf-8") == "harbor-run"
    assert any(delta.path == "command.txt" and delta.change_type == "added" for delta in result.files_changed)


def test_prepare_snapshot_skips_heavy_task_assets(tmp_path: Path) -> None:
    ctx, executor, _environment, remote_root = _make_context(tmp_path)
    (remote_root / "alpine-disk.qcow2").write_bytes(b"large disk placeholder")
    (remote_root / "notes.txt").write_text("small evidence\n", encoding="utf-8")

    ctx.last_snapshot = ctx._capture_snapshot()
    executor.prepare_snapshot()

    assert not (executor.workspace_root / "alpine-disk.qcow2").exists()
    assert (executor.workspace_root / "notes.txt").read_text(encoding="utf-8") == "small evidence\n"


def test_read_file_downloads_remote_truth_not_stale_local_mirror(tmp_path: Path) -> None:
    ctx, executor, environment, remote_root = _make_context(tmp_path)
    (remote_root / "note.txt").write_text("remote truth\n", encoding="utf-8")
    executor.prepare_snapshot()
    (executor.workspace_root / "note.txt").write_text("stale mirror\n", encoding="utf-8")

    result = ctx.read_file("note.txt")

    assert result.exit_code == 0
    assert result.stdout_head == "remote truth\n"
    assert any(source == "/app/note.txt" for source, _target in environment.download_file_calls)
    assert (executor.workspace_root / "note.txt").read_text(encoding="utf-8") == "remote truth\n"


def test_read_file_missing_remote_file_returns_failed_observation_not_harbor_exception(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote_workspace"
    remote_root.mkdir(parents=True)
    environment = MissingFileHarborEnvironment(remote_root)
    executor = HarborExecutor(
        environment=environment,
        remote_workspace_root="/app",
        local_mirror_root=tmp_path / "logs" / "tmp" / "harbor_mirror",
        scratch_root=tmp_path / "logs" / "tmp" / "harbor_staging",
        sync_on_init=False,
    )
    executor.prepare_snapshot = lambda: None  # type: ignore[method-assign]
    state_dir = tmp_path / "state"
    ctx = ExecutionContext(
        executor=executor,
        job_registry=JobRegistry(state_dir, backend=executor.backend, container_path_fn=executor.to_container_path),
        session_registry=SessionRegistry(state_dir, backend=executor.backend),
        raw_log_dir=tmp_path / "raw_logs",
    )

    result = ctx.read_file("solution.txt")

    assert result.exit_code == 1
    assert result.error is not None
    assert result.error.kind == "file_not_found"
    assert "solution.txt" in result.stderr_head


def test_write_file_uploads_to_remote_workspace_and_updates_delta(tmp_path: Path) -> None:
    ctx, executor, environment, remote_root = _make_context(tmp_path)

    result = ctx.write_file("nested/out.txt", "hello harbor\n")

    assert result.exit_code == 0
    assert environment.upload_file_calls[-1][1] == "/app/nested/out.txt"
    assert (remote_root / "nested" / "out.txt").read_text(encoding="utf-8") == "hello harbor\n"
    assert (executor.workspace_root / "nested" / "out.txt").read_text(encoding="utf-8") == "hello harbor\n"
    assert any(delta.path == "nested/out.txt" and delta.change_type == "added" for delta in result.files_changed)


def test_harbor_grader_reward_is_post_agent_result_attribution(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task_dir = tmp_path / "task"
    artifacts = tmp_path / "artifacts"
    (workspace / "logs" / "verifier").mkdir(parents=True)
    task_dir.mkdir()
    artifacts.mkdir()
    (workspace / "logs" / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")
    executor = ContainerExecutor(workspace_root=workspace)
    result = RunResult(
        verifier_clean=True,
        finalize_reason="task_done",
        summary="done",
        steps=1,
        model_calls=1,
        tokens_cached=0,
        tokens_fresh=0,
        cost=0.0,
        wall_time=0.1,
        no_delta_streaks=0,
        verification_rounds=1,
        recoveries=0,
        compaction_count=0,
        job_survival=True,
        session_survival=True,
    )

    attached = _attach_grader_reward(result, executor)
    assert result.grader_reward is None
    assert attached.grader_reward == 1.0

    task = TaskSpec(
        task_id="sample",
        instruction="do the task",
        task_dir=task_dir,
        workspace_root=workspace,
        artifacts_dir=artifacts,
    )
    prepared = build_harbor_run_manifest(task, runtime_mode="local")
    completed = build_harbor_run_manifest(
        task,
        runtime_mode="local",
        result_summary={
            "verifier_clean": True,
            "finalize_reason": "task_done",
            "grader_reward": attached.grader_reward,
        },
    )

    assert "result_attribution" not in prepared
    assert completed["result_attribution"]["grader_reward"] == 1.0
    assert completed["result_attribution"]["official_grader_phase"] == "post_agent"
    assert completed["result_attribution"]["official_grader_agent_visible"] is False
    assert completed["result_attribution"]["official_grader_authority"] == "external_measurement"


def test_harbor_session_registry_can_start_send_and_read(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote_workspace"
    remote_root.mkdir(parents=True)
    environment = FakeHarborEnvironment(remote_root)
    registry = HarborSessionRegistry(
        tmp_path / "state",
        environment=environment,
        remote_workspace_root="/app",
    )

    session_id = registry.start("console", "cat")
    try:
        registry.send(session_id, "hello harbor")
        registry.send(session_id, "Enter")

        deadline = time.monotonic() + 5
        screen = ""
        while time.monotonic() < deadline:
            screen = registry.read(session_id)
            if "hello harbor" in screen:
                break
            time.sleep(0.1)

        assert "hello harbor" in screen
        assert session_id in registry.list_session_ids()
    finally:
        registry.stop(session_id)

    assert session_id not in registry.list_session_ids()


def test_wait_refreshes_snapshot_before_diffing_out_of_band_remote_changes(tmp_path: Path) -> None:
    ctx, _executor, _environment, remote_root = _make_context(tmp_path)
    (remote_root / "watched.txt").write_text("before\n", encoding="utf-8")
    ctx.last_snapshot = ctx._capture_snapshot()

    (remote_root / "watched.txt").write_text("after\n", encoding="utf-8")
    result = ctx.wait(0, "observe remote mutation")

    assert result.exit_code == 0
    assert any(delta.path == "watched.txt" and delta.change_type == "modified" for delta in result.files_changed)


def test_start_job_and_job_status_run_inside_harbor_environment(tmp_path: Path) -> None:
    ctx, executor, environment, remote_root = _make_context(tmp_path)

    started = ctx.start_job(
        "printf started > service.log; sleep 1; printf done >> service.log",
        job_id="svc",
    )
    live = ctx.job_status("svc")

    assert started.exit_code == 0
    assert "started job svc" in started.stdout_head
    assert live.exit_code in {0, None}
    assert environment.upload_file_calls
    assert (remote_root / ".aether2" / "harbor_jobs" / "svc" / "run.sh").exists()

    time_limit = 30
    while time_limit > 0:
        final = ctx.job_status("svc")
        if "exit_code=0" in final.stdout_head:
            break
        time.sleep(0.1)
        time_limit -= 1

    assert "exit_code=0" in final.stdout_head
    assert (remote_root / "service.log").read_text(encoding="utf-8") == "starteddone"
    assert (executor.workspace_root / "service.log").read_text(encoding="utf-8") == "starteddone"
