"""Durable evidence-ledger primitives for HarnessEng Aether-2.

Responsible for:
- Creating and compacting the evidence ledger dict
- Recording observation / check-result evidence on requirements
- Requirement-entry CRUD helpers

All public names are re-exported verbatim by
``older_variants.harness.aether2.traces.delta`` so existing import sites
continue to work without change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from older_variants.harness.aether2.traces._text_utils import (
    _VALID_REQUIREMENT_STATUSES,
    _VALID_EVIDENCE_STRENGTHS,
    _append_capped,
    _clean_text,
    _coerce_int,
    _normalize_string_list,
    _read_attr,
    _normalize_requirement_status,
    _normalize_evidence_strength,
    _build_observation_ref,
    _build_check_ref,
    _failure_family_from_check,
)
from older_variants.harness.aether2.traces._failure_families import (
    _record_failure_family,
    _normalize_failure_families,
)

__all__ = [
    "build_evidence_ledger",
    "compact_evidence_ledger",
    "ensure_stated_requirements",
    "record_check_results",
    "record_observation_evidence",
    "serialize_evidence_ledger",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EVIDENCE_LEDGER_VERSION = 1
_REQUIREMENT_LIST_LIMITS = {
    "evidence_refs": 6,
    "evidence_provenance": 6,
    "failed_checks": 4,
    "disproven_assumptions": 4,
    "open_risks": 4,
    "verifier_blockers": 4,
    "next_required_evidence": 4,
}
_MAX_REQUIREMENTS = 24
_MAX_FAILURE_FAMILIES = 8
_MAX_BLOCKERS = 48
_MAX_TERMINAL_CLAIMS = 24


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def build_evidence_ledger(requirements: Iterable[str] | None = None) -> dict[str, Any]:
    """Return a fresh evidence ledger pre-seeded with *requirements*."""
    entries = [_new_requirement_entry(requirement) for requirement in requirements or ()]
    return compact_evidence_ledger(
        {
            "version": _EVIDENCE_LEDGER_VERSION,
            "requirements": entries,
            "blockers": [],
            "terminal_claims": [],
            "repeated_failure_families": [],
        }
    )


def ensure_stated_requirements(
    ledger: Mapping[str, Any] | None,
    requirements: Iterable[str] | None,
) -> dict[str, Any]:
    normalized = compact_evidence_ledger(ledger)
    requirement_map = _requirement_map(normalized)
    for requirement in requirements or ():
        text = _clean_text(requirement)
        if not text or text in requirement_map:
            continue
        requirement_map[text] = _new_requirement_entry(text)
    normalized["requirements"] = list(requirement_map.values())
    return compact_evidence_ledger(normalized)


def record_observation_evidence(
    ledger: Mapping[str, Any] | None,
    *,
    requirement: str,
    tool_name: str,
    step: int | None = None,
    exit_code: int | None = None,
    raw_log_path: str | None = None,
    artifact_paths: Iterable[str] | None = None,
    note: str | None = None,
    disproved_assumption: str | None = None,
    open_risk: str | None = None,
    verifier_blocker: str | None = None,
    next_required_evidence: str | None = None,
    failure_family: str | None = None,
) -> dict[str, Any]:
    normalized = compact_evidence_ledger(ledger)
    entry = _ensure_requirement_entry(normalized, requirement)
    artifacts = [_clean_text(path) for path in artifact_paths or () if _clean_text(path)]
    evidence_ref = _build_observation_ref(
        tool_name=tool_name,
        step=step,
        exit_code=exit_code,
        raw_log_path=raw_log_path,
        artifact_paths=artifacts,
        note=note,
    )

    if exit_code == 0 and (artifacts or _clean_text(note)):
        entry["evidence_refs"] = _append_capped(
            entry["evidence_refs"],
            evidence_ref,
            limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"],
        )
        if entry["status"] == "unproven":
            entry["status"] = "partial"
        if entry["evidence_strength"] == "none":
            entry["evidence_strength"] = "weak"
        entry["next_required_evidence"] = _append_capped(
            entry["next_required_evidence"],
            _clean_text(next_required_evidence)
            or f"direct visible proof for requirement: {entry['requirement']}",
            limit=_REQUIREMENT_LIST_LIMITS["next_required_evidence"],
        )

    if _clean_text(disproved_assumption):
        entry["disproven_assumptions"] = _append_capped(
            entry["disproven_assumptions"],
            _clean_text(disproved_assumption),
            limit=_REQUIREMENT_LIST_LIMITS["disproven_assumptions"],
        )
    if _clean_text(open_risk):
        entry["open_risks"] = _append_capped(
            entry["open_risks"],
            _clean_text(open_risk),
            limit=_REQUIREMENT_LIST_LIMITS["open_risks"],
        )
    if _clean_text(verifier_blocker):
        entry["verifier_blockers"] = _append_capped(
            entry["verifier_blockers"],
            _clean_text(verifier_blocker),
            limit=_REQUIREMENT_LIST_LIMITS["verifier_blockers"],
        )
    if _clean_text(next_required_evidence):
        entry["next_required_evidence"] = _append_capped(
            entry["next_required_evidence"],
            _clean_text(next_required_evidence),
            limit=_REQUIREMENT_LIST_LIMITS["next_required_evidence"],
        )
    if exit_code not in (None, 0):
        family = _clean_text(failure_family) or "tool_observation_nonzero_exit"
        _record_failure_family(normalized, family=family, evidence_ref=evidence_ref)

    return compact_evidence_ledger(normalized)


def record_check_results(
    ledger: Mapping[str, Any] | None,
    *,
    requirement: str,
    check_results: Iterable[Any],
    step: int | None = None,
    raw_log_path: str | None = None,
) -> dict[str, Any]:
    normalized = compact_evidence_ledger(ledger)
    entry = _ensure_requirement_entry(normalized, requirement)
    for result in check_results:
        command = _clean_text(_read_attr(result, "command"))
        exit_code = _coerce_int(_read_attr(result, "exit_code"))
        timed_out = bool(_read_attr(result, "timed_out", False))
        reason_code = _clean_text(_read_attr(result, "error_reason_code"))
        error_kind = _clean_text(_read_attr(result, "error_kind"))
        evidence_ref = _build_check_ref(
            command=command,
            step=step,
            exit_code=exit_code,
            raw_log_path=raw_log_path,
            reason_code=reason_code,
            error_kind=error_kind,
            timed_out=timed_out,
        )

        if exit_code == 0 and not timed_out:
            entry["evidence_refs"] = _append_capped(
                entry["evidence_refs"],
                evidence_ref,
                limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"],
            )
            if entry["status"] == "unproven":
                entry["status"] = "partial"
            if entry["evidence_strength"] == "none":
                entry["evidence_strength"] = "weak"
            entry["next_required_evidence"] = _append_capped(
                entry["next_required_evidence"],
                f"fresh requirement-level verification for: {entry['requirement']}",
                limit=_REQUIREMENT_LIST_LIMITS["next_required_evidence"],
            )
            continue

        summary = (
            f"cmd={command or '<unknown>'} exit={exit_code if exit_code is not None else 'none'}"
            + (" timed_out=true" if timed_out else "")
            + (f" reason={reason_code}" if reason_code else "")
            + (f" kind={error_kind}" if error_kind else "")
        )
        entry["failed_checks"] = _append_capped(
            entry["failed_checks"],
            summary,
            limit=_REQUIREMENT_LIST_LIMITS["failed_checks"],
        )
        entry["evidence_refs"] = _append_capped(
            entry["evidence_refs"],
            evidence_ref,
            limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"],
        )
        entry["disproven_assumptions"] = _append_capped(
            entry["disproven_assumptions"],
            f"declared check would verify requirement: {command or '<unknown>'}",
            limit=_REQUIREMENT_LIST_LIMITS["disproven_assumptions"],
        )
        entry["open_risks"] = _append_capped(
            entry["open_risks"],
            f"declared check failed for requirement: {entry['requirement']}",
            limit=_REQUIREMENT_LIST_LIMITS["open_risks"],
        )
        entry["next_required_evidence"] = _append_capped(
            entry["next_required_evidence"],
            f"repair and rerun a visible check for: {entry['requirement']}",
            limit=_REQUIREMENT_LIST_LIMITS["next_required_evidence"],
        )
        entry["status"] = "contradicted"
        entry["evidence_strength"] = "strong"
        _record_failure_family(
            normalized,
            family=_failure_family_from_check(reason_code=reason_code, timed_out=timed_out, exit_code=exit_code),
            evidence_ref=evidence_ref,
        )
    return compact_evidence_ledger(normalized)


def serialize_evidence_ledger(ledger: Mapping[str, Any] | None) -> str:
    return json.dumps(
        compact_evidence_ledger(ledger),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def compact_evidence_ledger(ledger: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized_requirements: list[dict[str, Any]] = []
    requirement_map = _requirement_map(ledger)
    blocker_map = _blocker_map_from_ledger(ledger)
    terminal_claims = _normalize_terminal_claims(
        (ledger or {}).get("terminal_claims", ()) if isinstance(ledger, Mapping) else (),
        limit=_MAX_TERMINAL_CLAIMS,
    )
    blocker_summaries = _blocker_requirement_summaries(blocker_map.values())
    for requirement in sorted(requirement_map):
        entry = requirement_map[requirement]
        blocker_summary = blocker_summaries.get(requirement, {})
        normalized_requirements.append(
            {
                "requirement_id": entry["requirement_id"],
                "requirement": requirement,
                "status": _normalize_requirement_status(entry.get("status"), default="unproven"),
                "evidence_strength": _normalize_evidence_strength(entry.get("evidence_strength"), default="none"),
                "evidence_refs": _normalize_string_list(
                    entry.get("evidence_refs", ()),
                    limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"],
                ),
                "evidence_provenance": _normalize_string_list(
                    entry.get("evidence_provenance", ()),
                    limit=_REQUIREMENT_LIST_LIMITS["evidence_provenance"],
                ),
                "failed_checks": _normalize_string_list(
                    entry.get("failed_checks", ()),
                    limit=_REQUIREMENT_LIST_LIMITS["failed_checks"],
                ),
                "disproven_assumptions": _normalize_string_list(
                    entry.get("disproven_assumptions", ()),
                    limit=_REQUIREMENT_LIST_LIMITS["disproven_assumptions"],
                ),
                "open_risks": _normalize_string_list(
                    entry.get("open_risks", ()),
                    limit=_REQUIREMENT_LIST_LIMITS["open_risks"],
                ),
                "verifier_blockers": _normalize_string_list(
                    [
                        *list(entry.get("verifier_blockers", ()) or ()),
                        *list(blocker_summary.get("verifier_blockers", ()) or ()),
                    ],
                    limit=_REQUIREMENT_LIST_LIMITS["verifier_blockers"],
                ),
                "next_required_evidence": _normalize_string_list(
                    [
                        *list(entry.get("next_required_evidence", ()) or ()),
                        *list(blocker_summary.get("next_required_evidence", ()) or ()),
                    ],
                    limit=_REQUIREMENT_LIST_LIMITS["next_required_evidence"],
                ),
            }
        )

    normalized_blockers = _normalize_blockers_from_ledger(
        (ledger or {}).get("blockers", ()) if isinstance(ledger, Mapping) else (),
        limit=_MAX_BLOCKERS,
    )
    normalized_families = _normalize_failure_families(
        (ledger or {}).get("repeated_failure_families", ()) if isinstance(ledger, Mapping) else (),
        limit=_MAX_FAILURE_FAMILIES,
    )

    return {
        "version": _EVIDENCE_LEDGER_VERSION,
        "requirements": normalized_requirements[:_MAX_REQUIREMENTS],
        "blockers": normalized_blockers,
        "terminal_claims": terminal_claims,
        "repeated_failure_families": normalized_families,
    }


# ---------------------------------------------------------------------------
# Internal helpers: requirement entries
# ---------------------------------------------------------------------------


def _new_requirement_entry(requirement: str) -> dict[str, Any]:
    cleaned = _clean_text(requirement)
    return {
        "requirement_id": _requirement_id(cleaned),
        "requirement": cleaned,
        "status": "unproven",
        "evidence_strength": "none",
        "evidence_refs": [],
        "evidence_provenance": [],
        "failed_checks": [],
        "disproven_assumptions": [],
        "open_risks": [],
        "verifier_blockers": [],
        "next_required_evidence": [],
    }


def _ensure_requirement_entry(ledger: dict[str, Any], requirement: str) -> dict[str, Any]:
    requirement_map = _requirement_map(ledger)
    text = _clean_text(requirement)
    if not text:
        raise ValueError("requirement must be non-empty")
    if text not in requirement_map:
        requirement_map[text] = _new_requirement_entry(text)
    ledger["requirements"] = list(requirement_map.values())
    return requirement_map[text]


def _requirement_map(ledger: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    requirement_map: dict[str, dict[str, Any]] = {}
    if not isinstance(ledger, Mapping):
        return requirement_map
    for item in ledger.get("requirements", ()) or ():
        if not isinstance(item, Mapping):
            continue
        requirement = _clean_text(item.get("requirement"))
        if not requirement:
            continue
        requirement_map[requirement] = {
            "requirement_id": _clean_text(item.get("requirement_id")) or _requirement_id(requirement),
            "requirement": requirement,
            "status": _normalize_requirement_status(item.get("status"), default="unproven"),
            "evidence_strength": _normalize_evidence_strength(item.get("evidence_strength"), default="none"),
            "evidence_refs": _normalize_string_list(item.get("evidence_refs", ()), limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"]),
            "evidence_provenance": _normalize_string_list(
                item.get("evidence_provenance", ()),
                limit=_REQUIREMENT_LIST_LIMITS["evidence_provenance"],
            ),
            "failed_checks": _normalize_string_list(item.get("failed_checks", ()), limit=_REQUIREMENT_LIST_LIMITS["failed_checks"]),
            "disproven_assumptions": _normalize_string_list(
                item.get("disproven_assumptions", ()),
                limit=_REQUIREMENT_LIST_LIMITS["disproven_assumptions"],
            ),
            "open_risks": _normalize_string_list(item.get("open_risks", ()), limit=_REQUIREMENT_LIST_LIMITS["open_risks"]),
            "verifier_blockers": _normalize_string_list(
                item.get("verifier_blockers", ()),
                limit=_REQUIREMENT_LIST_LIMITS["verifier_blockers"],
            ),
            "next_required_evidence": _normalize_string_list(
                item.get("next_required_evidence", ()),
                limit=_REQUIREMENT_LIST_LIMITS["next_required_evidence"],
            ),
        }
    return requirement_map


def _requirement_id(requirement: str) -> str:
    cleaned = _clean_text(requirement)
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12] if cleaned else "unknown"
    return f"req_{digest}"


# ---------------------------------------------------------------------------
# Internal helpers: blockers (compact-only surface — no mutation)
# ---------------------------------------------------------------------------


def _blocker_map_from_ledger(ledger: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return a blocker-id → normalized-blocker dict from *ledger*.

    Importers that need full blocker mutation should use
    ``older_variants.harness.aether2.traces.blockers._blocker_map`` instead.
    """
    from older_variants.harness.aether2.traces.blockers import _normalize_blocker

    blocker_map: dict[str, dict[str, Any]] = {}
    if not isinstance(ledger, Mapping):
        return blocker_map
    for item in ledger.get("blockers", ()) or ():
        if not isinstance(item, Mapping):
            continue
        blocker = _normalize_blocker(item)
        blocker_map[blocker["blocker_id"]] = blocker
    return blocker_map


def _normalize_blockers_from_ledger(values: Iterable[Any], *, limit: int) -> list[dict[str, Any]]:
    from older_variants.harness.aether2.traces.blockers import _normalize_blocker, _blocker_sort_key

    normalized = [_normalize_blocker(value) for value in values or () if isinstance(value, Mapping)]
    normalized.sort(key=_blocker_sort_key)
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit]


def _blocker_requirement_summaries(blockers: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, list[str]]]:
    from older_variants.harness.aether2.traces.blockers import _normalize_blocker_status

    grouped: dict[str, dict[str, list[str]]] = {}
    for blocker in blockers:
        status = _normalize_blocker_status(blocker.get("status"), default="active")
        if status in {"resolved", "obsolete"}:
            continue
        requirement = _clean_text(blocker.get("requirement"))
        if not requirement:
            continue
        group = grouped.setdefault(requirement, {"verifier_blockers": [], "next_required_evidence": []})
        label = _clean_text(blocker.get("insufficiency_reason"))
        if label:
            group["verifier_blockers"] = _append_capped(
                group["verifier_blockers"],
                label,
                limit=_REQUIREMENT_LIST_LIMITS["verifier_blockers"],
            )
        for item in blocker.get("required_next_evidence", ()) or ():
            group["next_required_evidence"] = _append_capped(
                group["next_required_evidence"],
                _clean_text(item),
                limit=_REQUIREMENT_LIST_LIMITS["next_required_evidence"],
            )
    return grouped


# ---------------------------------------------------------------------------
# Internal helpers: terminal claims (compact-only surface)
# ---------------------------------------------------------------------------


def _normalize_terminal_claims(values: Iterable[Any], *, limit: int) -> list[dict[str, Any]]:
    from older_variants.harness.aether2.traces.terminal_claims import (
        _normalize_terminal_claim,
        _terminal_claim_sort_key,
    )

    normalized = [_normalize_terminal_claim(value) for value in values or () if isinstance(value, Mapping)]
    normalized.sort(key=_terminal_claim_sort_key)
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit]
