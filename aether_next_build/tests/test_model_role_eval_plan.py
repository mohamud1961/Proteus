from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


BUILD_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = BUILD_ROOT / "scripts" / "run_model_role_eval_plan.py"
BOARDS = BUILD_ROOT / "evals" / "model_boards.v1.json"


def test_every_model_role_board_has_metrics_and_promotion_rules() -> None:
    payload = json.loads(BOARDS.read_text(encoding="utf-8"))
    assert set(payload["boards"]) == {
        "architect", "solver", "verifier", "perception", "system_smoke", "system_full",
    }
    for name, board in payload["boards"].items():
        assert board["purpose"], name
        assert board["runner"], name
        assert board["metrics"], name
        assert board["promotion"], name
    assert payload["global_rules"]["task_taxonomy_delivered_to_models"] is False


def test_model_role_plan_defaults_to_no_execution_and_finalizes(tmp_path: Path) -> None:
    out = tmp_path / "architect-plan"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--board", "architect",
            "--output-dir", str(out),
        ],
        cwd=BUILD_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert plan["status"] == "plan_only"
    assert plan["model_execution_requested"] is False
    assert plan["taxonomy_delivery_to_models"] is False
    assert (out / "FINALIZED.json").is_file()


def test_model_role_plan_requires_deterministic_promotion_before_ready(tmp_path: Path) -> None:
    out = tmp_path / "blocked"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--board", "solver",
            "--allow-model",
            "--output-dir", str(out),
        ],
        cwd=BUILD_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
    )
    assert proc.returncode != 0
    assert "--deterministic-summary is required" in proc.stderr
    assert not (out / "FINALIZED.json").exists()
