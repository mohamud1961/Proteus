from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_ROOT = REPO_ROOT / "eval_suite" / "families" / "filesystem" / "public_manifest_repair_smoke"
TASK_PACK_PATH = PACK_ROOT / "task_pack.json"
BOARD_PATH = REPO_ROOT / "eval_suite" / "boards" / "public_manifest_repair_smoke_v1.json"
SCOREBOARD_PATH = REPO_ROOT / "eval_suite" / "scoreboards" / "public_manifest_repair_smoke_v1.example.scoreboard.json"
SMOKE_SCRIPT_PATH = REPO_ROOT / "scripts" / "public_manifest_repair_smoke.sh"
GRADER_PATH = PACK_ROOT / "grader.py"


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_manifest_repair_task_pack_is_valid_and_public_safe():
    task_pack = json.loads(TASK_PACK_PATH.read_text(encoding="utf-8"))

    assert task_pack["task_id"] == "public_manifest_repair_smoke_v1"
    assert task_pack["contamination_policy"]["status"] == "clean"
    assert task_pack["contamination_policy"]["source"] == "original_synthetic"
    assert task_pack["surface_type"] == "verifier_repair"
    assert "release workspace" in task_pack["task_prompt"].lower()
    assert "benchmark" not in task_pack["task_prompt"].lower()


def test_public_manifest_repair_grader_handles_pass_and_fail(tmp_path):
    grader = _load_module(GRADER_PATH, "public_manifest_repair_smoke_grader_test")
    fixture_root = PACK_ROOT / "fixture"

    pass_root = tmp_path / "pass"
    fail_root = tmp_path / "fail"
    for src, dst in (
        (fixture_root / "workspace", fail_root / "workspace"),
        (fixture_root / "reference", fail_root / "reference"),
        (fixture_root / "workspace", pass_root / "workspace"),
        (fixture_root / "reference", pass_root / "reference"),
    ):
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            raise AssertionError(f"unexpected preexisting test path: {dst}")
        import shutil

        shutil.copytree(src, dst)

    reference_root = pass_root / "reference"
    pass_workspace_release = pass_root / "workspace" / "release"
    pass_workspace_release.joinpath("manifest.json").write_text(
        (reference_root / "manifest.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    pass_workspace_release.joinpath("summary.txt").write_text(
        (reference_root / "summary.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    pass_workspace_release.joinpath("checksum.txt").write_text(
        (reference_root / "checksum.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    pass_grade = grader.grade_workspace(workspace_root=pass_root / "workspace", reference_root=reference_root)
    fail_grade = grader.grade_workspace(workspace_root=fail_root / "workspace", reference_root=fail_root / "reference")

    assert pass_grade["verdict"] == "pass"
    assert pass_grade["score"] == 1.0
    assert pass_grade["reason_codes"] == []
    assert fail_grade["verdict"] == "fail"
    assert fail_grade["score"] == 0.0
    assert {"manifest_mismatch", "summary_mismatch", "checksum_file_mismatch", "checksum_not_derived_from_manifest"} <= set(
        fail_grade["reason_codes"]
    )


def test_public_manifest_repair_board_points_to_pack_and_grader():
    board = json.loads(BOARD_PATH.read_text(encoding="utf-8"))

    assert board["board_id"] == "public_manifest_repair_smoke_v1"
    assert board["board_type"] == "public_smoke_example"
    assert isinstance(board["task_pack_ref"], str)
    assert isinstance(board["grader_ref"], str)
    assert isinstance(board["fixture_root_ref"], str)
    assert TASK_PACK_PATH.exists()
    assert GRADER_PATH.exists()
    assert board["task_pack_ref"].endswith("public_manifest_repair_smoke/task_pack.json")
    assert board["grader_ref"].endswith("public_manifest_repair_smoke/grader.py")


def test_public_manifest_repair_example_scoreboard_matches_aggregated_rows():
    payload = json.loads(SCOREBOARD_PATH.read_text(encoding="utf-8"))

    assert payload["example_only"] is True
    assert payload["scope_label"] == "public_smoke_example_not_benchmark_evidence"
    assert payload["scoreboard"]["totals"] == {"pass": 1, "fail": 1, "invalid": 0, "total": 2}


def test_public_manifest_repair_smoke_script_writes_example_bundle(tmp_path):
    completed = subprocess.run(
        ["bash", str(SMOKE_SCRIPT_PATH), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    output_path = Path(completed.stdout.strip().splitlines()[-1])
    output = json.loads(output_path.read_text(encoding="utf-8"))
    generated = json.loads((tmp_path / "public_manifest_repair_smoke_example.json").read_text(encoding="utf-8"))

    assert output["board_id"] == "public_manifest_repair_smoke_v1"
    assert generated["scoreboard"]["totals"] == {"pass": 1, "fail": 1, "invalid": 0, "total": 2}
