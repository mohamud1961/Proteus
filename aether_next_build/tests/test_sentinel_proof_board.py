from __future__ import annotations

import json
from pathlib import Path

from scripts.build_sentinel_proof_board import build_rows_from_file, write_outputs


def _write_fixture(tmp_path: Path) -> Path:
    trace = {
        "steps": [
            {"step": 0, "turn": {"kind": "submit_outcome"}, "observations": [
                {"kind": "solver_parse_error", "summary": "bad"},
                {"kind": "model_verifier_inspection", "summary": "read_file"},
            ]},
            {"step": 1, "turn": {"kind": "submit_outcome"}, "observations": [
                {"kind": "model_verifier_skipped", "summary": "active completion findings require evidence"},
            ]},
        ]
    }
    trace_path = tmp_path / "task.trace.json"
    trace_path.write_text(json.dumps({"trace": trace}), encoding="utf-8")
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps([{
        "task": "log-summary-date-ranges",
        "reward": 1.0,
        "status": "completed",
        "step": 3,
        "trace_path": str(trace_path),
        "model_parse_errors": [{"error": "bad"}],
        "run_metrics": {"solver_parse_error_count": 1, "submit_without_new_evidence_count": 1},
        "receipt_summary": [
            {"kind": "model_verifier_result"},
            {"kind": "model_verifier_evidence"},
        ],
    }]), encoding="utf-8")
    return results_path


def test_sentinel_proof_board_uses_row_and_trace_metrics(tmp_path) -> None:
    rows = build_rows_from_file(_write_fixture(tmp_path))
    assert len(rows) == 1
    built = rows[0]
    assert built["task"] == "log-summary-date-ranges"
    assert built["trace_submit_count"] == 2
    assert built["trace_solver_parse_errors"] == 1
    assert built["trace_verifier_inspections"] == 1
    assert built["trace_submit_without_new_evidence"] == 1
    assert built["model_parse_error_count"] == 1
    assert built["metric_submit_without_new_evidence_count"] == 1
    assert built["receipt_model_verifier_evidence"] == 1
    assert built["receipt_model_verifier_result"] == 1

    out_dir = tmp_path / "board"
    write_outputs(rows, out_dir)
    assert (out_dir / "sentinel_proof_board.json").exists()
    assert (out_dir / "sentinel_proof_board.csv").exists()
    board_md = (out_dir / "SENTINEL_PROOF_BOARD.md").read_text(encoding="utf-8")
    assert "log-summary-date-ranges" in board_md
    assert "Rows: 1" in board_md


def test_sentinel_proof_board_missing_trace_is_tolerated(tmp_path) -> None:
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps([{
        "task": "kv-store-grpc",
        "reward": 0.0,
        "status": "incomplete",
        "step": 30,
        "trace_path": str(tmp_path / "does-not-exist.trace.json"),
    }]), encoding="utf-8")
    rows = build_rows_from_file(results_path)
    assert rows[0]["task"] == "kv-store-grpc"
    assert rows[0]["trace_steps"] == 0
