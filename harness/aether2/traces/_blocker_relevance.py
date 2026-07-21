"""Evidence-relevance checking for blocker lifecycle bookkeeping.

Determines whether a set of new evidence is relevant enough to move a blocker
into candidate-resolved state before the next verifier judgement. Extracted
from blockers.py to keep that module under 500 LOC.

No public API — all names are private (_prefixed).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from harness.aether2.traces._text_utils import (
    _TOKEN_RE,
    _RELEVANCE_STOPWORDS,
    _clean_text,
    _normalize_string_list,
)

__all__: list[str] = []

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


def _current_blocker_evidence_version(
    ledger: Mapping[str, Any],
    *,
    blocker: Mapping[str, Any],
    relevant_evidence_refs: Iterable[str] | None,
    relevant_failed_checks: Iterable[str] | None,
    relevant_artifact_paths: Iterable[str] | None,
    relevant_verifier_refs: Iterable[str] | None,
    relevant_evidence_classes: Iterable[str] | None,
) -> str:
    from harness.aether2.traces.evidence_ledger import _requirement_map
    from harness.aether2.traces.blockers import compute_relevant_evidence_version
    from harness.aether2.traces.verifier import _infer_evidence_classes

    entry = _requirement_map(ledger).get(_clean_text(blocker.get("requirement"))) or {}
    evidence_refs = list(entry.get("evidence_refs", []) or [])
    failed_checks = list(entry.get("failed_checks", []) or [])
    evidence_refs.extend(_normalize_string_list(relevant_evidence_refs or (), limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"]))
    failed_checks.extend(_normalize_string_list(relevant_failed_checks or (), limit=_REQUIREMENT_LIST_LIMITS["failed_checks"]))
    evidence_classes = _normalize_string_list(
        relevant_evidence_classes or (),
        limit=_REQUIREMENT_LIST_LIMITS["evidence_provenance"],
    )
    if not evidence_classes:
        evidence_classes = _infer_evidence_classes(
            evidence_refs=evidence_refs,
            failed_checks=failed_checks,
            artifact_paths=relevant_artifact_paths,
            verifier_refs=relevant_verifier_refs,
        )
    return compute_relevant_evidence_version(
        requirement=_clean_text(blocker.get("requirement")),
        evidence_refs=evidence_refs,
        failed_checks=failed_checks,
        artifact_paths=relevant_artifact_paths,
        verifier_refs=relevant_verifier_refs,
        reason_codes=blocker.get("reason_codes", ()),
        evidence_classes=evidence_classes,
    )


def _has_relevant_new_evidence(
    ledger: Mapping[str, Any],
    *,
    blocker: Mapping[str, Any],
    relevant_evidence_refs: Iterable[str] | None,
    relevant_failed_checks: Iterable[str] | None,
    relevant_artifact_paths: Iterable[str] | None,
    relevant_verifier_refs: Iterable[str] | None,
    relevant_evidence_classes: Iterable[str] | None,
) -> bool:
    from harness.aether2.traces.verifier import _infer_evidence_classes

    new_version = _current_blocker_evidence_version(
        ledger,
        blocker=blocker,
        relevant_evidence_refs=relevant_evidence_refs,
        relevant_failed_checks=relevant_failed_checks,
        relevant_artifact_paths=relevant_artifact_paths,
        relevant_verifier_refs=relevant_verifier_refs,
        relevant_evidence_classes=relevant_evidence_classes,
    )
    if new_version == _clean_text(blocker.get("evidence_version_last_evaluated")):
        return False
    required_class = _clean_text(blocker.get("required_evidence_class"))
    if required_class:
        evidence_classes = _normalize_string_list(
            relevant_evidence_classes or (),
            limit=_REQUIREMENT_LIST_LIMITS["evidence_provenance"],
        )
        if not evidence_classes:
            evidence_classes = _infer_evidence_classes(
                evidence_refs=relevant_evidence_refs,
                failed_checks=relevant_failed_checks,
                artifact_paths=relevant_artifact_paths,
                verifier_refs=relevant_verifier_refs,
            )
        if required_class not in evidence_classes:
            return False
    direct_relevant_checks = _normalize_string_list(
        relevant_failed_checks or (),
        limit=_REQUIREMENT_LIST_LIMITS["failed_checks"],
    )
    direct_relevant_verifier_refs = _normalize_string_list(
        relevant_verifier_refs or (),
        limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"],
    )
    if direct_relevant_checks or direct_relevant_verifier_refs:
        return True
    evidence_texts = [
        *_normalize_string_list(relevant_evidence_refs or (), limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"]),
        *direct_relevant_checks,
        *_normalize_string_list(relevant_artifact_paths or (), limit=_REQUIREMENT_LIST_LIMITS["evidence_refs"]),
    ]
    if not evidence_texts:
        from harness.aether2.traces.evidence_ledger import _requirement_map

        entry = _requirement_map(ledger).get(_clean_text(blocker.get("requirement"))) or {}
        seen = set(
            _normalize_string_list(
                blocker.get("rejected_evidence_refs", ()),
                limit=_BLOCKER_LIST_LIMITS["rejected_evidence_refs"],
            )
        )
        evidence_texts = [
            *[item for item in list(entry.get("evidence_refs", []) or []) if item not in seen],
            *[item for item in list(entry.get("failed_checks", []) or []) if item not in seen],
        ]
    if not evidence_texts:
        return False
    return _evidence_overlaps_blocker(blocker, evidence_texts)


def _evidence_overlaps_blocker(blocker: Mapping[str, Any], evidence_texts: Iterable[str]) -> bool:
    anchor_tokens = _token_set(
        [
            blocker.get("requirement"),
            blocker.get("insufficiency_reason"),
            *list(blocker.get("required_next_evidence", []) or []),
            *list(blocker.get("rejected_evidence_refs", []) or []),
        ]
    )
    if not anchor_tokens:
        return False
    for text in evidence_texts:
        evidence_tokens = _token_set([text])
        if evidence_tokens & anchor_tokens:
            return True
    return False


def _token_set(values: Iterable[Any]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for token in _TOKEN_RE.findall(_clean_text(value).lower()):
            if len(token) <= 2 or token in _RELEVANCE_STOPWORDS:
                continue
            tokens.add(token)
    return tokens
