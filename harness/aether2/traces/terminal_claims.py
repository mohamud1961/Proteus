"""Terminal-claim normalization helpers for HarnessEng Aether-2.

Responsible for normalizing, sorting, and ID-stamping the
``terminal_claims`` section of the evidence ledger.

All public names are re-exported verbatim by
``harness.aether2.traces.delta`` so existing import sites
continue to work without change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from harness.aether2.traces._text_utils import (
    _clean_text,
    _coerce_int,
    _normalize_string_list,
    _read_attr,
)

__all__ = [
    "record_terminal_claim",
]

# ---------------------------------------------------------------------------
# Limits (kept in sync with evidence_ledger.py)
# ---------------------------------------------------------------------------

_MAX_TERMINAL_CLAIMS = 24
_TERMINAL_CLAIM_LIST_LIMITS = {
    "requirements": 8,
    "evidence_refs": 6,
    "evidence_provenance": 6,
    "known_limitations": 6,
    "attempts": 6,
    "missing_external_state": 6,
    "recommended_next_evidence": 6,
}


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def record_terminal_claim(
    ledger: Mapping[str, Any] | None,
    *,
    claim: Mapping[str, Any],
    outcome: str,
    step: int | None = None,
    raw_log_path: str | None = None,
) -> dict[str, Any]:
    """Persist a generic terminal claim for later verifier/loop handoff.

    The loop should call this from its terminal claim tools once the claim is
    parsed into a structured mapping.  The helper is intentionally claim-shape
    only: it records the claim, but it does not promote the claim to proof.
    """
    from harness.aether2.traces.evidence_ledger import compact_evidence_ledger

    normalized = compact_evidence_ledger(ledger)
    claims = list(normalized.get("terminal_claims", []) or [])
    claims.append(
        _normalize_terminal_claim(
            claim,
            outcome=outcome,
            step=step,
            raw_log_path=raw_log_path,
        )
    )
    normalized["terminal_claims"] = _normalize_terminal_claims(claims, limit=_MAX_TERMINAL_CLAIMS)
    return compact_evidence_ledger(normalized)


# ---------------------------------------------------------------------------
# Internal helpers (also called from evidence_ledger.py)
# ---------------------------------------------------------------------------


def _normalize_terminal_claims(values: Iterable[Any], *, limit: int) -> list[dict[str, Any]]:
    normalized = [_normalize_terminal_claim(value) for value in values or () if isinstance(value, Mapping)]
    normalized.sort(key=_terminal_claim_sort_key)
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit]


def _normalize_terminal_claim(
    value: Mapping[str, Any],
    *,
    outcome: str | None = None,
    step: int | None = None,
    raw_log_path: str | None = None,
) -> dict[str, Any]:
    claim_kind = _normalize_terminal_claim_kind(outcome or value.get("outcome") or value.get("claim_kind"))
    summary = _clean_text(value.get("summary")) or _clean_text(value.get("blocker")) or _clean_text(value.get("claim"))
    checks = _normalize_string_list(value.get("checks", ()), limit=_TERMINAL_CLAIM_LIST_LIMITS["evidence_refs"])
    evidence_refs = _normalize_string_list(
        [
            *checks,
            *(_normalize_string_list(value.get("evidence", ()), limit=_TERMINAL_CLAIM_LIST_LIMITS["evidence_refs"])),
        ],
        limit=_TERMINAL_CLAIM_LIST_LIMITS["evidence_refs"],
    )
    requirements = _normalize_terminal_requirement_claims(value.get("requirements", ()))
    known_limitations = _normalize_string_list(
        value.get("known_limitations", ()),
        limit=_TERMINAL_CLAIM_LIST_LIMITS["known_limitations"],
    )
    attempts = _normalize_string_list(value.get("attempts", ()), limit=_TERMINAL_CLAIM_LIST_LIMITS["attempts"])
    missing_external_state = _normalize_string_list(
        value.get("missing_external_state", ()),
        limit=_TERMINAL_CLAIM_LIST_LIMITS["missing_external_state"],
    )
    recommended_next_evidence = _normalize_string_list(
        value.get("recommended_next_evidence", ()),
        limit=_TERMINAL_CLAIM_LIST_LIMITS["recommended_next_evidence"],
    )
    evidence_provenance = _normalize_string_list(
        value.get("evidence_provenance", ()),
        limit=_TERMINAL_CLAIM_LIST_LIMITS["evidence_provenance"],
    )
    mapping_status = "structured" if requirements and all(item["claim_quality"] == "structured" for item in requirements) else "weak"
    if claim_kind == "blocked" and summary and attempts and missing_external_state and recommended_next_evidence and evidence_refs:
        mapping_status = "structured"
    blocker = _clean_text(value.get("blocker"))
    claimed_boundary = _clean_text(value.get("claimed_boundary"))
    claim_payload = {
        "claim_kind": claim_kind,
        "summary": summary,
        "requirements": requirements,
        "checks": checks,
        "evidence_refs": evidence_refs,
        "known_limitations": known_limitations,
        "attempts": attempts,
        "missing_external_state": missing_external_state,
        "recommended_next_evidence": recommended_next_evidence,
        "evidence_provenance": evidence_provenance,
        "blocker": blocker,
        "claimed_boundary": claimed_boundary,
        "mapping_status": mapping_status,
        "raw_log_path": _clean_text(raw_log_path) or _clean_text(value.get("raw_log_path")),
        "step": _choose_step(step, value.get("step")),
    }
    claim_payload["claim_id"] = _build_terminal_claim_id(claim_payload)
    return claim_payload


def _normalize_terminal_requirement_claims(values: Iterable[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for value in values or ():
        if not isinstance(value, Mapping):
            continue
        requirement = _clean_text(_read_attr(value, "requirement"))
        if not requirement:
            continue
        from harness.aether2.traces.evidence_ledger import _requirement_id

        requirement_id = _clean_text(_read_attr(value, "requirement_id")) or _requirement_id(requirement)
        check = _clean_text(_read_attr(value, "check"))
        observation_ref = _clean_text(_read_attr(value, "observation_ref"))
        claimed_boundary = _clean_text(_read_attr(value, "claimed_boundary"))
        known_limitations = _normalize_string_list(
            _read_attr(value, "known_limitations", ()) or (),
            limit=_TERMINAL_CLAIM_LIST_LIMITS["known_limitations"],
        )
        claim_quality = "structured" if check or observation_ref or claimed_boundary or known_limitations else "weak"
        normalized.append(
            {
                "requirement": requirement,
                "requirement_id": requirement_id,
                "check": check,
                "observation_ref": observation_ref,
                "claimed_boundary": claimed_boundary,
                "known_limitations": known_limitations,
                "claim_quality": claim_quality,
            }
        )
    normalized.sort(key=lambda item: (item["requirement"], item["requirement_id"]))
    return normalized[: _TERMINAL_CLAIM_LIST_LIMITS["requirements"]]


def _normalize_terminal_claim_kind(value: Any) -> str:
    text = _clean_text(value).lower()
    if text in {"task_done", "done", "completion", "completed", "task_completion"}:
        return "completion"
    if text in {"task_blocked", "blocked", "unresolved", "report_unresolved"}:
        return "blocked"
    if "block" in text:
        return "blocked"
    return "completion"


def _build_terminal_claim_id(claim: Mapping[str, Any]) -> str:
    payload = {
        "claim_kind": _clean_text(claim.get("claim_kind")),
        "summary": _clean_text(claim.get("summary")),
        "requirements": claim.get("requirements", ()),
        "checks": claim.get("checks", ()),
        "evidence_refs": claim.get("evidence_refs", ()),
        "known_limitations": claim.get("known_limitations", ()),
        "attempts": claim.get("attempts", ()),
        "missing_external_state": claim.get("missing_external_state", ()),
        "recommended_next_evidence": claim.get("recommended_next_evidence", ()),
        "blocker": _clean_text(claim.get("blocker")),
        "claimed_boundary": _clean_text(claim.get("claimed_boundary")),
        "step": _coerce_int(claim.get("step")),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"claim_{digest}"


def _terminal_claim_sort_key(claim: Mapping[str, Any]) -> tuple[int, str, str]:
    kind_rank = {"blocked": 0, "completion": 1}.get(str(claim.get("claim_kind", "completion")), 2)
    step = _coerce_int(claim.get("step"))
    return (
        kind_rank,
        step if step is not None else 10**9,
        _clean_text(claim.get("claim_id")),
    )


def _choose_step(preferred: Any, fallback: Any) -> int | None:
    from harness.aether2.traces.evidence_ledger import _normalize_requirement_status  # noqa: F401 — side-effect free

    preferred_int = _coerce_int(preferred)
    if preferred_int is not None:
        return preferred_int
    return _coerce_int(fallback)
