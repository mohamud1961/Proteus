"""Redaction helper for the trace/verify layer.

Extracted from the kernel Layer-2 audit so the runtime does not depend on the
historical kernel modules (which now live under ``variants/harness``).
"""

from __future__ import annotations

from typing import Any


def _clean_hidden_refs(data: Any) -> Any:
    """Recursively removes keys containing words like expected, hidden, secret, grader, ground_truth from dictionaries."""
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            k_lower = k.lower()
            if any(word in k_lower for word in ("expected", "hidden", "secret", "grader", "ground_truth")):
                continue
            cleaned[k] = _clean_hidden_refs(v)
        return cleaned
    elif isinstance(data, list):
        return [_clean_hidden_refs(item) for item in data]
    return data
