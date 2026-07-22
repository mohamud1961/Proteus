from __future__ import annotations

import json

from aether_next.alignment_board import build_alignment_board, load_result_rows, write_alignment_report


def test_alignment_board_builds_confusion_matrix_and_invalid_counts() -> None:
    rows = [
        {"task": "true-clean", "reward": 1.0, "kernel_status": "completed"},
        {"task": "false-clean", "reward": 0.0, "kernel_status": "completed"},
        {"task": "miss", "reward": 1.0, "kernel_status": "incomplete"},
        {"task": "true-block", "reward": 0.0, "kernel_status": "incomplete"},
        {"task": "invalid", "reward": 0.0, "status": "grader_error", "grader_error": "reward missing"},
    ]

    board = build_alignment_board(rows, source_files=["rows.json"])

    assert board.confusion_matrix["clean"] == {"pass": 1, "fail": 1, "unavailable": 0}
    assert board.confusion_matrix["not_clean"] == {"pass": 1, "fail": 1, "unavailable": 0}
    assert board.confusion_matrix["invalid"] == {"pass": 0, "fail": 0, "unavailable": 1}
    assert board.status_counts["verifier_false_clean"] == 1
    assert board.status_counts["verifier_completion_miss"] == 1
    assert board.invalid_counts["grader_unavailable"] == 1


def test_alignment_board_prefers_explicit_verifier_verdict_when_present() -> None:
    rows = [
        {
            "task": "explicit-verifier-row",
            "reward": 0.0,
            "kernel_status": "completed",
            "model_verifier_final_verdict": "needs_repair",
        }
    ]

    board = build_alignment_board(rows, source_files=["rows.json"])

    assert board.rows[0]["model_verifier_final_verdict"] == "needs_repair"
    assert board.rows[0]["internal_completion_status"] == "incomplete"
    assert board.rows[0]["verifier_bucket"] == "not_clean"
    assert board.rows[0]["verifier_alignment_status"] == "aligned"


def test_alignment_board_loads_and_writes_reports(tmp_path) -> None:
    result_path = tmp_path / "results.json"
    result_path.write_text(json.dumps([{"task": "t", "reward": 1.0, "kernel_status": "completed"}]))
    rows, sources = load_result_rows([result_path])
    board = build_alignment_board(rows, source_files=sources)
    out_json = tmp_path / "board.json"
    out_md = tmp_path / "board.md"

    write_alignment_report(board, out_json, out_md)

    payload = json.loads(out_json.read_text())
    assert payload["row_count"] == 1
    assert payload["confusion_matrix"]["clean"]["pass"] == 1
    assert "Verifier/Grader Alignment Board" in out_md.read_text()
