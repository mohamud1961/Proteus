"""Failure-family accumulation helpers for the evidence ledger.

A failure family is a named category of repeated verification failure.
These helpers are extracted from evidence_ledger.py to keep that module
under 500 LOC.  They are purely mechanical — no domain heuristics.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from harness.aether2.traces._text_utils import _clean_text, _coerce_int

__all__ = [
    "_record_failure_family",
    "_normalize_failure_families",
]

_MAX_FAILURE_FAMILIES = 8


def _record_failure_family(ledger: dict[str, Any], *, family: str, evidence_ref: str | None) -> None:
    text = _clean_text(family)
    if not text:
        return
    current = list(ledger.get("repeated_failure_families", []) or [])
    current.append(
        {
            "family": text,
            "count": 1,
            "last_evidence_ref": _clean_text(evidence_ref),
        }
    )
    ledger["repeated_failure_families"] = _normalize_failure_families(current, limit=_MAX_FAILURE_FAMILIES)


def _normalize_failure_families(values: Iterable[Any], *, limit: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for value in values or ():
        if isinstance(value, Mapping):
            family = _clean_text(value.get("family"))
            count = _coerce_int(value.get("count")) or 1
            last_ref = _clean_text(value.get("last_evidence_ref"))
        else:
            family = _clean_text(value)
            count = 1
            last_ref = ""
        if not family:
            continue
        if family not in merged:
            merged[family] = {"family": family, "count": 0, "last_evidence_ref": ""}
        merged[family]["count"] += max(1, count)
        if last_ref:
            merged[family]["last_evidence_ref"] = last_ref
    ranked = sorted(merged.values(), key=lambda item: (-item["count"], item["family"]))
    return ranked[:limit]
