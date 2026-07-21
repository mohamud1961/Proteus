"""Verifier-report integration helpers for HarnessEng Aether-2.

Responsible for:
- Parsing structured verifier reports into blocker records
- Inferring evidence provenance and evidence class from report text
- Building blocker dicts from requirement-level and structure-level findings

All public names are re-exported verbatim by
``harness.aether2.traces.delta`` so existing import sites
continue to work without change.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from harness.aether2.traces._text_utils import (
    _append_capped,
    _clean_text,
    _normalize_string_list,
    _read_attr,
)
from harness.aether2.traces._blocker_builders import (
    _build_blocker_from_requirement_finding,
    _build_blocker_from_structure_finding,
    _default_next_evidence,
)

__all__ = [
    "register_verifier_blockers",
    "record_verifier_report",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REQUIREMENT_LIST_LIMITS = {
    "evidence_refs": 6,
    "evidence_provenance": 6,
    "failed_checks": 4,
    "disproven_assumptions": 4,
    "open_risks": 4,
    "verifier_blockers": 4,
    "next_required_evidence": 4,
}
_BLOCKER_LIST_LIMITS = {
    "reason_codes": 6,
    "rejected_evidence_refs": 6,
    "rejected_evidence_provenance": 6,
    "required_next_evidence": 4,
}
_VERIFIER_INTEGRITY_REQUIREMENT = "verifier report integrity"

_EVIDENCE_CLASS_VOCABULARY = (
    "external_client_or_protocol",
    "fresh_process_execution",
    "filesystem_or_path_state",
    "service_survival_or_response",
    "provided_check_execution",
    "value_or_invariant_comparison",
    "generic_observation",
)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def record_verifier_report(
    ledger: Mapping[str, Any] | None,
    *,
    report: Any,
    verifier_ref: str | None = None,
    step: int | None = None,
    exhaustion_round_limit: int = 2,
) -> dict[str, Any]:
    return register_verifier_blockers(
        ledger,
        report=report,
        verifier_ref=verifier_ref,
        step=step,
        exhaustion_round_limit=exhaustion_round_limit,
    )


def register_verifier_blockers(
    ledger: Mapping[str, Any] | None,
    *,
    report: Any,
    verifier_ref: str | None = None,
    step: int | None = None,
    exhaustion_round_limit: int = 2,
) -> dict[str, Any]:
    from harness.aether2.traces.evidence_ledger import (
        compact_evidence_ledger,
        _ensure_requirement_entry,
        _normalize_requirement_status,
        _record_failure_family,
    )
    from harness.aether2.traces.blockers import (
        _upsert_blocker,
        _resolve_requirement_blockers,
        _obsolete_unseen_blockers,
        _normalize_blocker_status,
        _coerce_step,
        _build_verifier_ref,
    )

    normalized = compact_evidence_ledger(ledger)
    summary = _clean_text(_read_attr(report, "summary"))
    step_value = _coerce_step(step, report=report, verifier_ref=verifier_ref)
    report_reason_codes = _normalize_string_list(
        _read_attr(report, "reason_codes", ()) or (),
        limit=_BLOCKER_LIST_LIMITS["reason_codes"],
    )
    seen_blockers_by_requirement: dict[str, set[str]] = {}
    touched_requirements: set[str] = {_VERIFIER_INTEGRITY_REQUIREMENT}

    for item in _read_attr(report, "requirements", ()) or ():
        requirement = _clean_text(_read_attr(item, "requirement"))
        if not requirement:
            continue
        touched_requirements.add(requirement)
        verdict = _normalize_requirement_status(_read_attr(item, "verdict"), default="unproven")
        evidence = _clean_text(_read_attr(item, "evidence"))
        provenance = _normalize_string_list(
            _read_attr(item, "evidence_provenance", ()) or _infer_evidence_provenance(
                requirement=requirement,
                verdict=verdict,
                evidence=evidence,
                report_reason_codes=report_reason_codes,
                source_requirement=_ensure_requirement_entry(normalized, requirement),
            ),
            limit=_REQUIREMENT_LIST_LIMITS["evidence_provenance"],
        )
        entry = _ensure_requirement_entry(normalized, requirement)
        if provenance:
            entry["evidence_provenance"] = _normalize_string_list(
                [*entry.get("evidence_provenance", ()), *provenance],
                limit=_REQUIREMENT_LIST_LIMITS["evidence_provenance"],
            )
        evidence_ref = _build_verifier_ref(
            requirement=requirement,
            verdict=verdict,
            evidence=evidence or summary,
            verifier_ref=verifier_ref,
        )
        entry["evidence_refs"] = _append_capped(
            entry["evidence_refs"],
            evidence_ref,
            limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"],
        )

        if verdict == "proven":
            entry["status"] = "proven"
            entry["evidence_strength"] = "strong"
            entry["verifier_blockers"] = []
            entry["next_required_evidence"] = []
            _resolve_requirement_blockers(
                normalized,
                requirement=requirement,
                step=step_value,
                resolution_evidence=evidence or summary or f"verifier confirmed requirement: {requirement}",
                verifier_confirmation=evidence_ref,
            )
            continue

        if verdict == "contradicted":
            entry["status"] = "contradicted"
            entry["evidence_strength"] = "strong"
            if evidence:
                entry["open_risks"] = _append_capped(
                    entry["open_risks"],
                    evidence,
                    limit=_REQUIREMENT_LIST_LIMITS["open_risks"],
                )
            entry["disproven_assumptions"] = _append_capped(
                entry["disproven_assumptions"],
                f"requirement already satisfied: {requirement}",
                limit=_REQUIREMENT_LIST_LIMITS["disproven_assumptions"],
            )
            entry["next_required_evidence"] = _append_capped(
                entry["next_required_evidence"],
                f"fresh visible proof after repair for: {requirement}",
                limit=_REQUIREMENT_LIST_LIMITS["next_required_evidence"],
            )
            _record_failure_family(normalized, family="verifier_unsatisfied", evidence_ref=evidence_ref)
        else:
            if entry["status"] == "proven":
                entry["status"] = "partial" if entry["evidence_refs"] else "unproven"
            if evidence:
                entry["open_risks"] = _append_capped(
                    entry["open_risks"],
                    evidence,
                    limit=_REQUIREMENT_LIST_LIMITS["open_risks"],
                )
            _record_failure_family(normalized, family="verifier_unverifiable", evidence_ref=evidence_ref)

        blocker = _build_blocker_from_requirement_finding(
            normalized,
            requirement=requirement,
            verdict=verdict,
            verifier_ref=evidence_ref,
            finding=item,
            report_summary=summary,
            report_reason_codes=report_reason_codes,
            rejected_evidence_provenance=provenance,
        )
        blocker_id = blocker["blocker_id"]
        seen_blockers_by_requirement.setdefault(requirement, set()).add(blocker_id)
        _upsert_blocker(
            normalized,
            blocker,
            step=step_value,
            exhaustion_round_limit=exhaustion_round_limit,
        )

    structure_findings = list(_iter_verifier_structure_findings(report))
    if structure_findings:
        touched_requirements.add(_VERIFIER_INTEGRITY_REQUIREMENT)
    for finding in structure_findings:
        requirement = _VERIFIER_INTEGRITY_REQUIREMENT
        entry = _ensure_requirement_entry(normalized, requirement)
        entry["status"] = "contradicted"
        entry["evidence_strength"] = "strong"
        detail = _clean_text(_read_attr(finding, "detail")) or summary or "verifier report could not be interpreted"
        entry["open_risks"] = _append_capped(
            entry["open_risks"],
            detail,
            limit=_REQUIREMENT_LIST_LIMITS["open_risks"],
        )
        blocker = _build_blocker_from_structure_finding(
            finding=finding,
            verifier_ref=verifier_ref,
            step=step_value,
            report_summary=summary,
        )
        blocker_id = blocker["blocker_id"]
        seen_blockers_by_requirement.setdefault(requirement, set()).add(blocker_id)
        _upsert_blocker(
            normalized,
            blocker,
            step=step_value,
            exhaustion_round_limit=exhaustion_round_limit,
        )
        _record_failure_family(
            normalized,
            family=_clean_text(_read_attr(finding, "failure_family")) or "verifier_structure_failure",
            evidence_ref=verifier_ref,
        )

    _obsolete_unseen_blockers(
        normalized,
        touched_requirements=touched_requirements,
        seen_blockers_by_requirement=seen_blockers_by_requirement,
        step=step_value,
    )

    for code in report_reason_codes:
        _record_failure_family(normalized, family=f"verifier_reason:{code}", evidence_ref=verifier_ref)

    return compact_evidence_ledger(normalized)


# ---------------------------------------------------------------------------
# Verifier structure-finding iterator
# ---------------------------------------------------------------------------


def _iter_verifier_structure_findings(report: Any) -> Iterable[dict[str, Any]]:
    parse_items = []
    parse_value = _read_attr(report, "parse_error")
    if parse_value:
        parse_items.append(parse_value)
    parse_items.extend(list(_read_attr(report, "parse_failures", ()) or ()))
    for item in parse_items:
        detail = _clean_text(_read_attr(item, "detail")) or _clean_text(item)
        if not detail:
            continue
        yield {
            "kind": "verifier_parse_failure",
            "detail": detail,
            "reason_codes": _read_attr(item, "reason_codes", ()) or ("verifier_parse_failure",),
            "required_next_evidence": _read_attr(item, "required_next_evidence", ())
            or ("repair verifier output format before retry",),
            "rejected_evidence_refs": _read_attr(item, "rejected_evidence_refs", ()) or (),
            "failure_family": "verifier_parse_failure",
        }

    schema_items = list(_read_attr(report, "schema_errors", ()) or ())
    schema_items.extend(list(_read_attr(report, "schema_failures", ()) or ()))
    for item in schema_items:
        detail = _clean_text(_read_attr(item, "detail")) or _clean_text(item)
        if not detail:
            continue
        yield {
            "kind": "verifier_schema_failure",
            "detail": detail,
            "reason_codes": _read_attr(item, "reason_codes", ()) or ("verifier_schema_failure",),
            "required_next_evidence": _read_attr(item, "required_next_evidence", ())
            or ("repair verifier schema and rerun verification",),
            "rejected_evidence_refs": _read_attr(item, "rejected_evidence_refs", ()) or (),
            "failure_family": "verifier_schema_failure",
        }


# ---------------------------------------------------------------------------
# Evidence provenance / class inference helpers
# ---------------------------------------------------------------------------


def _infer_evidence_provenance(
    *,
    requirement: str,
    verdict: str,
    evidence: str,
    report_reason_codes: Iterable[str] | None = None,
    source_requirement: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Best-effort, conservative provenance fallback when a verifier finding omits it."""

    corpus = " ".join(
        part for part in [_clean_text(requirement), _clean_text(evidence)] if part
    ).lower()
    reason_codes = {str(code).strip().lower() for code in (report_reason_codes or ())}
    labels: list[str] = []

    if any(
        token in corpus
        for token in ("read back", "readback", "cat ", "head ", "tail ", "ls ", "exists", "present")
    ):
        labels.append("readback")
    if any(
        token in corpus
        for token in ("same method", "same heuristic", "self-check", "self check", "circular", "replayed", "same client")
    ):
        labels.append("same_method_check")
    if any(token in corpus for token in ("--help", "--version", "command -v", "which ", "import ")):
        labels.append("model_authored_check")
    if "verifier_parse_failure" in reason_codes or "verifier_schema_failure" in reason_codes:
        labels.append("model_authored_check")

    if not labels:
        labels.append("unknown")

    return tuple(_normalize_string_list(labels, limit=_REQUIREMENT_LIST_LIMITS["evidence_provenance"]))


def _normalize_terminal_evidence_class(
    value: Any,
    *,
    requirement: str,
    evidence: str,
    evidence_provenance: Iterable[str] | None = None,
    report_reason_codes: Iterable[str] | None = None,
) -> str:
    """Return a small, generic required-evidence-class label."""

    explicit = _clean_text(value).lower().replace(" ", "_")
    if explicit in _EVIDENCE_CLASS_VOCABULARY:
        return explicit

    corpus = " ".join(
        part for part in [_clean_text(requirement), _clean_text(evidence)] if part
    ).lower()
    provenance = {str(item).strip().lower() for item in (evidence_provenance or ())}
    reason_codes = {str(code).strip().lower() for code in (report_reason_codes or ())}

    if any(token in corpus for token in ("curl", "http", "client", "request", "response", "external")):
        return "external_client_or_protocol"
    if any(token in corpus for token in ("service", "survive", "survival", "listening", "port", "process")):
        return "service_survival_or_response"
    if any(token in corpus for token in ("fresh process", "new process", "reimport", "restart")):
        return "fresh_process_execution"
    if any(token in corpus for token in ("path", "install", "directory", "filesystem", "artifact")):
        return "filesystem_or_path_state"
    if any(token in corpus for token in ("pytest", "cargo test", "go test", "npm test", "make test", "provided check", "official test")):
        return "provided_check_execution"
    if any(token in corpus for token in ("expected", "actual", "checksum", "hash", "diff", "invariant")):
        return "value_or_invariant_comparison"
    if "proxy" in provenance or "verifier_parse_failure" in reason_codes or "verifier_schema_failure" in reason_codes:
        return "generic_observation"
    return "generic_observation"


def _infer_evidence_classes(
    *,
    evidence_refs: Iterable[str] | None = None,
    failed_checks: Iterable[str] | None = None,
    artifact_paths: Iterable[str] | None = None,
    verifier_refs: Iterable[str] | None = None,
) -> list[str]:
    corpus = " ".join(
        _clean_text(item)
        for item in [
            *(evidence_refs or ()),
            *(failed_checks or ()),
            *(artifact_paths or ()),
            *(verifier_refs or ()),
        ]
        if _clean_text(item)
    ).lower()
    classes: list[str] = []
    if any(token in corpus for token in ("curl", "http", "client", "request", "response")):
        classes.append("external_client_or_protocol")
    if any(token in corpus for token in ("service", "survive", "survival", "listening", "port", "process")):
        classes.append("service_survival_or_response")
    if any(token in corpus for token in ("fresh process", "new process", "reimport", "restart")):
        classes.append("fresh_process_execution")
    if any(token in corpus for token in ("path", "install", "directory", "filesystem", "artifact")):
        classes.append("filesystem_or_path_state")
    if any(token in corpus for token in ("pytest", "cargo test", "go test", "npm test", "make test", "provided check")):
        classes.append("provided_check_execution")
    if any(token in corpus for token in ("expected", "actual", "checksum", "hash", "diff", "invariant")):
        classes.append("value_or_invariant_comparison")
    return _normalize_string_list(classes, limit=_REQUIREMENT_LIST_LIMITS["evidence_provenance"])


# ---------------------------------------------------------------------------
# Blocker builder helpers (private)
# ---------------------------------------------------------------------------


# _build_blocker_from_requirement_finding, _build_blocker_from_structure_finding,
# _default_next_evidence imported from _blocker_builders above.
