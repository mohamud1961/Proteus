"""Canonical provider-cache key and telemetry helpers."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Mapping


@dataclass(frozen=True)
class ProviderCacheTelemetry:
    input_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_read_share: float | None = None
    cache_write_share: float | None = None


def build_prompt_cache_key(*, deployment: str, role: str, namespace: str) -> str:
    material = "\x00".join((str(deployment), str(role), str(namespace))).encode("utf-8")
    return "aether-next-" + hashlib.sha256(material).hexdigest()[:48]


def _number(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def parse_provider_cache_telemetry(usage: Mapping[str, Any] | None) -> ProviderCacheTelemetry:
    if not isinstance(usage, Mapping):
        return ProviderCacheTelemetry()
    input_tokens = _number(usage.get("input_tokens", usage.get("prompt_tokens")))
    details = usage.get("input_tokens_details", usage.get("prompt_tokens_details", {}))
    if not isinstance(details, Mapping):
        details = {}
    cached = _number(details.get("cached_tokens"))
    writes = _number(details.get("cache_write_tokens"))
    read_share = cached / input_tokens if input_tokens and cached is not None and cached <= input_tokens else None
    write_share = writes / input_tokens if input_tokens and writes is not None and writes <= input_tokens else None
    return ProviderCacheTelemetry(input_tokens, cached, writes, read_share, write_share)
