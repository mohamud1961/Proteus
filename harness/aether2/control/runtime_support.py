"""Runtime-support helpers for the Aether-2 control loop.

Responsibilities:
- Determine whether a service-monitoring pass is warranted for a given task_done.
- Execute a bounded monitoring pass over jobs, sessions, and services.
- Provide environment-contract drift detection between orientation and verification.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

from harness.aether2.runtime.jobs import JobRegistry
from harness.aether2.runtime.sessions import SessionRegistry
from harness.aether2.traces.delta import (
    StateSnapshot,
    snapshot as delta_snapshot,
    with_evidence_ledger,
)

__all__ = [
    "_SERVICE_MONITOR_WINDOW_SEC",
    "_check_runs_in_workspace",
    "_env_contract_drift",
    "_env_contract_metadata",
    "_job_status_payload",
    "_monitor_persistent_runtime",
    "_service_monitoring_candidate",
    "_service_pid",
]

_SERVICE_MONITOR_WINDOW_SEC = 2


def _service_monitoring_candidate(
    *,
    job_ids: list[str],
    session_ids: list[str],
    claim_checks: list[str],
    snapshot: StateSnapshot,
) -> bool:
    """Return True if this task_done warrants a bounded service-monitoring pass."""

    if job_ids or session_ids or getattr(snapshot, "service_registry", {}):
        return True
    service_tokens = ("curl", "http://", "https://", "port", "listen", "service", "server", "socket", "pgrep", "lsof")
    return any(any(token in check.lower() for token in service_tokens) for check in claim_checks)


def _job_status_payload(job_registry: JobRegistry, job_id: str) -> dict[str, Any]:
    """Return a status payload dict for a single job, safe on missing job."""

    try:
        status = job_registry.status(job_id)
    except KeyError:
        return {"present": False}
    return {
        "present": True,
        "alive": status.alive,
        "exit_code": status.exit_code,
        "pid": status.pid,
        "tail": status.tail,
    }


def _check_runs_in_workspace(result: Any, workspace_root: Path) -> bool:
    """Return True if result.cwd is inside workspace_root."""

    cwd = Path(str(getattr(result, "cwd", "") or "")).resolve(strict=False)
    try:
        cwd.relative_to(workspace_root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _service_pid(entry: Mapping[str, Any] | None) -> str | None:
    """Extract the PID string from a service registry entry, or None."""

    if not isinstance(entry, Mapping):
        return None
    value = entry.get("pid")
    if value is None:
        return None
    return str(value)


def _monitor_persistent_runtime(
    *,
    ctx: Any,
    job_registry: JobRegistry,
    session_registry: SessionRegistry,
    job_ids: list[str],
    session_ids: list[str],
    claim_checks: list[str],
    check_results: list[Any],
    remaining_sec: float | None,
    start_snapshot: StateSnapshot,
) -> tuple[dict[str, Any], StateSnapshot]:
    """Run a bounded monitoring pass and return an action-digest + updated snapshot.

    Observes jobs, sessions, and services over a short time window to collect
    evidence of persistent runtime activity. Returns `{"applies": False}` when
    no monitoring candidate is detected.
    """

    if not _service_monitoring_candidate(
        job_ids=job_ids,
        session_ids=session_ids,
        claim_checks=claim_checks,
        snapshot=start_snapshot,
    ):
        return {"applies": False}, start_snapshot

    bounded_window = max(0, min(_SERVICE_MONITOR_WINDOW_SEC, int(remaining_sec or 0)))
    if bounded_window <= 0:
        return {
            "applies": True,
            "window_sec": 0,
            "summary": ["bounded monitoring window unavailable before deadline"],
        }, start_snapshot

    start_jobs = {job_id: _job_status_payload(job_registry, job_id) for job_id in job_ids}
    start_sessions = set(session_registry.list_session_ids())
    start_services = getattr(start_snapshot, "service_registry", {}) or {}
    time.sleep(bounded_window)
    end_snapshot = delta_snapshot(ctx.workspace_root)
    end_snapshot = with_evidence_ledger(end_snapshot, getattr(start_snapshot, "evidence_ledger", {}))
    end_jobs = {job_id: _job_status_payload(job_registry, job_id) for job_id in job_ids}
    end_sessions = set(session_registry.list_session_ids())
    end_services = getattr(end_snapshot, "service_registry", {}) or {}

    summary: list[str] = []
    jobs_payload: dict[str, Any] = {}
    for job_id in job_ids:
        before = start_jobs.get(job_id, {"present": False})
        after = end_jobs.get(job_id, {"present": False})
        start_log_size = int(((getattr(start_snapshot, "job_registry", {}) or {}).get(job_id, {}) or {}).get("log_size", 0))
        end_log_size = int(((getattr(end_snapshot, "job_registry", {}) or {}).get(job_id, {}) or {}).get("log_size", 0))
        log_growth = max(0, end_log_size - start_log_size)
        jobs_payload[job_id] = {
            "start": before,
            "end": after,
            "log_growth_bytes": log_growth,
        }
        if before.get("alive") and after.get("alive"):
            summary.append(f"job {job_id} still running after {bounded_window}s bounded window")
        elif before.get("alive") and not after.get("alive"):
            summary.append(
                f"job {job_id} exited before end of {bounded_window}s bounded window exit code={after.get('exit_code')}"
            )
        if before.get("pid") and after.get("pid") and before.get("pid") != after.get("pid"):
            summary.append(
                f"job {job_id} pid changed from {before.get('pid')} to {after.get('pid')} after {bounded_window}s bounded window"
            )
        if log_growth:
            summary.append(f"job {job_id} log grew by {log_growth} bytes during bounded window")
        tail = str(after.get("tail", "") or "").lower()
        if any(token in tail for token in ("error", "traceback", "exception")):
            summary.append(f"job {job_id} produced new error output during bounded window")

    sessions_payload: dict[str, Any] = {}
    for session_id in session_ids:
        present_before = session_id in start_sessions
        present_after = session_id in end_sessions
        sessions_payload[session_id] = {"start_present": present_before, "end_present": present_after}
        if present_before and present_after:
            summary.append(f"session {session_id} remained registered after {bounded_window}s bounded window")
        elif present_before and not present_after:
            summary.append(f"session {session_id} disappeared before end of {bounded_window}s bounded window")

    services_payload: dict[str, Any] = {}
    for service_id in sorted(set(start_services) | set(end_services)):
        before = start_services.get(service_id)
        after = end_services.get(service_id)
        services_payload[service_id] = {"start": before, "end": after}
        before_pid = _service_pid(before)
        after_pid = _service_pid(after)
        if before is not None and after is not None and before_pid and after_pid and before_pid != after_pid:
            summary.append(
                f"service {service_id} pid changed from {before_pid} to {after_pid} after {bounded_window}s bounded window"
            )
        elif before is not None and after is not None:
            summary.append(f"service {service_id} remained registered after {bounded_window}s bounded window")
        elif before is not None and after is None:
            summary.append(f"service {service_id} disappeared before end of {bounded_window}s bounded window")

    same_workspace_probes = bool(check_results) and all(
        _check_runs_in_workspace(result, ctx.workspace_root) for result in check_results
    )
    if check_results:
        if same_workspace_probes:
            summary.append("client probes ran from the same workspace root")
        else:
            summary.append("client probes did not run from the same workspace root")
        if any(bool(getattr(result, "timed_out", False)) for result in check_results):
            summary.append("client probe timed out during bounded monitoring window")
        successful_outputs = [
            str(getattr(result, "stdout", "") or "").strip()
            for result in check_results
            if getattr(result, "exit_code", None) == 0 and str(getattr(result, "stdout", "") or "").strip()
        ]
        if len(successful_outputs) >= 2 and len(set(successful_outputs)) == 1:
            summary.append("repeated client probes returned the same response body across the bounded window")

    if not summary:
        summary.append(f"bounded monitoring observed no decisive service or persistence change over {bounded_window}s")

    return {
        "applies": True,
        "window_sec": bounded_window,
        "jobs": jobs_payload,
        "sessions": sessions_payload,
        "services": services_payload,
        "summary": summary,
    }, end_snapshot


def _env_contract_metadata(snapshot: Mapping[str, Any]) -> dict[str, str | None]:
    """Extract version and digest from an orientation snapshot dict."""

    version = snapshot.get("env_contract_version")
    digest = snapshot.get("env_contract_digest")
    return {
        "version": str(version) if isinstance(version, str) and version else None,
        "digest": str(digest) if isinstance(digest, str) and digest else None,
    }


def _env_contract_drift(
    orientation_snapshot: Mapping[str, Any],
    verification_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Detect environment-contract drift between orientation and verification snapshots."""

    orientation_meta = _env_contract_metadata(orientation_snapshot)
    verification_meta = _env_contract_metadata(verification_snapshot)
    differences: list[str] = []
    if orientation_meta["version"] != verification_meta["version"]:
        differences.append("contract_version_changed")
    if orientation_meta["digest"] != verification_meta["digest"]:
        differences.append("contract_digest_changed")
    return {
        "orientation": orientation_meta,
        "verification": verification_meta,
        "drift_detected": bool(differences),
        "differences": differences,
    }
