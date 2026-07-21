"""Blocker-record constructors for verifier-report findings.

Builds the initial blocker dict from a verifier requirement-level
finding or a structure-level finding (parse / schema error).

Extracted from verifier.py to keep that module under 500 LOC.
No public API — all names are private (_prefixed).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from harness.aether2.traces._text_utils import (
    _clean_text,
    _normalize_string_list,
    _read_attr,
)

__all__: list[str] = []

_VERIFIER_INTEGRITY_REQUIREMENT = "verifier report integrity"
_BLOCKER_LIST_LIMITS = {
    "reason_codes": 6,
    "rejected_evidence_refs": 6,
    "rejected_evidence_provenance": 6,
    "required_next_evidence": 4,
}


def _default_next_evidence(*, requirement: str, verdict: str) -> str:
    if verdict == "contradicted":
        return f"fresh visible proof after repair for: {requirement}"
    if requirement == _VERIFIER_INTEGRITY_REQUIREMENT:
        return "repair verifier output shape and rerun verification"
    return f"direct visible evidence for: {requirement}"


def _build_blocker_from_requirement_finding(
    ledger: Mapping[str, Any],
    *,
    requirement: str,
    verdict: str,
    verifier_ref: str,
    finding: Any,
    report_summary: str,
    report_reason_codes: list[str],
    rejected_evidence_provenance: Iterable[str] | None = None,
) -> dict[str, Any]:
    from harness.aether2.traces.evidence_ledger import (
        _requirement_map,
        _new_requirement_entry,
    )
    from harness.aether2.traces.blockers import (
        _build_blocker_id,
        compute_relevant_evidence_version,
    )
    from harness.aether2.traces.verifier import _normalize_terminal_evidence_class

    requirement_map = _requirement_map(ledger)
    entry = requirement_map.get(requirement) or _new_requirement_entry(requirement)
    reason_codes = _normalize_string_list(
        _read_attr(finding, "reason_codes", ()) or report_reason_codes,
        limit=_BLOCKER_LIST_LIMITS["reason_codes"],
    )
    provenance = _normalize_string_list(
        _read_attr(finding, "evidence_provenance", ()) or rejected_evidence_provenance or (),
        limit=_BLOCKER_LIST_LIMITS["rejected_evidence_provenance"],
    )
    insufficiency_reason = (
        _clean_text(_read_attr(finding, "insufficiency_reason"))
        or _clean_text(_read_attr(finding, "evidence"))
        or report_summary
        or f"verifier lacked decisive visible evidence for: {requirement}"
    )
    rejected_evidence_refs = _normalize_string_list(
        _read_attr(finding, "rejected_evidence_refs", ())
        or _read_attr(finding, "insufficient_evidence_refs", ())
        or entry.get("evidence_refs", ()),
        limit=_BLOCKER_LIST_LIMITS["rejected_evidence_refs"],
    )
    required_next_evidence = _normalize_string_list(
        _read_attr(finding, "required_next_evidence", ())
        or [_default_next_evidence(requirement=requirement, verdict=verdict)],
        limit=_BLOCKER_LIST_LIMITS["required_next_evidence"],
    )
    required_evidence_class = _normalize_terminal_evidence_class(
        _read_attr(finding, "required_evidence_class"),
        requirement=requirement,
        evidence=_clean_text(_read_attr(finding, "evidence")) or report_summary,
        evidence_provenance=provenance,
        report_reason_codes=reason_codes,
    )
    evidence_version = compute_relevant_evidence_version(
        requirement=requirement,
        evidence_refs=entry.get("evidence_refs", ()),
        failed_checks=entry.get("failed_checks", ()),
        verifier_refs=[verifier_ref],
        reason_codes=reason_codes,
        evidence_classes=[required_evidence_class, *provenance],
    )
    req_id = entry["requirement_id"]
    return {
        "blocker_id": _build_blocker_id(
            requirement_id=req_id,
            verdict=verdict,
            reason_codes=reason_codes,
            insufficiency_reason=insufficiency_reason,
            required_next_evidence=required_next_evidence,
            required_evidence_class=required_evidence_class,
        ),
        "requirement_id": req_id,
        "requirement": requirement,
        "verdict": verdict,
        "reason_codes": reason_codes,
        "required_evidence_class": required_evidence_class,
        "created_step": None,
        "last_updated_step": None,
        "age_steps": 0,
        "rejected_evidence_refs": rejected_evidence_refs,
        "rejected_evidence_provenance": provenance,
        "insufficiency_reason": insufficiency_reason,
        "required_next_evidence": required_next_evidence,
        "evidence_version_last_evaluated": evidence_version,
        "status": "active",
        "resolution_evidence": "",
        "verifier_confirmation": "",
        "evaluation_rounds": 1,
        "candidate_resolution_attempts": 0,
    }


def _build_blocker_from_structure_finding(
    *,
    finding: Any,
    verifier_ref: str | None,
    step: int | None,
    report_summary: str,
) -> dict[str, Any]:
    from harness.aether2.traces.evidence_ledger import _requirement_id
    from harness.aether2.traces.blockers import (
        _build_blocker_id,
        compute_relevant_evidence_version,
    )
    from harness.aether2.traces.verifier import _normalize_terminal_evidence_class

    requirement = _VERIFIER_INTEGRITY_REQUIREMENT
    requirement_id = _requirement_id(requirement)
    verdict = "contradicted"
    detail = (
        _clean_text(_read_attr(finding, "detail"))
        or _clean_text(_read_attr(finding, "evidence"))
        or report_summary
        or "verifier report could not be interpreted"
    )
    reason_codes = _normalize_string_list(
        _read_attr(finding, "reason_codes", ()) or [_clean_text(_read_attr(finding, "kind"))],
        limit=_BLOCKER_LIST_LIMITS["reason_codes"],
    )
    next_evidence = _normalize_string_list(
        _read_attr(finding, "required_next_evidence", ())
        or ["repair verifier output shape and rerun verification"],
        limit=_BLOCKER_LIST_LIMITS["required_next_evidence"],
    )
    required_evidence_class = _normalize_terminal_evidence_class(
        _read_attr(finding, "required_evidence_class"),
        requirement=requirement,
        evidence=detail,
        evidence_provenance=("proxy",),
        report_reason_codes=reason_codes,
    )
    evidence_version = compute_relevant_evidence_version(
        requirement=requirement,
        evidence_refs=[detail],
        verifier_refs=[_clean_text(verifier_ref)],
        reason_codes=reason_codes,
        evidence_classes=[required_evidence_class],
    )
    return {
        "blocker_id": _build_blocker_id(
            requirement_id=requirement_id,
            verdict=verdict,
            reason_codes=reason_codes,
            insufficiency_reason=detail,
            required_next_evidence=next_evidence,
            required_evidence_class=required_evidence_class,
        ),
        "requirement_id": requirement_id,
        "requirement": requirement,
        "verdict": verdict,
        "reason_codes": reason_codes,
        "required_evidence_class": required_evidence_class,
        "created_step": step,
        "last_updated_step": step,
        "age_steps": 0,
        "rejected_evidence_refs": _normalize_string_list(
            _read_attr(finding, "rejected_evidence_refs", ()) or (),
            limit=_BLOCKER_LIST_LIMITS["rejected_evidence_refs"],
        ),
        "rejected_evidence_provenance": ["proxy"],
        "insufficiency_reason": detail,
        "required_next_evidence": next_evidence,
        "evidence_version_last_evaluated": evidence_version,
        "status": "active",
        "resolution_evidence": "",
        "verifier_confirmation": "",
        "evaluation_rounds": 1,
        "candidate_resolution_attempts": 0,
    }
