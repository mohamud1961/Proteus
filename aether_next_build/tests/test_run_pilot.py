from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import run_pilot


def test_run_pilot_persists_in_progress_row_before_task_runner_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    task_dir = tasks_dir / "demo-task"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('docker_image = "demo:latest"\n', encoding="utf-8")
    out_path = tmp_path / "results.json"

    monkeypatch.setattr(run_pilot, "make_azure_callable", lambda **_kwargs: (lambda *_a, **_k: "{}"))
    monkeypatch.setattr(run_pilot, "_resolve_task_image", lambda _task_dir: "demo:latest")

    def fake_run_tbench_task(**kwargs):
        assert kwargs["network_scope"] == "loopback_only"
        rows = json.loads(out_path.read_text(encoding="utf-8"))
        assert rows == [
            {
                "task": "demo-task",
                "image": "demo:latest",
                "architect_mode": "workbench",
                "reward": 0.0,
                "status": "running",
                "kernel_status": "running",
                "error": "attempt_in_progress",
                "error_detail": "task attempt started but no terminal result row has been written yet",
                "classifier_label": "attempt_in_progress",
                "classifier_confidence": "high",
                "classifier_detail": "non-terminal launch receipt; replace with completed/error row when run_tbench_task returns",
                "step": 0,
                "reconfigurations": 0,
                "model_parse_errors": [],
                "grader_exit": -1,
                "grader_stdout_tail": "",
                "grader_stderr_tail": "",
                "receipt_summary": [],
            }
        ]
        return {
            "task": "demo-task",
            "image": "demo:latest",
            "architect_mode": "workbench",
            "reward": 1.0,
            "status": "completed",
            "kernel_status": "completed",
            "classifier_label": "none",
            "classifier_confidence": "high",
            "classifier_detail": "completed",
            "step": 1,
            "reconfigurations": 0,
            "model_parse_errors": [],
            "grader_exit": 0,
            "grader_stdout_tail": "",
            "grader_stderr_tail": "",
            "receipt_summary": [],
        }

    monkeypatch.setattr(run_pilot, "run_tbench_task", fake_run_tbench_task)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pilot.py",
            "--tasks-dir",
            str(tasks_dir),
            "--tasks",
            "demo-task",
            "--out",
            str(out_path),
            "--network-scope",
            "loopback_only",
        ],
    )

    assert run_pilot.main() == 0

    rows = json.loads(out_path.read_text(encoding="utf-8"))
    assert rows[0]["status"] == "completed"
    assert rows[0]["reward"] == 1.0


def test_resolve_task_image_reads_yaml_declared_image(tmp_path: Path) -> None:
    task_dir = tmp_path / "yaml-task"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        'instruction: "do it"\n'
        'docker_image: "example/yaml:latest"\n'
        'max_agent_timeout_sec: 900.0\n',
        encoding="utf-8",
    )

    assert run_pilot._resolve_task_image(str(task_dir)) == "example/yaml:latest"


def test_resolve_task_image_builds_dockerfile_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = tmp_path / "yaml-task"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text('instruction: "do it"\n', encoding="utf-8")
    (task_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        class Proc:
            returncode = 1 if cmd[:3] == ["docker", "image", "inspect"] else 0
            stdout = ""
            stderr = ""
        return Proc()

    monkeypatch.setattr(run_pilot.subprocess, "run", fake_run)

    image = run_pilot._resolve_task_image(str(task_dir))

    assert image.startswith("aether-next-task-yaml-task-")
    assert calls[0][:3] == ["docker", "image", "inspect"]
    assert calls[1][:3] == ["docker", "build", "-t"]
