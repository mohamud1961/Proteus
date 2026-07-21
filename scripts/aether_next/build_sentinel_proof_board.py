#!/usr/bin/env python3
"""Build a proof-board summary from Aether result rows/traces.

This is a post-run audit helper. It does not run models or graders. It reads
result JSON rows (and optional trace_path values inside them) and emits the
metrics needed to judge the next 5-task sentinel:

- prompt/protocol parse health
- step efficiency
- submit/reviewer loop health
- reviewer inspection evidence
- official/internal alignment
- repeated-action / low-information loop indicators
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

SENTINEL_TASKS = (
    "log-summary-date-ranges",
    "video-processing",
    "gcode-to-text",
    "kv-store-grpc",
    "code-from-image",
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _trace_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    trace_path = row.get("trace_path")
    if not trace_path:
        return {}
    path = Path(str(trace_path))
    if not path.exists():
        return {}
    data = _load_json(path)
    return data.get("trace", data) if isinstance(data, dict) else {}


def _trace_counts(trace: Mapping[str, Any]) -> dict[str, Any]:
    steps = trace.get("steps", []) if isinstance(trace, Mapping) else []
    submit_count = 0
    solver_parse_errors = 0
    repeated_submit_skips = 0
    verifier_inspections = 0
    for step in steps or []:
        turn = step.get("turn", {}) if isinstance(step, Mapping) else {}
        if turn.get("kind") == "submit_outcome":
            submit_count += 1
        for obs in step.get("observations", []) or []:
            kind = str(obs.get("kind", ""))
            if kind == "solver_parse_error":
                solver_parse_errors += 1
            if kind == "model_verifier_inspection":
                verifier_inspections += 1
            if kind == "model_verifier_skipped" and "active" in str(obs.get("summary", "")).lower():
                repeated_submit_skips += 1
    return {
        "trace_steps": len(steps or []),
        "trace_submit_count": submit_count,
        "trace_solver_parse_errors": solver_parse_errors,
        "trace_verifier_inspections": verifier_inspections,
        "trace_submit_without_new_evidence": repeated_submit_skips,
    }


def _receipt_counts(row: Mapping[str, Any]) -> dict[str, Any]:
    receipts = row.get("receipt_summary", [])
    if not isinstance(receipts, list):
        receipts = []
    counts: dict[str, int] = {}
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        kind = str(receipt.get("kind", ""))
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return {
        "receipt_model_verifier_result": counts.get("model_verifier_result", 0),
        "receipt_model_verifier_evidence": counts.get("model_verifier_evidence", 0),
        "receipt_model_verifier_inspection": counts.get("model_verifier_inspection", 0),
        "receipt_solver_parse_error": counts.get("solver_parse_error", 0),
        "receipt_model_verifier_skipped": counts.get("model_verifier_skipped", 0),
    }


def build_rows_from_file(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    if isinstance(data, list):
        rows_data = data
    else:
        rows_data = [data]

    rows = []
    for row in rows_data:
        trace = _trace_from_row(row)
        metrics = row.get("run_metrics", {}) if isinstance(row.get("run_metrics"), Mapping) else {}
        out = {
            "source": str(path),
            "task": row.get("task", path.stem),
            "reward": row.get("reward"),
            "status": row.get("status"),
            "kernel_status": row.get("kernel_status", row.get("status")),
            "model_verifier_final_verdict": row.get("model_verifier_final_verdict", ""),
            "classifier_label": row.get("classifier_label", ""),
            "step": row.get("step", 0),
            "expected_steps": row.get("expected_steps", 0),
            "step_efficiency": row.get("step_efficiency"),
            "architect_defect": row.get("architect_defect", False),
            "model_parse_error_count": len(row.get("model_parse_errors", []) or []),
            "metric_solver_parse_error_count": metrics.get("solver_parse_error_count"),
            "metric_tool_schema_error_count": metrics.get("tool_schema_error_count"),
            "metric_submit_without_new_evidence_count": metrics.get("submit_without_new_evidence_count"),
            "metric_repeated_command_count": metrics.get("repeated_command_count"),
            "metric_repeated_write_count": metrics.get("repeated_write_count"),
            "alignment": row.get("alignment", row.get("grader_internal_alignment", "")),
            "trace_path": row.get("trace_path", ""),
        }
        out.update(_receipt_counts(row))
        out.update(_trace_counts(trace))
        rows.append(out)
    return rows


def write_outputs(rows: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sentinel_proof_board.json").write_text(json.dumps({"rows": rows}, indent=2, sort_keys=True), encoding="utf-8")
    fieldnames = sorted({key for row in rows for key in row})
    with (out_dir / "sentinel_proof_board.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Sentinel Proof Board", "", f"Rows: {len(rows)}", "", "| Task | Reward | Status | Steps | Verifier verdict | Parse errors | Submit no-evidence | Inspections |", "|---|---:|---|---:|---|---:|---:|---:|"]
    for row in rows:
        lines.append(
            f"| {row.get('task')} | {row.get('reward')} | {row.get('status')} | {row.get('step')} | "
            f"{row.get('model_verifier_final_verdict')} | {row.get('model_parse_error_count')} | "
            f"{row.get('metric_submit_without_new_evidence_count') or row.get('trace_submit_without_new_evidence')} | "
            f"{row.get('receipt_model_verifier_inspection') or row.get('trace_verifier_inspections')} |"
        )
    (out_dir / "SENTINEL_PROOF_BOARD.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", type=Path, help="result JSON rows from a sentinel run")
    parser.add_argument("--out-dir", type=Path, default=Path("sentinel_proof_board"))
    args = parser.parse_args()
    rows = []
    for path in args.results:
        rows.extend(build_rows_from_file(path))
    write_outputs(rows, args.out_dir)
    print(json.dumps({"rows": len(rows), "out_dir": str(args.out_dir)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
