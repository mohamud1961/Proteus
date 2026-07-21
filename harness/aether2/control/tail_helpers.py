"""Tail-state, plan-tracking, and loop-support helpers for the Aether-2 control loop.

Pure extraction from loop.py — zero behaviour change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "_build_tail_state",
    "_check_result_summary",
    "_collect_established_facts",
    "_collect_tail_events",
    "_diff_to_dict",
    "_estimate_transcript_tokens",
    "_job_alive_safe",
    "_model_requested_rebase",
    "_model_requested_verification",
    "_sync_fact_ledger_state",
    "_unused_affordances",
    "_update_plan_text",
]


def _build_tail_state(
    *,
    plan_text: str | None,
    elapsed_sec: float,
    remaining_sec: float | None,
    evidence_ledger: Mapping[str, Any],
    mirror: Any,
    streak: int,
    job_registry: Any,
    session_registry: Any,
    job_ids: list[str],
    session_ids: list[str],
    note: Any | None,
    events: list[str] | None = None,
    proof_state: Mapping[str, Any] | None = None,
    progress_note: str | None = None,
) -> dict[str, Any]:
    """Build the §6.3 tail-state dict appended to each model request."""
    # Import locally to avoid circulars.
    from harness.aether2.control.requirements import _tail_evidence_ledger

    fuel_gauge: dict[str, Any] = {"elapsed_sec": round(elapsed_sec, 3)}
    if remaining_sec is not None:
        fuel_gauge["remaining_sec"] = round(remaining_sec, 3)

    jobs_state: dict[str, Any] = {}
    for job_id in job_ids:
        try:
            status = job_registry.status(job_id)
            jobs_state[job_id] = {"alive": status.alive, "exit_code": status.exit_code}
        except KeyError:
            continue

    derived_state: dict[str, Any] = {
        "no_delta_streak": streak,
        "active_jobs": jobs_state,
        "active_sessions": list(session_ids),
    }
    if events:
        derived_state["events"] = list(events)

    tail: dict[str, Any] = {
        "plan": plan_text or "",
        "fuel_gauge": fuel_gauge,
        "derived_state": derived_state,
        "evidence_ledger": _tail_evidence_ledger(evidence_ledger),
    }
    if isinstance(proof_state, Mapping) and proof_state:
        tail["proof_state"] = dict(proof_state)
    if progress_note:
        tail["progress_note"] = progress_note
    if note is not None:
        tail["mirror_note"] = note.text
        if note.fuel_gauge_text:
            tail["mirror_fuel_gauge"] = note.fuel_gauge_text
    return tail


def _model_requested_rebase(response_text: str, tool_calls: Any) -> bool:
    """Return True when the model's text-only response requests a context rebase."""
    if tool_calls:
        return False
    first_line = response_text.splitlines()[0].strip().upper() if response_text.splitlines() else ""
    return first_line.startswith("REBASE_REQUEST:")


def _model_requested_verification(response_text: str, tool_calls: Any) -> bool:
    """Return True when the model's text-only response requests verification."""
    if tool_calls:
        return False
    first_line = response_text.splitlines()[0].strip().upper() if response_text.splitlines() else ""
    return first_line.startswith("VERIFY_REQUEST:")


def _collect_established_facts(context: Any) -> list[str]:
    """Return a compact list of facts visible in the context's delta_state."""
    snapshot = context.delta_state
    if snapshot is None:
        return []
    facts: list[str] = []
    files = getattr(snapshot, "files", {}) or {}
    jobs = getattr(snapshot, "job_registry", {}) or {}
    sessions = getattr(snapshot, "session_registry", {}) or {}
    if files:
        facts.append(f"written files: {', '.join(sorted(files)[:5])}")
    if jobs:
        facts.append(f"jobs: {', '.join(sorted(jobs)[:5])}")
    if sessions:
        facts.append(f"sessions: {', '.join(sorted(sessions)[:5])}")
    return facts


def _unused_affordances() -> list[str]:
    """Return the list of tool names that are affordances not yet used in a run."""
    return [
        "start_job",
        "job_status",
        "session_start",
        "session_send",
        "session_read",
        "read_file",
        "write_file",
        "wait",
    ]


def _collect_tail_events(
    *,
    ctx: Any,
    job_registry: Any,
    job_ids: list[str],
    seen_artifacts: set[str],
    known_job_status: dict[str, tuple[bool, int | None]],
) -> list[str]:
    """Spec §6.3: artifact/service events since the last tail render.

    New artifacts written and job started/died transitions. Mutates
    ``seen_artifacts`` / ``known_job_status`` to track what has already been
    surfaced.
    """
    events: list[str] = []
    for path in sorted(ctx.last_delta_report.added_paths):
        if path in seen_artifacts:
            continue
        seen_artifacts.add(path)
        events.append(f"artifact_written:{path}")

    for job_id in job_ids:
        try:
            status = job_registry.status(job_id)
        except KeyError:
            continue
        current = (status.alive, status.exit_code)
        previous = known_job_status.get(job_id)
        if previous is None:
            events.append(f"job_started:{job_id}")
        elif previous[0] and not current[0]:
            events.append(f"job_died:{job_id} exit_code={current[1]}")
        known_job_status[job_id] = current

    return events


def _sync_fact_ledger_state(context: Any, ctx: Any) -> None:
    """Merge ExecutionContext cumulative facts into context.delta_state (spec §6.5)."""
    from dataclasses import replace as dataclass_replace

    if context.delta_state is None:
        return
    context.delta_state = dataclass_replace(
        context.delta_state,
        installed_packages=tuple(ctx.installed_packages),
        nonzero_exits=tuple(ctx.nonzero_exits),
    )


def _update_plan_text(plan_text: str | None, response_text: str) -> str | None:
    """Track the model-owned plan (spec §6.3 tail telemetry).

    The first non-empty assistant ``response.text`` becomes the initial plan.
    Thereafter, if an assistant turn's first line starts with "PLAN"
    (case-insensitive), its full text replaces the plan.
    """
    if not response_text:
        return plan_text
    first_line = response_text.splitlines()[0] if response_text.splitlines() else ""
    if first_line.strip().upper().startswith("PLAN"):
        return response_text
    if plan_text is None:
        return response_text
    return plan_text


def _estimate_transcript_tokens(context: Any) -> int:
    """Cheaply estimate the token count of the context's transcript."""
    rendered = json.dumps(context.transcript, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return len(rendered.encode("utf-8")) // 4


def _diff_to_dict(report: Any) -> dict[str, Any]:
    """Serialise a DeltaReport into a plain dict for verification payloads."""
    return {
        "files_changed": [item.__dict__ for item in report.files_changed],
        "added_paths": list(report.added_paths),
        "modified_paths": list(report.modified_paths),
        "deleted_paths": list(report.deleted_paths),
        "artifact_registry_changed": report.artifact_registry_changed,
        "service_registry_changed": report.service_registry_changed,
        "process_registry_changed": report.process_registry_changed,
        "job_registry_changed": report.job_registry_changed,
        "session_registry_changed": report.session_registry_changed,
    }


def _job_alive_safe(job_registry: Any, job_id: str) -> bool:
    """Return True if a job is alive or exited cleanly, False on KeyError."""
    try:
        status = job_registry.status(job_id)
    except KeyError:
        return False
    return status.alive or status.exit_code == 0


def _check_result_summary(result: Any) -> str:
    """Build a compact human-readable summary string from a check result."""
    command = str(getattr(result, "command", "") or "").strip() or "<unknown>"
    exit_code = getattr(result, "exit_code", None)
    summary = f"cmd={command} exit={exit_code if exit_code is not None else 'none'}"
    if bool(getattr(result, "timed_out", False)):
        summary += " timed_out=true"
    reason_code = str(getattr(result, "error_reason_code", "") or "").strip()
    if reason_code:
        summary += f" reason={reason_code}"
    return summary
