"""Verifier/grader alignment board for Aether result rows."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .classifier import reconcile_grader_alignment


@dataclass(frozen=True)
class AlignmentBoard:
    rows: list[dict[str, Any]]
    confusion_matrix: dict[str, dict[str, int]]
    status_counts: dict[str, int]
    invalid_counts: dict[str, int]
    source_files: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_count": len(self.rows),
            "source_files": self.source_files,
            "confusion_matrix": self.confusion_matrix,
            "verifier_alignment_status_counts": self.status_counts,
            "invalid_row_counts": self.invalid_counts,
            "rows": self.rows,
        }


def load_result_rows(paths: Iterable[str | Path]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    sources: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        loaded = _extract_rows(payload)
        for row in loaded:
            normalized = dict(row)
            normalized.setdefault("source_file", str(path))
            rows.append(normalized)
        sources.append(str(path))
    return rows, sources


def build_alignment_board(rows: Iterable[Mapping[str, Any]], *, source_files: Iterable[str] = ()) -> AlignmentBoard:
    normalized_rows = [_normalize_row(row) for row in rows]
    matrix = {
        "clean": {"pass": 0, "fail": 0, "unavailable": 0},
        "not_clean": {"pass": 0, "fail": 0, "unavailable": 0},
        "invalid": {"pass": 0, "fail": 0, "unavailable": 0},
    }
    status_counts: Counter[str] = Counter()
    invalid_counts: Counter[str] = Counter()
    for row in normalized_rows:
        verifier_bucket = row["verifier_bucket"]
        grader_bucket = row["official_grader_status"]
        matrix[verifier_bucket][grader_bucket] += 1
        status_counts[row["verifier_alignment_status"]] += 1
        if verifier_bucket == "invalid" or grader_bucket == "unavailable":
            invalid_counts[row["invalid_reason"]] += 1
    return AlignmentBoard(
        rows=normalized_rows,
        confusion_matrix=matrix,
        status_counts=dict(sorted(status_counts.items())),
        invalid_counts=dict(sorted(invalid_counts.items())),
        source_files=list(source_files),
    )


def write_alignment_report(board: AlignmentBoard, out_json: str | Path, out_md: str | Path | None = None) -> None:
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(board.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    if out_md is not None:
        out_md = Path(out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_render_markdown(board), encoding="utf-8")


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("rows", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, Mapping)]
        return [dict(payload)]
    raise ValueError("result payload must be a row, list of rows, or object with rows/results")


def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    reward = _reward_value(row.get("reward"))
    grader_error = row.get("grader_error")
    if not grader_error and row.get("status") == "grader_error":
        grader_error = row.get("error")
    kernel_status = str(row.get("kernel_status") or row.get("status") or "unknown")
    verifier_verdict = str(row.get("model_verifier_final_verdict") or "").strip()
    alignment = reconcile_grader_alignment(
        reward=reward,
        grader_error=str(grader_error) if grader_error else None,
        kernel_status=kernel_status,
        verifier_verdict=verifier_verdict or None,
    )
    official_status = _official_status(row.get("official_grader_status") or alignment["official_grader_status"])
    internal_status = str(row.get("internal_completion_status") or alignment["internal_completion_status"])
    alignment_status = str(row.get("verifier_alignment_status") or alignment["verifier_alignment_status"])
    invalid_reason = _invalid_reason(row, official_status, internal_status)
    normalized = {
        "task": row.get("task", ""),
        "source_file": row.get("source_file", ""),
        "reward": reward,
        "status": row.get("status", ""),
        "kernel_status": kernel_status,
        "model_verifier_final_verdict": verifier_verdict,
        "official_grader_status": official_status,
        "internal_completion_status": internal_status,
        "verifier_alignment_status": alignment_status,
        "verifier_bucket": _verifier_bucket(internal_status, official_status, invalid_reason),
        "invalid_reason": invalid_reason,
        "classifier_label": row.get("classifier_label", ""),
        "classifier_detail": row.get("classifier_detail", ""),
        "trace_path": row.get("trace_path", ""),
        "trace_write_error": row.get("trace_write_error", ""),
        "grader_error": row.get("grader_error", ""),
    }
    return normalized


def _reward_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _official_status(value: Any) -> str:
    status = str(value or "unavailable")
    if status in {"pass", "fail", "unavailable"}:
        return status
    return "unavailable"


def _invalid_reason(row: Mapping[str, Any], official_status: str, internal_status: str) -> str:
    if official_status == "unavailable":
        if row.get("grader_error"):
            return "grader_unavailable"
        if row.get("error"):
            return "runner_error"
        return "official_grader_unavailable"
    if internal_status in {"config_invalid", "timeout", "error"}:
        return internal_status
    return ""


def _verifier_bucket(internal_status: str, official_status: str, invalid_reason: str) -> str:
    if invalid_reason and official_status == "unavailable":
        return "invalid"
    if internal_status == "completed":
        return "clean"
    return "not_clean"


def _render_markdown(board: AlignmentBoard) -> str:
    lines = [
        "# Verifier/Grader Alignment Board",
        "",
        f"Rows: {len(board.rows)}",
        "",
        "## Confusion Matrix",
        "",
        "| Verifier bucket | Grader pass | Grader fail | Grader unavailable |",
        "| --- | ---: | ---: | ---: |",
    ]
    for bucket in ("clean", "not_clean", "invalid"):
        counts = board.confusion_matrix[bucket]
        lines.append(f"| {bucket} | {counts['pass']} | {counts['fail']} | {counts['unavailable']} |")
    lines.extend(["", "## Alignment Status Counts", ""])
    for status, count in board.status_counts.items():
        lines.append(f"- {status}: {count}")
    if board.invalid_counts:
        lines.extend(["", "## Invalid Row Counts", ""])
        for reason, count in board.invalid_counts.items():
            lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Rows", ""])
    lines.append("| Task | Verifier | Grader | Alignment | Status | Trace |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in board.rows:
        trace = row.get("trace_path") or row.get("trace_write_error") or ""
        lines.append(
            f"| {_markdown_cell(row['task'])} | {_markdown_cell(row['verifier_bucket'])} | "
            f"{_markdown_cell(row['official_grader_status'])} | "
            f"{_markdown_cell(row['verifier_alignment_status'])} | "
            f"{_markdown_cell(row['kernel_status'])} | {_markdown_cell(trace)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
