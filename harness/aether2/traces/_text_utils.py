"""Shared text-normalization utilities for the traces package.

All helpers here are side-effect-free and contain no domain logic —
they exist solely to de-duplicate string cleaning, coercion, and
list-deduplication that every other traces sub-module needs.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

__all__ = [
    "_TOKEN_RE",
    "_RELEVANCE_STOPWORDS",
    "_clean_text",
    "_read_attr",
    "_coerce_int",
    "_normalize_string_list",
    "_append_capped",
    # Evidence normalization (no domain logic, just value coercion)
    "_normalize_requirement_status",
    "_normalize_evidence_strength",
    # Evidence ref builders (pure text construction)
    "_build_observation_ref",
    "_build_check_ref",
    "_failure_family_from_check",
]

_VALID_REQUIREMENT_STATUSES = {"unproven", "partial", "proven", "contradicted"}
_VALID_EVIDENCE_STRENGTHS = {"none", "weak", "moderate", "strong"}

_TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")

_RELEVANCE_STOPWORDS = {
    "a",
    "after",
    "already",
    "an",
    "and",
    "artifact",
    "artifacts",
    "be",
    "check",
    "confirmed",
    "direct",
    "evidence",
    "for",
    "fresh",
    "from",
    "in",
    "is",
    "it",
    "log",
    "of",
    "or",
    "proof",
    "ref",
    "repair",
    "requirement",
    "rerun",
    "step",
    "the",
    "to",
    "verifier",
    "visible",
    "with",
    "would",
}


def _clean_text(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        return ""
    return " ".join(text.split())


def _read_attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_string_list(values: Iterable[Any], *, limit: int) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = _clean_text(value)
        if not text or text in seen:
            continue
        cleaned.append(text)
        seen.add(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def _append_capped(values: list[str], item: str | None, *, limit: int) -> list[str]:
    text = _clean_text(item)
    if not text:
        return list(values)
    combined = [*values, text]
    return _normalize_string_list(combined, limit=limit)


# ---------------------------------------------------------------------------
# Evidence-value normalization (no domain logic, pure value coercion)
# ---------------------------------------------------------------------------


def _normalize_requirement_status(value: Any, *, default: str) -> str:
    text = _clean_text(value).lower()
    if text == "satisfied":
        text = "proven"
    elif text == "unsatisfied":
        text = "contradicted"
    elif text == "unverifiable":
        text = "unproven"
    if text in _VALID_REQUIREMENT_STATUSES:
        return text
    return default


def _normalize_evidence_strength(value: Any, *, default: str) -> str:
    text = _clean_text(value).lower()
    if text in _VALID_EVIDENCE_STRENGTHS:
        return text
    return default


# ---------------------------------------------------------------------------
# Evidence ref builders (pure text construction from structured args)
# ---------------------------------------------------------------------------


def _build_observation_ref(
    *,
    tool_name: str,
    step: int | None,
    exit_code: int | None,
    raw_log_path: str | None,
    artifact_paths: list[str],
    note: str | None,
) -> str:
    parts = [f"tool={_clean_text(tool_name) or 'unknown'}"]
    if step is not None:
        parts.append(f"step={step}")
    if exit_code is not None:
        parts.append(f"exit={exit_code}")
    if _clean_text(raw_log_path):
        parts.append(f"log={_clean_text(raw_log_path)}")
    if artifact_paths:
        parts.append(f"artifacts={','.join(artifact_paths)}")
    if _clean_text(note):
        parts.append(f"note={_clean_text(note)}")
    return " ".join(parts)


def _build_check_ref(
    *,
    command: str,
    step: int | None,
    exit_code: int | None,
    raw_log_path: str | None,
    reason_code: str | None,
    error_kind: str | None,
    timed_out: bool,
) -> str:
    parts = [f"check={command or '<unknown>'}"]
    if step is not None:
        parts.append(f"step={step}")
    if exit_code is not None:
        parts.append(f"exit={exit_code}")
    if timed_out:
        parts.append("timed_out=true")
    if reason_code:
        parts.append(f"reason={reason_code}")
    if error_kind:
        parts.append(f"kind={error_kind}")
    if _clean_text(raw_log_path):
        parts.append(f"log={_clean_text(raw_log_path)}")
    return " ".join(parts)


def _failure_family_from_check(*, reason_code: str | None, timed_out: bool, exit_code: int | None) -> str:
    if timed_out:
        return "check_timeout"
    if reason_code:
        return f"check_reason:{reason_code}"
    if exit_code not in (None, 0):
        return "check_exit_nonzero"
    return "check_failure"
