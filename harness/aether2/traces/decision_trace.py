#!/usr/bin/env python3
"""Observable decision-trace extraction and receipt bundling for HarnessEng.

This is analysis-only. It reconstructs visible action/observation chains from
result rows and actual receipts, but it is not private chain-of-thought and it
does not infer hidden intent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from harness.aether2.traces.dt_row_loading import (
    _resolved,
    _extract_attempt_ref,
    _infer_run_ref,
    _load_row_records,
)
from harness.aether2.traces.dt_receipts import (
    summarize_text,
    _short_json,
    _dedupe_dicts,
    _extract_embedded_tool_invocations,
    _load_json_receipt,
    _extract_external_receipt_paths,
    _summarize_json_receipt,
    _collect_passthrough_metadata,
)
from harness.aether2.traces.dt_event_extraction import (
    _pair_embedded_invocations,
    _pair_route_trace_events,
    _extract_reasoning_trace_events,
    _extract_verification_gaps_from_row,
    _extract_verification_gaps_from_route_receipts,
)

NON_COT_NOTE = (
    "This is not private chain-of-thought. It is a post-run analysis artifact "
    "derived from visible actions, receipts, and verifier gaps."
)

__all__ = [
    "NON_COT_NOTE",
    "build_and_write_bundle",
    "build_parser",
    "collect_decision_trace_bundle",
    "main",
    "render_summary",
    "summarize_text",
]


def _build_row_bundle(record: dict[str, Any]) -> dict[str, Any]:
    row = record.get("row") if isinstance(record.get("row"), dict) else None
    source_row_ref = str(record.get("source_row_ref") or record.get("source_input_ref") or "")
    source_input_ref = str(record.get("source_input_ref") or source_row_ref)
    source_run_ref = _infer_run_ref(Path(source_input_ref), row, source_row_ref)
    attempt_ref, attempt_provenance = _extract_attempt_ref(row or {}, source_row_ref)
    provenance = {
        "source_input_ref": source_input_ref,
        "source_row_ref": source_row_ref,
        "source_run_ref": source_run_ref,
        "attempt_ref": attempt_ref,
        "attempt_provenance": attempt_provenance,
        "source_kind": record.get("source_kind"),
    }

    parse_issues = list(record.get("parse_issues") or [])
    row_status = (row or {}).get("row_status") or (row or {}).get("verdict") or (row or {}).get("status")
    verifier_exit_code = (row or {}).get("verifier_exit_code")
    task_id = (row or {}).get("task_id") or (row or {}).get("task_pack_id")
    run_id = (row or {}).get("run_id")
    eval_id = (row or {}).get("eval_id")

    embedded_invocations = _extract_embedded_tool_invocations(row)
    primary_mode = "embedded_tool_invocations" if embedded_invocations else "route_trace_events"
    receipt_bundle: list[dict[str, Any]] = []
    primary_events: list[dict[str, Any]] = []
    route_receipts: list[dict[str, Any]] = []
    reasoning_trace_payload: dict[str, Any] | None = None
    reasoning_trace_path: Path | None = None

    external_receipt_paths = _extract_external_receipt_paths(
        row,
        {"receipt_bundle": []},
        source_input_ref=source_input_ref,
        source_row_ref=source_row_ref,
    )
    external_receipts: list[dict[str, Any]] = []
    external_parse_issues: list[str] = []
    route_trace_source_path: Path | None = None
    verifier_context_invocations: list[dict[str, Any]] = []
    passthrough_metadata = _collect_passthrough_metadata(row)

    for path in external_receipt_paths:
        if not path.exists():
            external_parse_issues.append(f"{path}: missing receipt file")
            continue
        if path.name == "run_events.jsonl":
            route_trace_source_path = path
            from harness.aether2.traces.dt_row_loading import _read_text, _loads_json
            text, read_issues = _read_text(path)
            external_parse_issues.extend(read_issues)
            if text is None:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                payload, line_issues = _loads_json(stripped, source_ref=f"{path}#line:{line_no}")
                external_parse_issues.extend(line_issues)
                if isinstance(payload, dict):
                    route_receipts.append(payload)
                    from harness.aether2.traces.dt_event_extraction import _summarize_route_receipt
                    external_receipts.append(_summarize_route_receipt(payload, source_path=path))
            continue

        payload, receipt_issues = _load_json_receipt(path)
        external_parse_issues.extend(receipt_issues)
        if isinstance(payload, dict):
            external_receipts.append(_summarize_json_receipt(path, payload))
            for field, value in _collect_passthrough_metadata(payload).items():
                passthrough_metadata.setdefault(field, value)
            if path.name == "reasoning_trace.json":
                reasoning_trace_payload = payload
                reasoning_trace_path = path
            if path.parent.name == "verifier_context":
                invocations = payload.get("tool_invocations")
                if isinstance(invocations, list):
                    verifier_context_invocations.extend(item for item in invocations if isinstance(item, dict))

    row_gaps = _extract_verification_gaps_from_row(row)

    if embedded_invocations:
        receipt_bundle, primary_events = _pair_embedded_invocations(
            embedded_invocations,
            provenance=provenance,
            row_gaps=row_gaps,
        )
    elif reasoning_trace_payload is not None and reasoning_trace_path is not None:
        receipt_bundle, primary_events, reasoning_parse_issues = _extract_reasoning_trace_events(
            reasoning_trace_payload,
            trace_path=reasoning_trace_path,
            provenance=provenance,
            row_gaps=row_gaps,
        )
        external_parse_issues.extend(reasoning_parse_issues)
        primary_mode = "reasoning_trace_steps"
    elif route_receipts:
        receipt_bundle, primary_events = _pair_route_trace_events(
            route_receipts,
            source_path=route_trace_source_path or Path(source_row_ref),
            provenance=provenance,
            row_gaps=row_gaps,
        )
        row_gaps = _dedupe_dicts(row_gaps + _extract_verification_gaps_from_route_receipts(route_receipts))
    elif verifier_context_invocations:
        receipt_bundle, primary_events = _pair_embedded_invocations(
            verifier_context_invocations,
            provenance=provenance,
            row_gaps=row_gaps,
        )
        primary_mode = "verifier_context_tool_invocations"
    else:
        receipt_bundle = []
        primary_events = []

    if external_receipts:
        receipt_bundle.extend(external_receipts)

    if route_receipts:
        row_gaps = _dedupe_dicts(row_gaps + _extract_verification_gaps_from_route_receipts(route_receipts))

    if not row_gaps and row_status not in (None, "pass", "passed", "ok"):
        row_gaps = [
            {
                "gap_type": "row_status",
                "row_status": row_status,
                "verifier_exit_code": verifier_exit_code,
            }
        ]

    if not row_gaps and verifier_exit_code not in (None, 0):
        row_gaps = [
            {
                "gap_type": "verifier_exit_code",
                "verifier_exit_code": verifier_exit_code,
            }
        ]

    if primary_events:
        primary_events[-1]["unresolved_verifier_gaps"] = row_gaps

    bundle = {
        "bundle_type": "observable_decision_trace",
        "analysis_scope": "post_run_analysis_only",
        "non_cot_note": NON_COT_NOTE,
        "source_input_ref": source_input_ref,
        "source_run_ref": source_run_ref,
        "source_row_ref": source_row_ref,
        "attempt_ref": attempt_ref,
        "attempt_provenance": attempt_provenance,
        "source_kind": record.get("source_kind"),
        "task_id": task_id,
        "run_id": run_id,
        "eval_id": eval_id,
        "row_status": row_status,
        "verifier_exit_code": verifier_exit_code,
        **passthrough_metadata,
        "primary_receipt_mode": primary_mode,
        "receipt_bundle": _dedupe_dicts(receipt_bundle),
        "events": primary_events,
        "unresolved_verifier_gaps": row_gaps,
        "parse_issues": _dedupe_dicts([{"parse_issue": issue} for issue in parse_issues + external_parse_issues]),
    }
    return bundle


def collect_decision_trace_bundle(inputs: Sequence[str | Path]) -> dict[str, Any]:
    input_paths = [Path(item) for item in inputs]
    records = _load_row_records(input_paths)
    row_bundles = [_build_row_bundle(record) for record in records]
    summary = _build_summary(row_bundles)
    return {
        "analysis_scope": "post_run_analysis_only",
        "non_cot_note": NON_COT_NOTE,
        "source_inputs": [_resolved(path) for path in input_paths],
        "rows": row_bundles,
        "summary": summary,
    }


def _build_summary(row_bundles: list[dict[str, Any]]) -> dict[str, Any]:
    row_count = len(row_bundles)
    event_count = sum(len(row.get("events") or []) for row in row_bundles)
    receipt_count = sum(len(row.get("receipt_bundle") or []) for row in row_bundles)
    parse_issue_count = sum(len(row.get("parse_issues") or []) for row in row_bundles)
    gap_count = sum(len(row.get("unresolved_verifier_gaps") or []) for row in row_bundles)
    missing_row_count = sum(
        1
        for row in row_bundles
        if row.get("source_kind") in {"missing_file", "missing_index"}
        or row.get("row_status") == "malformed"
        or row.get("row") is None
    )
    provenance_labels = sorted(
        {
            f"{row.get('source_run_ref', {}).get('run_id') if isinstance(row.get('source_run_ref'), dict) else row.get('source_run_ref')}::{row.get('attempt_provenance')}"
            for row in row_bundles
        }
    )
    evidence_kinds: dict[str, int] = {}
    for row in row_bundles:
        for event in row.get("events") or []:
            cls = event.get("evidence_classification")
            if not isinstance(cls, dict):
                continue
            label = f"{cls.get('mode', 'unknown')}::{cls.get('action_kind', 'unknown')}::{cls.get('signal_scope', 'unknown')}"
            evidence_kinds[label] = evidence_kinds.get(label, 0) + 1
    return {
        "row_count": row_count,
        "event_count": event_count,
        "receipt_count": receipt_count,
        "parse_issue_count": parse_issue_count,
        "unresolved_gap_count": gap_count,
        "missing_row_count": missing_row_count,
        "provenance_labels": provenance_labels,
        "evidence_classifications": dict(sorted(evidence_kinds.items())),
        "non_cot_note": NON_COT_NOTE,
    }


def render_summary(bundle: dict[str, Any]) -> str:
    rows = bundle.get("rows") if isinstance(bundle, dict) else []
    summary = bundle.get("summary") if isinstance(bundle, dict) else {}
    if not isinstance(rows, list):
        rows = []
    if not isinstance(summary, dict):
        summary = {}

    lines: list[str] = []
    lines.append("# Observable Decision Trace Summary")
    lines.append("")
    lines.append(NON_COT_NOTE)
    lines.append("")
    lines.append(f"- Rows: {summary.get('row_count', len(rows))}")
    lines.append(f"- Events: {summary.get('event_count', 0)}")
    lines.append(f"- Receipt bundle items: {summary.get('receipt_count', 0)}")
    lines.append(f"- Parse issues: {summary.get('parse_issue_count', 0)}")
    lines.append(f"- Unresolved verifier gaps: {summary.get('unresolved_gap_count', 0)}")
    lines.append(f"- Missing or malformed rows tolerated: {summary.get('missing_row_count', 0)}")
    lines.append("")
    if summary.get("provenance_labels"):
        lines.append("## Provenance Labels")
        lines.append("")
        for label in summary.get("provenance_labels", []):
            lines.append(f"- {label}")
        lines.append("")
    if summary.get("evidence_classifications"):
        lines.append("## Evidence Classifications")
        lines.append("")
        for label, count in sorted(summary.get("evidence_classifications", {}).items()):
            lines.append(f"- {label}: {count}")
        lines.append("")
    lines.append("## Rows")
    lines.append("")
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_run_ref = row.get("source_run_ref")
        run_label = source_run_ref.get("run_id") if isinstance(source_run_ref, dict) else source_run_ref
        lines.append(f"### {row.get('task_id') or row.get('source_row_ref')}")
        lines.append("")
        lines.append(f"- source_run: `{run_label}`")
        lines.append(f"- attempt: `{row.get('attempt_ref')}`")
        lines.append(f"- attempt_provenance: `{row.get('attempt_provenance')}`")
        lines.append(f"- receipt_mode: `{row.get('primary_receipt_mode')}`")
        lines.append(f"- events: `{len(row.get('events') or [])}`")
        lines.append(f"- receipt_bundle_items: `{len(row.get('receipt_bundle') or [])}`")
        lines.append(f"- unresolved_verifier_gaps: `{len(row.get('unresolved_verifier_gaps') or [])}`")
        lines.append(f"- parse_issues: `{len(row.get('parse_issues') or [])}`")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_decision_trace_bundle(bundle: dict[str, Any], out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "decision_trace.jsonl"
    summary_path = out_dir / "decision_trace_summary.md"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in bundle.get("rows", []):
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n")
    summary_path.write_text(render_summary(bundle), encoding="utf-8")
    return {"decision_trace_jsonl": _resolved(jsonl_path), "decision_trace_summary": _resolved(summary_path)}


def build_and_write_bundle(inputs: Sequence[str | Path], out_dir: Path) -> dict[str, Any]:
    bundle = collect_decision_trace_bundle(inputs)
    output_files = write_decision_trace_bundle(bundle, out_dir)
    bundle["output_files"] = output_files
    return bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        nargs="+",
        required=True,
        help=(
            "One or more result-row roots or files. Direct index files are used if present "
            "(result_rows.jsonl, row.json, or attempt*_rows_combined.jsonl)."
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory for decision_trace.jsonl and decision_trace_summary.md.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    roots = [Path(item) for item in args.root]
    out_dir = Path(args.out) if args.out else _default_output_dir(roots)
    bundle = build_and_write_bundle(roots, out_dir)
    print(
        json.dumps(
            {
                "row_count": bundle.get("summary", {}).get("row_count", 0),
                "event_count": bundle.get("summary", {}).get("event_count", 0),
                "parse_issue_count": bundle.get("summary", {}).get("parse_issue_count", 0),
                "output_dir": _resolved(out_dir),
                "decision_trace_jsonl": bundle.get("output_files", {}).get("decision_trace_jsonl"),
                "decision_trace_summary": bundle.get("output_files", {}).get("decision_trace_summary"),
            },
            sort_keys=True,
        )
    )
    return 0


def _default_output_dir(roots: Sequence[Path]) -> Path:
    if len(roots) == 1:
        root = roots[0]
        if root.is_dir():
            return root / "decision_trace_bundle"
        return root.parent / "decision_trace_bundle"
    return Path.cwd() / "decision_trace_bundle"


if __name__ == "__main__":
    raise SystemExit(main())
