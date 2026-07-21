#!/usr/bin/env python3
"""Run a deterministic controlled replay report over captured Phase 2 traces.

This is a trace-only harness. It uses the existing ``replay_injection.py``
helpers and the committed ``phase2_traces`` checkpoints, but it does not make
model calls, execute solver/task code, run Docker/VM jobs, or touch a
benchmark/grader.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from replay_injection import build_ab_packet, load_trace, files_already_read, no_progress_signal, repeated_actions  # noqa: E402


@dataclass(frozen=True)
class ControlledReplayCase:
    case_id: str
    trace_rel_path: str

    @property
    def trace_path(self) -> Path:
        return _SCRIPT_DIR / self.trace_rel_path


CASES: tuple[ControlledReplayCase, ...] = (
    ControlledReplayCase(
        case_id="filter-js-from-html",
        trace_rel_path="phase2_traces/codex/filter-js-from-html.trace.json",
    ),
    ControlledReplayCase(
        case_id="sparql-university",
        trace_rel_path="phase2_traces/codex/sparql-university.trace.json",
    ),
    ControlledReplayCase(
        case_id="openssl-selfsigned-cert",
        trace_rel_path="phase2_traces/codex/openssl-selfsigned-cert.trace.json",
    ),
)

_AXIS_NAMES = (
    "old_context vs enriched_deterministic_context",
    "preset/basic context vs context recipe/structured memory evidence",
    "deterministic feedback vs active/verifier-like findings",
    "no verifier vs verifier packet evidence",
    "query_memory weak/absent vs enriched memory/tool guidance",
    "compression/simple vs current enriched context",
)


def _json_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _prefix_labels(trace: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in trace.get("prefix_messages", []) or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).lstrip()
        match = re.match(r"\[([^\]]+)\]", content)
        if match:
            labels.append(match.group(1))
    return labels


def _nonempty_keys(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    present: list[str] = []
    for key in keys:
        value = mapping.get(key)
        if value in (None, "", [], {}, ()):  # noqa: RUF012
            continue
        present.append(key)
    return present


def _axis_record(
    axis: str,
    *,
    status: str,
    evidence: Mapping[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    record = {"axis": axis, "status": status}
    if evidence is not None:
        record["evidence"] = dict(evidence)
    if reason:
        record["reason"] = reason
    return record


def _supporting_recipe_fields(architect_config: Mapping[str, Any]) -> list[str]:
    return _nonempty_keys(
        architect_config,
        (
            "selected_capabilities",
            "process_policy",
            "workflow_policy",
            "proof_plan",
            "inspection_plan",
            "verifier_model_tier",
        ),
    )


def _trace_feedback_fields(context: Mapping[str, Any]) -> list[str]:
    return _nonempty_keys(
        context,
        (
            "open_obligations",
            "planned_checks",
            "failure_clusters",
            "monitor_alerts",
            "recent_progress",
            "artifacts_present",
        ),
    )


def build_case_record(case: ControlledReplayCase) -> dict[str, Any]:
    trace = load_trace(case.trace_path)
    steps = trace.get("steps", []) or []
    if not steps:
        raise ValueError(f"{case.trace_path} has no steps")
    checkpoint_step = int(steps[-1].get("step", len(steps) - 1))
    checkpoint_turn_kind = str((steps[-1].get("turn") or {}).get("kind", ""))
    packet = build_ab_packet(trace, checkpoint_step)
    old_context = packet["variants"]["old_context"]
    enriched_context = packet["variants"]["enriched_deterministic_context"]
    context_delta = sorted(set(enriched_context) - set(old_context))
    repeated = repeated_actions(trace, checkpoint_step)
    reads = files_already_read(trace, checkpoint_step)
    progress = no_progress_signal(trace, checkpoint_step)
    pending_checks = old_context.get("pending_checks", []) or []
    enriched_pending_checks = enriched_context.get("pending_checks", []) or []
    repair_hints = [
        item
        for item in enriched_pending_checks
        if isinstance(item, dict) and str(item.get("repair_hint", "")).strip()
    ]
    prefix_labels = _prefix_labels(trace)
    architect_config = trace.get("architect_config", {})
    if not isinstance(architect_config, dict):
        architect_config = {}
    recipe_fields = _supporting_recipe_fields(architect_config)
    feedback_fields = _trace_feedback_fields(old_context)
    model_hint_present = False

    axes: list[dict[str, Any]] = [
        _axis_record(
            _AXIS_NAMES[0],
            status="pass",
            evidence={
                "old_context_key_count": len(old_context),
                "enriched_context_key_count": len(enriched_context),
                "added_keys": context_delta,
                "old_context_bytes": _json_size(old_context),
                "enriched_context_bytes": _json_size(enriched_context),
                "repeated_actions_count": len(repeated),
                "files_already_read_count": len(reads),
                "model_hint_present": model_hint_present,
            },
        ),
        _axis_record(
            _AXIS_NAMES[1],
            status="pass",
            evidence={
                "basic_context_labels": prefix_labels,
                "structured_recipe_fields": recipe_fields,
                "architect_model_tier": architect_config.get("architect_model_tier"),
                "solver_model_tier": architect_config.get("solver_model_tier"),
            },
        ),
        _axis_record(
            _AXIS_NAMES[2],
            status="evidence_limited",
            reason=(
                "The trace exposes deterministic feedback fields "
                f"{feedback_fields!r} but no active findings or verifier packet payload "
                "field to compare against."
            ),
        ),
        _axis_record(
            _AXIS_NAMES[3],
            status="evidence_limited",
            reason=(
                "Only the verifier model tier is present in architect_config; the trace "
                "does not include a verifier packet or packet-level verifier evidence block."
            ),
        ),
        _axis_record(
            _AXIS_NAMES[4],
            status="evidence_limited",
            reason=(
                "No query_memory, memory_guidance, or similar memory-tool guidance field "
                "is present in the captured trace checkpoints."
            ),
        ),
        _axis_record(
            _AXIS_NAMES[5],
            status="pass",
            evidence={
                "old_context_bytes": _json_size(old_context),
                "enriched_context_bytes": _json_size(enriched_context),
                "delta_bytes": _json_size(enriched_context) - _json_size(old_context),
                "no_progress_streak": int(progress["no_progress_streak"]),
                "repeated_actions_count": len(repeated),
                "files_already_read_count": len(reads),
                "pending_checks_count": len(pending_checks),
                "repair_hints_count": len(repair_hints),
            },
        ),
    ]

    return {
        "case_id": case.case_id,
        "trace_path": str(case.trace_path),
        "checkpoint_step": checkpoint_step,
        "checkpoint_turn_kind": checkpoint_turn_kind,
        "model_hint_present": model_hint_present,
        "metrics": {
            "repeated_actions_count": len(repeated),
            "files_already_read_count": len(reads),
            "no_progress_streak": int(progress["no_progress_streak"]),
            "pending_checks_count": len(pending_checks),
            "repair_hints_count": len(repair_hints),
            "model_hint_present": model_hint_present,
            "old_context_key_count": len(old_context),
            "enriched_context_key_count": len(enriched_context),
            "added_context_keys": context_delta,
        },
        "evidence": {
            "prefix_labels": prefix_labels,
            "structured_recipe_fields": recipe_fields,
            "feedback_fields": feedback_fields,
            "architect_config_fields": sorted(architect_config.keys()),
        },
        "axes": axes,
        "packet": {
            "old_context": old_context,
            "enriched_deterministic_context": enriched_context,
        },
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    axis_counts = {axis: {"pass": 0, "evidence_limited": 0} for axis in _AXIS_NAMES}
    totals = {
        "repeated_actions_count": 0,
        "files_already_read_count": 0,
        "no_progress_streak_max": 0,
        "pending_checks_count": 0,
        "repair_hints_count": 0,
    }
    for record in records:
        metrics = record["metrics"]
        totals["repeated_actions_count"] += int(metrics["repeated_actions_count"])
        totals["files_already_read_count"] += int(metrics["files_already_read_count"])
        totals["no_progress_streak_max"] = max(
            totals["no_progress_streak_max"],
            int(metrics["no_progress_streak"]),
        )
        totals["pending_checks_count"] += int(metrics["pending_checks_count"])
        totals["repair_hints_count"] += int(metrics["repair_hints_count"])
        for axis in record["axes"]:
            axis_counts[axis["axis"]][axis["status"]] += 1
    return {
        "case_count": len(records),
        "model_hint_present": False,
        "metric_totals": totals,
        "axis_status_counts": axis_counts,
    }


def _report(records: list[dict[str, Any]], summary: Mapping[str, Any]) -> str:
    lines = [
        "# Controlled Replay Report",
        "",
        "Trace-only replay harness over committed Phase 2 checkpoints.",
        "No model calls, solver/task execution, Docker, VM, or benchmark/grader work was performed.",
        "",
        "## Summary",
        "",
        "| case | trace | step | repeated_actions | files_already_read | no_progress_streak | pending_checks | repair_hints | model_hint_present |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for record in records:
        metrics = record["metrics"]
        lines.append(
            "| {case_id} | {trace_name} | {step} | {repeated_actions_count} | {files_already_read_count} | "
            "{no_progress_streak} | {pending_checks_count} | {repair_hints_count} | {model_hint_present} |".format(
                case_id=record["case_id"],
                trace_name=Path(record["trace_path"]).name,
                step=record["checkpoint_step"],
                repeated_actions_count=metrics["repeated_actions_count"],
                files_already_read_count=metrics["files_already_read_count"],
                no_progress_streak=metrics["no_progress_streak"],
                pending_checks_count=metrics["pending_checks_count"],
                repair_hints_count=metrics["repair_hints_count"],
                model_hint_present=str(metrics["model_hint_present"]).lower(),
            )
        )

    lines.extend(
        [
            "",
            "## Axis Coverage",
            "",
            "| axis | status | note |",
            "|---|---|---|",
        ]
    )
    for axis in _AXIS_NAMES:
        counts = summary["axis_status_counts"][axis]
        if counts["pass"] and not counts["evidence_limited"]:
            status = "pass"
        elif counts["evidence_limited"] and not counts["pass"]:
            status = "evidence_limited"
        else:
            status = "mixed"
        note = f"pass={counts['pass']} evidence_limited={counts['evidence_limited']}"
        lines.append(f"| {axis} | {status} | {note} |")

    lines.extend(["", "## Cases", ""])
    for record in records:
        metrics = record["metrics"]
        evidence = record["evidence"]
        lines.extend(
            [
                f"### {record['case_id']}",
                "",
                f"- Trace: `{record['trace_path']}`",
                f"- Checkpoint: step `{record['checkpoint_step']}` / turn `{record['checkpoint_turn_kind']}`",
                f"- Model hint present: `{str(record['model_hint_present']).lower()}`",
                f"- Prefix labels: `{', '.join(evidence['prefix_labels'])}`",
                f"- Structured recipe fields: `{', '.join(evidence['structured_recipe_fields'])}`",
                f"- Feedback fields: `{', '.join(evidence['feedback_fields'])}`",
                "",
                "| metric | value |",
                "|---|---:|",
                f"| repeated_actions_count | {metrics['repeated_actions_count']} |",
                f"| files_already_read_count | {metrics['files_already_read_count']} |",
                f"| no_progress_streak | {metrics['no_progress_streak']} |",
                f"| pending_checks_count | {metrics['pending_checks_count']} |",
                f"| repair_hints_count | {metrics['repair_hints_count']} |",
                f"| old_context_key_count | {metrics['old_context_key_count']} |",
                f"| enriched_context_key_count | {metrics['enriched_context_key_count']} |",
                f"| added_context_keys | `{', '.join(metrics['added_context_keys'])}` |",
                "",
                "| axis | status | evidence / reason |",
                "|---|---|---|",
            ]
        )
        for axis in record["axes"]:
            if "evidence" in axis:
                evidence_json = json.dumps(axis["evidence"], sort_keys=True, ensure_ascii=True)
                lines.append(f"| {axis['axis']} | {axis['status']} | `{evidence_json}` |")
            else:
                lines.append(f"| {axis['axis']} | {axis['status']} | {axis['reason']} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def run(out_dir: Path, *, cases: tuple[ControlledReplayCase, ...] = CASES) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    records = [build_case_record(case) for case in cases]
    summary = summarize(records)
    payload = {
        "schema_version": "aether_next.controlled_replay_report.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_hint_present": False,
        "cases": records,
        "summary": summary,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "CONTROLLED_REPLAY_REPORT.md").write_text(_report(records, summary), encoding="utf-8")
    return {"out_dir": str(out_dir), "summary": summary, "cases": records}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("controlled_replay_eval_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")),
    )
    args = parser.parse_args()
    result = run(args.out_dir)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
