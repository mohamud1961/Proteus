"""Append-only context epochs and provider-cache evidence.

This is the canonical, model-free context primitive.  The stable prefix is
content addressed and never changes during ordinary progress; dynamic events
are appended to an epoch and are reset only by explicit compaction.  Large
payloads are stored in :class:`OutputHandleStore` and represented by a small
lossless handle in rendered context.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from hashlib import sha256
import json
from typing import Any, Mapping

from .receipts import OutputHandleStore, ReceiptStore


STABLE_SECTION_ORDER = (
    "kernel_constitution",
    "fixed_tool_schema",
    "task_contract",
    "envmap",
    "architect_solver_prompt",
    "compiled_workbench",
    "response_protocol",
)


def _normalise(value: Any) -> Any:
    if is_dataclass(value):
        return _normalise(asdict(value))
    if hasattr(value, "to_payload") and callable(value.to_payload):
        return _normalise(value.to_payload())
    if hasattr(value, "as_payload") and callable(value.as_payload):
        return _normalise(value.as_payload())
    if isinstance(value, Mapping):
        return {str(key): _normalise(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalise(item) for item in sorted(value, key=str)] if isinstance(value, (set, frozenset)) else [_normalise(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def common_prefix_bytes(left: bytes, right: bytes) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


@dataclass(frozen=True)
class StablePrefix:
    text: str
    sha256: str
    bytes: int
    section_hashes: Mapping[str, str]
    envmap_version: int
    envmap_sha256: str


@dataclass
class ContextEpoch:
    config_version: int
    epoch_id: int
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    events: list[Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.checkpoint = deepcopy(dict(self.checkpoint))
        self.events = [deepcopy(dict(event)) for event in self.events]

    def append(self, event: Mapping[str, Any]) -> None:
        if not isinstance(event, Mapping):
            raise TypeError("context event must be a mapping")
        self.events.append(deepcopy(dict(event)))

    def render(self) -> str:
        lines = [
            "[context_epoch]",
            canonical_json(
                {
                    "schema_version": "context_epoch.v2",
                    "config_version": self.config_version,
                    "epoch_id": self.epoch_id,
                    "checkpoint": self.checkpoint,
                }
            ),
        ]
        for ordinal, event in enumerate(self.events, start=1):
            lines.extend((f"[event_{ordinal:04d}]", canonical_json(event)))
        return "\n".join(lines)

    def should_compact(self, *, max_events: int, max_dynamic_bytes: int) -> bool:
        return len(self.events) >= max_events or len(self.render().encode("utf-8")) >= max_dynamic_bytes

    def compact(self, checkpoint: Mapping[str, Any] | None = None) -> "ContextEpoch":
        merged = deepcopy(dict(self.checkpoint))
        if checkpoint is not None:
            merged.update(deepcopy(dict(checkpoint)))
        return ContextEpoch(
            config_version=self.config_version,
            epoch_id=self.epoch_id + 1,
            checkpoint=merged,
        )


@dataclass(frozen=True)
class CacheManifest:
    stable_prefix_sha256: str
    stable_prefix_bytes: int
    request_bytes: int
    common_prefix_bytes_with_previous: int
    exact_reusable_share: float
    stable_share: float
    config_version: int
    epoch_id: int
    event_count: int
    provider_input_tokens: int | None = None
    provider_cached_tokens: int | None = None
    provider_cache_share: float | None = None
    provider_cache_write_tokens: int | None = None
    provider_cache_write_share: float | None = None
    envmap_version: int = 1
    envmap_sha256: str = ""


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _provider_usage(usage: Mapping[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    if not isinstance(usage, Mapping):
        return None, None, None
    input_tokens = _nonnegative_int(usage.get("input_tokens", usage.get("prompt_tokens")))
    details = usage.get("input_tokens_details", usage.get("prompt_tokens_details", {}))
    if not isinstance(details, Mapping):
        details = {}
    return input_tokens, _nonnegative_int(details.get("cached_tokens")), _nonnegative_int(details.get("cache_write_tokens"))


def build_stable_prefix(
    *,
    kernel_constitution: str,
    fixed_tool_schema: Mapping[str, Any],
    task_contract: Any,
    envmap: Any,
    compiled: Any = None,
    compiled_workbench: Any = None,
    architect_solver_prompt: str | None = None,
    response_protocol: Mapping[str, Any] | None = None,
) -> StablePrefix:
    """Build a deterministic stable prefix from trusted, config-derived data."""
    if compiled_workbench is None and compiled is not None:
        compiled_workbench = getattr(compiled, "solver_config_json", compiled)
    if architect_solver_prompt is None and compiled is not None:
        config = getattr(compiled, "config", None)
        architect_solver_prompt = getattr(config, "solver_system_prompt", "")
    env_payload = envmap.to_payload() if hasattr(envmap, "to_payload") else envmap
    env_version = int(getattr(envmap, "version", 1))
    env_hash = str(getattr(envmap, "sha256", ""))
    sections = {
        "kernel_constitution": kernel_constitution,
        "fixed_tool_schema": fixed_tool_schema,
        "task_contract": task_contract,
        "envmap": env_payload,
        "architect_solver_prompt": architect_solver_prompt or "",
        "compiled_workbench": compiled_workbench or {},
        "response_protocol": response_protocol or {},
    }
    lines: list[str] = []
    section_hashes: dict[str, str] = {}
    for index, name in enumerate(STABLE_SECTION_ORDER):
        payload = canonical_json(sections[name])
        lines.extend((f"[{index:02d}_{name}]", payload))
        section_hashes[name] = sha256(payload.encode("utf-8")).hexdigest()
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    return StablePrefix(
        text=text,
        sha256=sha256(encoded).hexdigest(),
        bytes=len(encoded),
        section_hashes=section_hashes,
        envmap_version=env_version,
        envmap_sha256=env_hash or sha256(canonical_json(env_payload).encode("utf-8")).hexdigest(),
    )


def build_checkpoint(
    selections: Any = (),
    *,
    active_findings: list[Mapping[str, Any]] | None = None,
    latest_result: Mapping[str, Any] | None = None,
    current_state: Mapping[str, Any] | None = None,
    dynamic_world_state: Mapping[str, Any] | None = None,
    retrieval_handles: list[str] | None = None,
) -> Mapping[str, Any]:
    rows = []
    handles = list(retrieval_handles or [])
    for item in selections or ():
        row = _normalise(item)
        rows.append(row)
        if isinstance(row, Mapping) and row.get("retrieval_handle"):
            handles.append(str(row["retrieval_handle"]))
    return {
        "selected_context": rows,
        "active_findings": deepcopy(list(active_findings or [])),
        "latest_result": deepcopy(dict(latest_result)) if latest_result is not None else None,
        "retrieval_handles": list(dict.fromkeys(handles)),
        "current_state": deepcopy(dict(current_state or dynamic_world_state or {})),
        "dynamic_world_state": deepcopy(dict(dynamic_world_state or current_state or {})),
    }


def render_request(prefix: StablePrefix, epoch: ContextEpoch) -> bytes:
    return (prefix.text + "\n" + epoch.render()).encode("utf-8")


def cache_manifest(
    prefix: StablePrefix,
    epoch: ContextEpoch,
    *,
    previous_request: bytes | None = None,
    provider_usage: Mapping[str, Any] | None = None,
) -> CacheManifest:
    request = render_request(prefix, epoch)
    common = common_prefix_bytes(previous_request, request) if previous_request is not None else 0
    input_tokens, cached_tokens, write_tokens = _provider_usage(provider_usage)
    read_share = cached_tokens / input_tokens if input_tokens and cached_tokens is not None and cached_tokens <= input_tokens else None
    write_share = (
        write_tokens / input_tokens
        if input_tokens and write_tokens is not None and write_tokens <= input_tokens
        else None
    )
    return CacheManifest(
        stable_prefix_sha256=prefix.sha256,
        stable_prefix_bytes=prefix.bytes,
        request_bytes=len(request),
        common_prefix_bytes_with_previous=common,
        exact_reusable_share=(common / len(request)) if request else 0.0,
        stable_share=(prefix.bytes / len(request)) if request else 0.0,
        config_version=epoch.config_version,
        epoch_id=epoch.epoch_id,
        event_count=len(epoch.events),
        provider_input_tokens=input_tokens,
        provider_cached_tokens=cached_tokens,
        provider_cache_share=read_share,
        provider_cache_write_tokens=write_tokens,
        provider_cache_write_share=write_share,
        envmap_version=prefix.envmap_version,
        envmap_sha256=prefix.envmap_sha256,
    )


def _compact_large(value: Any, handles: OutputHandleStore, *, max_inline_chars: int) -> Any:
    # Bytes are never JSON-inlineable, regardless of size.  Store them behind
    # an exact handle so binary tool output remains renderable and lossless.
    if isinstance(value, bytes):
        handle = handles.put(value, kind="context_event_output")
        return {"output_handle": handle, "bytes": len(value), "sha256": sha256(value).hexdigest()}
    if isinstance(value, str) and len(value) > max_inline_chars:
        handle = handles.put(value, kind="context_event_output")
        return {"output_handle": handle, "chars": len(value), "sha256": sha256(value.encode("utf-8", "surrogateescape")).hexdigest()}
    if isinstance(value, Mapping):
        # Runtime action receipts may already contain a compact, exact
        # descriptor for a large result.  Do not walk that descriptor again:
        # in particular, replacing its ``output_handle`` with a second handle
        # would make the context point at metadata rather than the original
        # payload (and can multiply receipt rows under a small inline limit).
        if value.get("type") == "large_action_result" and isinstance(value.get("output_handle"), str):
            return deepcopy(dict(value))
        return {str(key): _compact_large(item, handles, max_inline_chars=max_inline_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact_large(item, handles, max_inline_chars=max_inline_chars) for item in value]
    if isinstance(value, tuple):
        return [_compact_large(item, handles, max_inline_chars=max_inline_chars) for item in value]
    return deepcopy(value)


class ContextManager:
    def __init__(
        self,
        prefix: StablePrefix,
        epoch: ContextEpoch,
        *,
        max_events: int = 64,
        max_dynamic_bytes: int = 64_000,
        receipts: ReceiptStore | None = None,
        max_inline_chars: int = 2048,
    ) -> None:
        if max_events <= 0 or max_dynamic_bytes <= 0:
            raise ValueError("context compaction limits must be positive")
        self.prefix = prefix
        self.epoch = epoch
        self.max_events = max_events
        self.max_dynamic_bytes = max_dynamic_bytes
        self.max_inline_chars = max_inline_chars
        self.output_handles = OutputHandleStore(receipts)
        self.previous_request: bytes | None = None

    def current_request(self, provider_usage: Mapping[str, Any] | None = None) -> tuple[bytes, CacheManifest]:
        request = render_request(self.prefix, self.epoch)
        manifest = cache_manifest(self.prefix, self.epoch, previous_request=self.previous_request, provider_usage=provider_usage)
        self.previous_request = request
        return request, manifest

    request = current_request

    def append_event(self, event: Mapping[str, Any]) -> None:
        self.epoch.append(_compact_large(event, self.output_handles, max_inline_chars=self.max_inline_chars))

    def compact_if_needed(self, checkpoint: Mapping[str, Any] | None = None) -> bool:
        if not self.epoch.should_compact(max_events=self.max_events, max_dynamic_bytes=self.max_dynamic_bytes):
            return False
        self.epoch = self.epoch.compact(checkpoint)
        return True

    def compact(self, checkpoint: Mapping[str, Any] | None = None) -> None:
        self.epoch = self.epoch.compact(checkpoint)

    def retrieve_output(self, handle: str) -> str | bytes:
        return self.output_handles.get(handle)
