from __future__ import annotations

import json
from pathlib import Path


BUILD_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = BUILD_ROOT / "evals" / "manifest.v1.json"
BOARD = BUILD_ROOT / "evals" / "official_task_board.v1.json"


def test_eval_manifest_has_unique_cases_and_existing_targets() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = payload["cases"]
    case_ids = [str(case["id"]) for case in cases]
    assert len(case_ids) == len(set(case_ids))
    assert payload["schema"] == "aether.harness_eval_manifest.v1"
    for case in cases:
        assert case["covers"], case["id"]
        if case["kind"] in {"pytest", "pytest_collect"}:
            assert case.get("targets"), case["id"]
            for target in case["targets"]:
                assert (BUILD_ROOT / target).exists(), (case["id"], target)


def test_every_scorecard_id_has_an_eval_owner() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    declared = set(payload["scorecard_ids"])
    covered = {
        str(item)
        for case in payload["cases"]
        for item in case.get("covers", [])
    }
    assert declared == covered


def test_model_and_vm_cases_are_explicitly_gated() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    gated = [case for case in payload["cases"] if case["kind"] == "plan"]
    assert gated
    assert {case["gate"] for case in gated} == {"model", "vm"}
    assert all(case["required"] is False for case in gated)


def test_official_task_board_is_eval_only_and_complete() -> None:
    board = json.loads(BOARD.read_text(encoding="utf-8"))
    assert board["source"]["classification_scope"].startswith("evaluation-only")
    assert board["source"]["task_count"] == 90
    assert len(board["full_board"]) == 90
    assert len(set(board["full_board"])) == 90
    smoke_ids = [row["task_id"] for row in board["smoke_board"]]
    assert len(smoke_ids) == 24
    assert set(smoke_ids) <= set(board["full_board"])
