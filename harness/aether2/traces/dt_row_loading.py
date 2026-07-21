"""Row parsing and source discovery for decision trace extraction."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Sequence

_DIRECT_ROW_FILENAMES = (
    "result_rows.jsonl",
    "row.json",
)
_COMBINED_ROW_FILENAMES = (
    "attempt1_rows_combined.jsonl",
    "attempt2_rows_combined.jsonl",
)

_ATTEMPT_FROM_TEXT_RE = re.compile(r"(?:^|/)(attempt[_-]?(\d+))(?:/|$)")
_COMBINED_MARKER_RE = re.compile(r"^### FILE: (.+)$", flags=re.MULTILINE)


def _resolved(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _read_text(path: Path) -> tuple[str | None, list[str]]:
    try:
        return path.read_text(encoding="utf-8"), []
    except OSError as exc:
        return None, [f"{path}: {type(exc).__name__}: {exc}"]


def _loads_json(text: str, *, source_ref: str) -> tuple[Any | None, list[str]]:
    try:
        return json.loads(text), []
    except json.JSONDecodeError as exc:
        return None, [f"{source_ref}: JSONDecodeError: {exc}"]


def _extract_attempt_ref(row: dict[str, Any], source_ref: str) -> tuple[str | None, str]:
    for key in ("attempt", "attempt_id", "attempt_index", "attempt_number"):
        value = row.get(key)
        if value is None or value == "":
            continue
        return str(value), f"row_field:{key}"
    match = _ATTEMPT_FROM_TEXT_RE.search(source_ref)
    if match:
        return match.group(2), "source_path"
    return None, "absent"


def _infer_run_ref(input_ref: Path, row: dict[str, Any] | None, source_row_ref: str | None = None) -> dict[str, str]:
    run_id = ""
    run_path = ""

    if isinstance(row, dict):
        raw_run_id = row.get("run_id") or row.get("eval_id")
        if isinstance(raw_run_id, str) and raw_run_id:
            run_id = raw_run_id
        raw_run_dir = row.get("run_dir")
        if isinstance(raw_run_dir, str) and raw_run_dir:
            run_path = _resolved(raw_run_dir)
        raw_workspace = row.get("workspace")
        if not run_path and isinstance(raw_workspace, str) and raw_workspace:
            run_path = _resolved(Path(raw_workspace).parent if raw_workspace.endswith("/workspace") else raw_workspace)

    if not run_path and source_row_ref:
        marker_match = _ATTEMPT_FROM_TEXT_RE.search(source_row_ref)
        if marker_match:
            marker_path = Path(source_row_ref)
            run_path = _resolved(marker_path.parent.parent)

    if not run_path:
        if input_ref.is_file() and input_ref.name == "result_rows.jsonl":
            run_path = _resolved(input_ref.parent)
        elif input_ref.is_file() and input_ref.name.endswith("_rows_combined.jsonl"):
            run_path = _resolved(input_ref.parent.parent if input_ref.parent.name == "rows" else input_ref.parent)
        elif input_ref.is_file() and input_ref.name == "row.json":
            run_path = _resolved(input_ref.parent.parent)
        elif input_ref.is_dir():
            run_path = _resolved(input_ref)
        else:
            run_path = _resolved(input_ref.parent)

    if not run_id:
        run_id = Path(run_path).name or Path(input_ref).name

    if source_row_ref and not run_id:
        run_id = Path(source_row_ref).parent.name or Path(source_row_ref).name

    return {"run_id": run_id, "run_path": run_path}


def _parse_combined_rows(text: str, *, source_file: Path) -> list[dict[str, Any]]:
    parts = _COMBINED_MARKER_RE.split(text)
    if len(parts) <= 1:
        return []

    records: list[dict[str, Any]] = []
    iterator = iter(parts[1:])
    for marker, body in zip(iterator, iterator):
        marker = marker.strip()
        body = body.strip()
        row, issues = _loads_json(body, source_ref=marker)
        records.append(
            {
                "row": row if isinstance(row, dict) else None,
                "source_input_ref": _resolved(source_file),
                "source_row_ref": marker,
                "source_kind": "combined_row_file",
                "source_index": len(records),
                "parse_issues": issues,
            }
        )
    return records


def _parse_jsonl_rows(text: str, *, source_file: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        row, issues = _loads_json(stripped, source_ref=f"{source_file}#line:{line_no}")
        records.append(
            {
                "row": row if isinstance(row, dict) else None,
                "source_input_ref": _resolved(source_file),
                "source_row_ref": f"{_resolved(source_file)}#line:{line_no}",
                "source_kind": "result_rows_jsonl",
                "source_index": line_no,
                "parse_issues": issues,
            }
        )
    return records


def _parse_single_row(path: Path) -> list[dict[str, Any]]:
    text, issues = _read_text(path)
    if text is None:
        return [
            {
                "row": None,
                "source_input_ref": _resolved(path),
                "source_row_ref": _resolved(path),
                "source_kind": "row_json",
                "source_index": 0,
                "parse_issues": issues,
            }
        ]
    row, parse_issues = _loads_json(text, source_ref=str(path))
    return [
        {
            "row": row if isinstance(row, dict) else None,
            "source_input_ref": _resolved(path),
            "source_row_ref": _resolved(path),
            "source_kind": "row_json",
            "source_index": 0,
            "parse_issues": issues + parse_issues,
        }
    ]


def _direct_row_sources_for_dir(root: Path) -> list[Path]:
    candidates: list[Path] = []
    if root.name == "rows":
        for filename in _COMBINED_ROW_FILENAMES:
            candidate = root / filename
            if candidate.exists():
                candidates.append(candidate)
        return candidates

    for filename in _DIRECT_ROW_FILENAMES:
        candidate = root / filename
        if candidate.exists():
            candidates.append(candidate)
    for filename in _COMBINED_ROW_FILENAMES:
        candidate = root / "rows" / filename
        if candidate.exists():
            candidates.append(candidate)
    return candidates


def _load_row_records(inputs: Sequence[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for input_path in inputs:
        if input_path.is_dir():
            candidate_files = _direct_row_sources_for_dir(input_path)
            if not candidate_files:
                records.append(
                    {
                        "row": None,
                        "source_input_ref": _resolved(input_path),
                        "source_row_ref": _resolved(input_path),
                        "source_kind": "missing_index",
                        "source_index": 0,
                        "parse_issues": [f"{input_path}: no direct row index file found"],
                    }
                )
                continue
            for candidate in candidate_files:
                records.extend(_load_row_records([candidate]))
            continue

        text, issues = _read_text(input_path)
        if text is None:
            records.append(
                {
                    "row": None,
                    "source_input_ref": _resolved(input_path),
                    "source_row_ref": _resolved(input_path),
                    "source_kind": "missing_file",
                    "source_index": 0,
                    "parse_issues": issues,
                }
            )
            continue

        if input_path.name.endswith(".jsonl"):
            if "### FILE:" in text:
                parsed = _parse_combined_rows(text, source_file=input_path)
                if parsed:
                    records.extend(parsed)
                    continue
            records.extend(_parse_jsonl_rows(text, source_file=input_path))
            continue

        if input_path.name.endswith(".json"):
            records.extend(_parse_single_row(input_path))
            continue

        row, parse_issues = _loads_json(text, source_ref=str(input_path))
        records.append(
            {
                "row": row if isinstance(row, dict) else None,
                "source_input_ref": _resolved(input_path),
                "source_row_ref": _resolved(input_path),
                "source_kind": "unknown",
                "source_index": 0,
                "parse_issues": issues + parse_issues,
            }
        )
    return records
