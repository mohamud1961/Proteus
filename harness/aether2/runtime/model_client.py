"""Normalized model client wrapper for Aether-2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import time

from harness.aether2.runtime.model_routes import ModelClientError, TRANSIENT_STATUS_CODES, make_model_client_from_route
from harness.aether2.runtime.transcript_repair import (
    RepairEvent,
    repair_tool_call_pairs,
    serialize_parallel_tool_calls,
)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: tuple[dict[str, Any], ...]
    usage: dict[str, int]
    status: str
    raw_response: dict[str, Any]


class Aether2ModelClient:
    """Call the configured provider with native tools and normalized retries."""

    def __init__(
        self,
        model_route: dict[str, Any],
        *,
        max_attempts: int = 3,
        backoff_sec: float = 0.5,
        **client_kwargs: Any,
    ) -> None:
        self.model_route = dict(model_route)
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_sec = max(0.0, float(backoff_sec))
        self._client = make_model_client_from_route(self.model_route, **client_kwargs)
        # Cumulative audit of tool-call/response pairing repairs applied before any
        # request reached the provider. Every model call in a run funnels through
        # this client, so this is the single source of truth for the run.
        self.transcript_repair_events: list[RepairEvent] = []

    @property
    def transcript_repairs(self) -> int:
        return len(self.transcript_repair_events)

    def call(self, messages, tools, *, cache_prefix_len: int) -> ModelResponse:
        # Pre-send tool-call pairing invariant. Guarantees the provider never sees
        # an assistant `tool_calls` without matching `tool` responses (a hard 400),
        # regardless of which upstream branch assembled the transcript. No-op for
        # valid transcripts, so flag-off baseline behaviour is unchanged.
        repair = repair_tool_call_pairs(messages)
        if repair.repaired:
            self.transcript_repair_events.extend(repair.events)
        outgoing = serialize_parallel_tool_calls(repair.messages)
        attempt = 0
        while True:
            attempt += 1
            try:
                # `cache_prefix_len` is an Aether-2 internal hint for prompt-cache
                # accounting; the underlying provider clients (e.g.
                # AzureOpenAIAPIKeyModelClient) forward unrecognized kwargs straight
                # into the request payload, so it must NOT be passed through here.
                raw = self._client.complete(
                    list(outgoing),
                    tools=_tools_for_route(self.model_route, tools),
                )
                return _normalize_response(raw)
            except ModelClientError as exc:
                if exc.status_code not in TRANSIENT_STATUS_CODES or attempt >= self.max_attempts:
                    raise
                time.sleep(self.backoff_sec * attempt)


def _flatten_function_tools(tools: Any) -> list[dict[str, Any]]:
    """Flatten Responses-API-shaped tool specs for the Chat Completions surface.

    `runner.aether2.tools.TOOL_SCHEMAS` (and Aether-2's `call()` signature in
    general) use the Responses-API tool shape:
        {"type": "function", "function": {"name": ..., "parameters": ...}}

    `runner.model_client`'s Chat Completions tool normalizer
    (`_normalize_chat_completions_tools`) expects the flat Chat Completions
    shape:
        {"type": "function", "name": ..., "parameters": ...}

    and silently drops any tool dict that doesn't have a top-level "name"
    (because `_normalize_request_tools` passes dicts with a string "type"
    through unchanged). Without this flattening, no tools reach the model and
    it falls back to narrating actions as plain text instead of calling
    tools. This flattening is a no-op for tool specs that are already flat.
    """
    flattened: list[dict[str, Any]] = []
    for tool in list(tools):
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function_spec = dict(tool["function"])
            flat = {"type": "function", **function_spec}
            flattened.append(flat)
        else:
            flattened.append(dict(tool))
    return flattened


def _tools_for_route(model_route: dict[str, Any], tools: Any) -> list[dict[str, Any]]:
    """Return the tool shape expected by the configured provider route."""
    request_settings = model_route.get("request_settings")
    if (
        isinstance(request_settings, dict)
        and request_settings.get("azure_api_surface") == "v1_responses"
    ):
        return [dict(tool) for tool in list(tools or []) if isinstance(tool, dict)]
    return _flatten_function_tools(tools)


def _normalize_response(raw: dict[str, Any]) -> ModelResponse:
    usage_raw = raw.get("usage")
    usage_dict = dict(usage_raw) if isinstance(usage_raw, dict) else {}
    input_tokens = max(0, _coerce_int(usage_dict.get("input_tokens")))
    cached_input_tokens = max(0, _coerce_int(usage_dict.get("cached_input_tokens")))
    output_tokens = max(0, _coerce_int(usage_dict.get("output_tokens")))
    total_tokens = max(0, _coerce_int(usage_dict.get("total_tokens"))) or (
        input_tokens + output_tokens
    )
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "fresh_input_tokens": max(0, input_tokens - cached_input_tokens),
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    tool_calls_raw = raw.get("tool_calls")
    tool_calls = (
        tuple(dict(tool) for tool in tool_calls_raw if isinstance(tool, dict))
        if isinstance(tool_calls_raw, list)
        else ()
    )
    return ModelResponse(
        text=str(raw.get("text", "")),
        tool_calls=tool_calls,
        usage=usage,
        status=str(raw.get("status", "")),
        raw_response=dict(raw),
    )


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
