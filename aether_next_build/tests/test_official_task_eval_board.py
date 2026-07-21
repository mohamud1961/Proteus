from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


BUILD_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = BUILD_ROOT / "scripts" / "run_official_task_eval_board.py"
BOARD = BUILD_ROOT / "evals" / "official_task_board.v1.json"


def _public_smoke_corpus(root: Path) -> None:
    board = json.loads(BOARD.read_text(encoding="utf-8"))
    for row in board["smoke_board"]:
        task = root / row["task_id"]
        task.mkdir(parents=True)
        (task / "instruction.md").write_text("Complete this public task.\n", encoding="utf-8")
        (task / "task.toml").write_text('docker_image = "example/task:latest"\n', encoding="utf-8")


def test_official_board_defaults_to_plan_only_and_finalizes(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    _public_smoke_corpus(tasks)
    out = tmp_path / "plan"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--board", "smoke",
            "--tasks-dir", str(tasks),
            "--output-dir", str(out),
            "--network-scope", "loopback_only",
        ],
        cwd=BUILD_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert plan["task_count"] == 24
    assert plan["taxonomy_delivery_to_harness"] is False
    assert plan["model_execution_allowed"] is False
    assert plan["network_scope"] == "loopback_only"
    assert (out / "FINALIZED.json").is_file()
    assert not list(out.glob("sample_*"))


def test_model_board_refuses_to_start_without_deterministic_pass(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    _public_smoke_corpus(tasks)
    out = tmp_path / "blocked"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--board", "smoke",
            "--tasks-dir", str(tasks),
            "--output-dir", str(out),
            "--allow-model",
        ],
        cwd=BUILD_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
    )
    assert proc.returncode != 0
    assert "--deterministic-summary is required" in proc.stderr
    assert not list(out.glob("sample_*"))
