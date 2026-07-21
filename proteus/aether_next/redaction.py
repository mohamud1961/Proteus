"""Small deterministic redaction helpers for evidence packets."""
from __future__ import annotations

from typing import Any

SECRET_MARKER = "[REDACTED]"

_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "auth",
    "credential",
    "password",
    "secret",
    "token",
)


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def redact_secrets(value: Any) -> Any:
    """Return *value* with obvious secret-bearing fields redacted.

    This is intentionally key-based and deterministic. It does not try to infer
    every secret from arbitrary prose, but it prevents structured payload fields
    named like credentials from being propagated into model-visible packets.
    """
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if is_secret_key(str(key)):
                redacted[key] = SECRET_MARKER
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    return value


def redact_text_with_events(text: str) -> tuple[str, list[dict[str, int | str]]]:
    """Redact obvious secret-like spans from raw model/provider text.

    This is deterministic and logs byte/character ranges in the returned event
    list.  It intentionally targets high-risk structured patterns rather than
    trying to infer every possible secret from arbitrary prose.
    """
    import re

    raw = str(text or "")
    patterns: tuple[tuple[str, str], ...] = (
        (
            "secret_assignment",
            r"(?i)(?<![A-Za-z0-9_])[\"']?(api[_-]?key|token|secret|password|credential|auth)[\"']?\s*[:=]\s*[\"']?([^\s,\"';}]+)",
        ),
        ("bearer_token", r"(?i)\bbearer\s+([A-Za-z0-9._~+\-/]+=*)"),
        ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
    )
    events: list[dict[str, int | str]] = []
    replacements: list[tuple[int, int, str]] = []
    for kind, pattern in patterns:
        flags = re.DOTALL if kind == "private_key_block" else 0
        for match in re.finditer(pattern, raw, flags):
            if kind == "secret_assignment" and match.lastindex and match.lastindex >= 2:
                start, end = match.span(2)
            elif kind == "bearer_token":
                start, end = match.span(1)
            else:
                start, end = match.span(0)
            replacements.append((start, end, kind))
    # Merge overlapping ranges deterministically.
    replacements.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in replacements:
        if not merged or start > merged[-1][1]:
            merged.append((start, end, kind))
        else:
            old_start, old_end, old_kind = merged[-1]
            merged[-1] = (old_start, max(old_end, end), old_kind if old_kind == kind else old_kind + "+" + kind)
    if not merged:
        return raw, []
    parts: list[str] = []
    cursor = 0
    for start, end, kind in merged:
        parts.append(raw[cursor:start])
        parts.append(SECRET_MARKER)
        events.append({"type": kind, "start": start, "end": end, "replacement": SECRET_MARKER})
        cursor = end
    parts.append(raw[cursor:])
    return "".join(parts), events
