"""Model-authored Task Operating Contract parsing and prompt helpers.

This module is intentionally narrow and generic. The harness provides the
schema and persistence; the model authors the task-local content from visible
task context.
"""

from __future__ import annotations

from typing import Any, Mapping
import json
import re

TASK_OPERATING_CONTRACT_MARKER = "[TASK_OPERATING_CONTRACT]"

TASK_OPERATING_CONTRACT_REQUEST = "\n".join(
    [
        "Before taking more actions, author a short task operating contract from the visible task only.",
        "Emit one block exactly in this form, then continue normally:",
        "[TASK_OPERATING_CONTRACT]",
        '{"required_final_state":[],"proof_that_counts":[],"proxy_evidence_that_does_not_count":[],"irreversible_or_bulk_actions":[],"real_effect_to_observe":[],"environment_or_tool_discovery":[],"first_evidence_plan":[]}',
        "Keep every list short, concrete, and task-local. Do not use hidden metadata or benchmark terms.",
    ]
)

_CONTRACT_BLOCK_RE = re.compile(
    r"(?s)\[TASK_OPERATING_CONTRACT\]\s*(?:```json\s*)?(?P<body>\{.*?\})(?:\s*```)?"
)

_CONTRACT_KEYS = (
    "required_final_state",
    "proof_that_counts",
    "proxy_evidence_that_does_not_count",
    "irreversible_or_bulk_actions",
    "real_effect_to_observe",
    "environment_or_tool_discovery",
    "first_evidence_plan",
)


def extract_task_operating_contract(text: str) -> dict[str, Any] | None:
    """Return a normalized model-authored operating contract, or None."""

    if TASK_OPERATING_CONTRACT_MARKER not in (text or ""):
        return None
    match = _CONTRACT_BLOCK_RE.search(text or "")
    if match is None:
        return None
    try:
        raw = json.loads(match.group("body"))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, Mapping):
        return None

    normalized: dict[str, Any] = {"source": "model_authored", "schema_version": 1}
    populated = 0
    for key in _CONTRACT_KEYS:
        value = raw.get(key, [])
        if isinstance(value, str):
            items = [_clean_text(value)] if _clean_text(value) else []
        elif isinstance(value, (list, tuple)):
            items = []
            for item in value:
                cleaned = _clean_text(item)
                if cleaned and cleaned not in items:
                    items.append(cleaned)
                if len(items) >= 6:
                    break
        else:
            items = []
        normalized[key] = items
        if items:
            populated += 1
    if populated == 0:
        return None
    return normalized


def has_task_operating_contract(contract: Mapping[str, Any] | None) -> bool:
    if not isinstance(contract, Mapping):
        return False
    return any(bool(contract.get(key)) for key in _CONTRACT_KEYS)


def _clean_text(value: Any) -> str:
    text = " ".join(str(value).split())
    if not text:
        return ""
    return text[:280]


__all__ = [
    "TASK_OPERATING_CONTRACT_MARKER",
    "TASK_OPERATING_CONTRACT_REQUEST",
    "extract_task_operating_contract",
    "has_task_operating_contract",
]
