"""Blocker core: normalization, mutation, and evidence-version helpers.

Responsible for:
- Normalizing blocker records (_normalize_blocker, _blocker_map, _blocker_sort_key)
- Mutating blockers in a ledger (upsert, resolve, obsolete, exhausted)
- Evidence-version and relevance checks for blocker lifecycle bookkeeping

Verifier-report parsing lives in ``harness.aether2.traces.verifier``.
All public names are re-exported verbatim by
``harness.aether2.traces.delta`` so existing import sites
continue to work without change.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from harness.aether2.traces._text_utils import (
    _append_capped,
    _clean_text,
    _coerce_int,
    _normalize_string_list,
    _read_attr,
)
from harness.aether2.traces._blocker_relevance import (
    _current_blocker_evidence_version,
    _has_relevant_new_evidence,
    _evidence_overlaps_blocker,
    _token_set,
)

__all__ = [
    "compute_relevant_evidence_version",
    "mark_blockers_candidate_resolved",
    "mark_blockers_exhausted",
]

# ---------------------------------------------------------------------------
# Constants (kept in sync with evidence_ledger.py)
# ---------------------------------------------------------------------------

_VALID_BLOCKER_STATUSES = {"active", "candidate_resolved", "resolved", "obsolete", "exhausted"}
_REQUIREMENT_LIST_LIMITS = {
    "evidence_refs": 6,
    "evidence_provenance": 6,
    "failed_checks": 4,
    "disproven_assumptions": 4,
    "open_risks": 4,
    "verifier_blockers": 4,
    "next_required_evidence": 4,
}
_MAX_BLOCKERS = 48
_BLOCKER_LIST_LIMITS = {
    "reason_codes": 6,
    "rejected_evidence_refs": 6,
    "rejected_evidence_provenance": 6,
    "required_next_evidence": 4,
}
_VERIFIER_INTEGRITY_REQUIREMENT = "verifier report integrity"


# ---------------------------------------------------------------------------
# Public blocker mutation functions
# ---------------------------------------------------------------------------


def mark_blockers_candidate_resolved(
    ledger: Mapping[str, Any] | None,
    *,
    step: int | None = None,
    requirement: str | None = None,
    blocker_ids: Iterable[str] | None = None,
    relevant_evidence_refs: Iterable[str] | None = None,
    relevant_failed_checks: Iterable[str] | None = None,
    relevant_artifact_paths: Iterable[str] | None = None,
    relevant_verifier_refs: Iterable[str] | None = None,
    relevant_evidence_classes: Iterable[str] | None = None,
) -> dict[str, Any]:
    from harness.aether2.traces.evidence_ledger import compact_evidence_ledger

    normalized = compact_evidence_ledger(ledger)
    blocker_id_filter = set(_normalize_string_list(blocker_ids or (), limit=_MAX_BLOCKERS))
    blockers = _blocker_map(normalized)
    for blocker in _iter_target_blockers(
        blockers,
        requirement=requirement,
        blocker_id_filter=blocker_id_filter,
        statuses={"active", "exhausted"},
    ):
        if not _has_relevant_new_evidence(
            normalized,
            blocker=blocker,
            relevant_evidence_refs=relevant_evidence_refs,
            relevant_failed_checks=relevant_failed_checks,
            relevant_artifact_paths=relevant_artifact_paths,
            relevant_verifier_refs=relevant_verifier_refs,
            relevant_evidence_classes=relevant_evidence_classes,
        ):
            continue
        blocker["status"] = "candidate_resolved"
        blocker["last_updated_step"] = _choose_step(step, blocker.get("last_updated_step"))
        blocker["age_steps"] = _compute_age_steps(blocker.get("created_step"), blocker.get("last_updated_step"))
        blocker["evidence_version_last_evaluated"] = _current_blocker_evidence_version(
            normalized,
            blocker=blocker,
            relevant_evidence_refs=relevant_evidence_refs,
            relevant_failed_checks=relevant_failed_checks,
            relevant_artifact_paths=relevant_artifact_paths,
            relevant_verifier_refs=relevant_verifier_refs,
            relevant_evidence_classes=relevant_evidence_classes,
        )
    normalized["blockers"] = list(blockers.values())
    return compact_evidence_ledger(normalized)


def mark_blockers_exhausted(
    ledger: Mapping[str, Any] | None,
    *,
    step: int | None = None,
    requirement: str | None = None,
    blocker_ids: Iterable[str] | None = None,
    exhaustion_round_limit: int = 2,
    force: bool = False,
) -> dict[str, Any]:
    from harness.aether2.traces.evidence_ledger import compact_evidence_ledger

    normalized = compact_evidence_ledger(ledger)
    blocker_id_filter = set(_normalize_string_list(blocker_ids or (), limit=_MAX_BLOCKERS))
    limit = max(1, exhaustion_round_limit)
    blockers = _blocker_map(normalized)
    for blocker in _iter_target_blockers(
        blockers,
        requirement=requirement,
        blocker_id_filter=blocker_id_filter,
        statuses={"active", "candidate_resolved"},
    ):
        if not force and int(blocker.get("candidate_resolution_attempts") or 0) < limit:
            continue
        blocker["status"] = "exhausted"
        blocker["last_updated_step"] = _choose_step(step, blocker.get("last_updated_step"))
        blocker["age_steps"] = _compute_age_steps(blocker.get("created_step"), blocker.get("last_updated_step"))
    normalized["blockers"] = list(blockers.values())
    return compact_evidence_ledger(normalized)


def compute_relevant_evidence_version(
    *,
    requirement: str,
    evidence_refs: Iterable[str] | None = None,
    failed_checks: Iterable[str] | None = None,
    artifact_paths: Iterable[str] | None = None,
    verifier_refs: Iterable[str] | None = None,
    reason_codes: Iterable[str] | None = None,
    evidence_classes: Iterable[str] | None = None,
) -> str:
    payload = {
        "requirement": _clean_text(requirement),
        "artifact_paths": _normalize_string_list(artifact_paths or (), limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"]),
        "evidence_refs": _normalize_string_list(evidence_refs or (), limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"]),
        "failed_checks": _normalize_string_list(failed_checks or (), limit=_REQUIREMENT_LIST_LIMITS["failed_checks"]),
        "evidence_classes": _normalize_string_list(evidence_classes or (), limit=_REQUIREMENT_LIST_LIMITS["evidence_provenance"]),
        "reason_codes": _normalize_string_list(reason_codes or (), limit=_BLOCKER_LIST_LIMITS["reason_codes"]),
        "verifier_refs": _normalize_string_list(verifier_refs or (), limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"]),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Blocker normalization helpers (also called from evidence_ledger.py)
# ---------------------------------------------------------------------------


def _normalize_blocker_status(value: Any, *, default: str) -> str:
    text = _clean_text(value).lower()
    if text in _VALID_BLOCKER_STATUSES:
        return text
    return default


def _blocker_map(ledger: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    blocker_map: dict[str, dict[str, Any]] = {}
    if not isinstance(ledger, Mapping):
        return blocker_map
    for item in ledger.get("blockers", ()) or ():
        if not isinstance(item, Mapping):
            continue
        blocker = _normalize_blocker(item)
        blocker_map[blocker["blocker_id"]] = blocker
    return blocker_map


def _normalize_blocker(value: Mapping[str, Any]) -> dict[str, Any]:
    requirement = _clean_text(value.get("requirement"))
    from harness.aether2.traces.evidence_ledger import _requirement_id, _normalize_requirement_status

    requirement_id = _clean_text(value.get("requirement_id")) or _requirement_id(requirement)
    verdict = _normalize_requirement_status(value.get("verdict"), default="unproven")
    reason_codes = _normalize_string_list(value.get("reason_codes", ()), limit=_BLOCKER_LIST_LIMITS["reason_codes"])
    rejected_refs = _normalize_string_list(
        value.get("rejected_evidence_refs", ()),
        limit=_BLOCKER_LIST_LIMITS["rejected_evidence_refs"],
    )
    next_evidence = _normalize_string_list(
        value.get("required_next_evidence", ()),
        limit=_BLOCKER_LIST_LIMITS["required_next_evidence"],
    )
    insufficiency_reason = _clean_text(value.get("insufficiency_reason"))
    blocker_id = _clean_text(value.get("blocker_id")) or _build_blocker_id(
        requirement_id=requirement_id,
        verdict=verdict,
        reason_codes=reason_codes,
        insufficiency_reason=insufficiency_reason,
        required_next_evidence=next_evidence,
    )
    created_step = _coerce_int(value.get("created_step"))
    last_updated_step = _coerce_int(value.get("last_updated_step"))
    status = _normalize_blocker_status(value.get("status"), default="active")
    resolution_evidence = _clean_text(value.get("resolution_evidence"))
    verifier_confirmation = _clean_text(value.get("verifier_confirmation"))
    return {
        "blocker_id": blocker_id,
        "requirement_id": requirement_id,
        "requirement": requirement,
        "verdict": verdict,
        "reason_codes": reason_codes,
        "created_step": created_step,
        "last_updated_step": last_updated_step,
        "age_steps": _coerce_int(value.get("age_steps"))
        if _coerce_int(value.get("age_steps")) is not None
        else _compute_age_steps(created_step, last_updated_step),
        "rejected_evidence_refs": rejected_refs,
        "insufficiency_reason": insufficiency_reason,
        "required_next_evidence": next_evidence,
        "evidence_version_last_evaluated": _clean_text(value.get("evidence_version_last_evaluated")),
        "status": status,
        "resolution_evidence": resolution_evidence,
        "verifier_confirmation": verifier_confirmation,
        "evaluation_rounds": max(1, _coerce_int(value.get("evaluation_rounds")) or 1),
        "candidate_resolution_attempts": max(0, _coerce_int(value.get("candidate_resolution_attempts")) or 0),
    }


def _blocker_sort_key(blocker: Mapping[str, Any]) -> tuple[str, int, str]:
    status_rank = {
        "active": 0,
        "candidate_resolved": 1,
        "exhausted": 2,
        "resolved": 3,
        "obsolete": 4,
    }.get(str(blocker.get("status", "active")), 5)
    return (
        _clean_text(blocker.get("requirement")),
        status_rank,
        _clean_text(blocker.get("blocker_id")),
    )


def _build_blocker_id(
    *,
    requirement_id: str,
    verdict: str,
    reason_codes: Iterable[str],
    insufficiency_reason: str,
    required_next_evidence: Iterable[str],
    required_evidence_class: str = "",
) -> str:
    from harness.aether2.traces.evidence_ledger import _normalize_requirement_status

    payload = {
        "requirement_id": _clean_text(requirement_id),
        "verdict": _normalize_requirement_status(verdict, default="unproven"),
        "reason_codes": _normalize_string_list(reason_codes, limit=_BLOCKER_LIST_LIMITS["reason_codes"]),
        "insufficiency_reason": _clean_text(insufficiency_reason),
        "required_next_evidence": _normalize_string_list(
            required_next_evidence,
            limit=_BLOCKER_LIST_LIMITS["required_next_evidence"],
        ),
        "required_evidence_class": _clean_text(required_evidence_class),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"blk_{digest}"


# ---------------------------------------------------------------------------
# Blocker mutation helpers (private)
# ---------------------------------------------------------------------------


def _upsert_blocker(
    ledger: dict[str, Any],
    blocker: Mapping[str, Any],
    *,
    step: int | None,
    exhaustion_round_limit: int,
) -> None:
    blockers = _blocker_map(ledger)
    incoming = _normalize_blocker(blocker)
    current = blockers.get(incoming["blocker_id"])
    if current is None:
        incoming["created_step"] = _choose_step(step, incoming.get("created_step"))
        incoming["last_updated_step"] = _choose_step(step, incoming.get("last_updated_step"))
        incoming["age_steps"] = _compute_age_steps(incoming.get("created_step"), incoming.get("last_updated_step"))
        blockers[incoming["blocker_id"]] = incoming
        ledger["blockers"] = list(blockers.values())
        return

    previous_status = current["status"]
    previous_created_step = current.get("created_step")
    previous_last_updated_step = current.get("last_updated_step")
    previous_evaluation_rounds = int(current.get("evaluation_rounds") or 1)
    previous_candidate_attempts = int(current.get("candidate_resolution_attempts") or 0)
    current.update(incoming)
    current["created_step"] = _choose_step(previous_created_step, incoming.get("created_step"))
    current["last_updated_step"] = _choose_step(step, previous_last_updated_step)
    current["age_steps"] = _compute_age_steps(current.get("created_step"), current.get("last_updated_step"))
    current["evaluation_rounds"] = previous_evaluation_rounds + 1
    current["resolution_evidence"] = ""
    current["verifier_confirmation"] = ""
    if previous_status == "candidate_resolved":
        attempts = previous_candidate_attempts + 1
        current["candidate_resolution_attempts"] = attempts
        current["status"] = "exhausted" if attempts >= max(1, exhaustion_round_limit) else "active"
    elif previous_status == "resolved":
        current["status"] = "active"
        current["candidate_resolution_attempts"] = 0
    else:
        current["candidate_resolution_attempts"] = previous_candidate_attempts
        current["status"] = "active"
    blockers[current["blocker_id"]] = current
    ledger["blockers"] = list(blockers.values())


def _resolve_requirement_blockers(
    ledger: dict[str, Any],
    *,
    requirement: str,
    step: int | None,
    resolution_evidence: str,
    verifier_confirmation: str,
) -> None:
    blockers = _blocker_map(ledger)
    for blocker in blockers.values():
        if blocker["requirement"] != requirement:
            continue
        if blocker["status"] in {"resolved", "obsolete"}:
            continue
        blocker["status"] = "resolved"
        blocker["resolution_evidence"] = _clean_text(resolution_evidence)
        blocker["verifier_confirmation"] = _clean_text(verifier_confirmation)
        blocker["last_updated_step"] = _choose_step(step, blocker.get("last_updated_step"))
        blocker["age_steps"] = _compute_age_steps(blocker.get("created_step"), blocker.get("last_updated_step"))
    ledger["blockers"] = list(blockers.values())


def _obsolete_unseen_blockers(
    ledger: dict[str, Any],
    *,
    touched_requirements: set[str],
    seen_blockers_by_requirement: dict[str, set[str]],
    step: int | None,
) -> None:
    blockers = _blocker_map(ledger)
    for blocker in blockers.values():
        requirement = blocker["requirement"]
        if requirement not in touched_requirements:
            continue
        if blocker["status"] in {"resolved", "obsolete"}:
            continue
        seen_ids = seen_blockers_by_requirement.get(requirement, set())
        if blocker["blocker_id"] in seen_ids:
            continue
        if blocker["status"] == "candidate_resolved":
            continue
        blocker["status"] = "obsolete"
        blocker["last_updated_step"] = _choose_step(step, blocker.get("last_updated_step"))
        blocker["age_steps"] = _compute_age_steps(blocker.get("created_step"), blocker.get("last_updated_step"))
    ledger["blockers"] = list(blockers.values())


def _iter_target_blockers(
    blockers: Mapping[str, dict[str, Any]],
    *,
    requirement: str | None,
    blocker_id_filter: set[str],
    statuses: set[str],
) -> Iterable[dict[str, Any]]:
    requirement_text = _clean_text(requirement)
    for blocker in blockers.values():
        if blocker_id_filter and blocker["blocker_id"] not in blocker_id_filter:
            continue
        if requirement_text and blocker["requirement"] != requirement_text:
            continue
        if blocker["status"] not in statuses:
            continue
        yield blocker


# ---------------------------------------------------------------------------
# Step / age helpers
# ---------------------------------------------------------------------------


def _compute_age_steps(created_step: Any, last_updated_step: Any) -> int:
    created = _coerce_int(created_step)
    updated = _coerce_int(last_updated_step)
    if created is None or updated is None:
        return 0
    return max(0, updated - created)


def _choose_step(preferred: Any, fallback: Any) -> int | None:
    preferred_int = _coerce_int(preferred)
    if preferred_int is not None:
        return preferred_int
    return _coerce_int(fallback)


def _coerce_step(step: Any, *, report: Any, verifier_ref: str | None) -> int | None:
    explicit = _coerce_int(step)
    if explicit is not None:
        return explicit
    report_step = _coerce_int(_read_attr(report, "step"))
    if report_step is not None:
        return report_step
    return _extract_step_from_text(verifier_ref)


def _extract_step_from_text(value: str | None) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    match = re.search(r"step=(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def _build_verifier_ref(
    *,
    requirement: str,
    verdict: str,
    evidence: str,
    verifier_ref: str | None,
) -> str:
    parts = [f"verifier requirement={requirement}", f"verdict={verdict}"]
    if evidence:
        parts.append(f"evidence={evidence}")
    if _clean_text(verifier_ref):
        parts.append(f"ref={_clean_text(verifier_ref)}")
    return " ".join(parts)
