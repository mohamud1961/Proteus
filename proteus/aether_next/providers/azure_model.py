"""Azure OpenAI model provider using the Responses API in background mode.

This is the ONLY module allowed to ``import openai``.  It builds a
``ModelCallable`` that the kernel's ``ModelHooks`` layer can consume.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from .model_retry import (
    DEFAULT_BACKOFF_BASE_S,
    DEFAULT_BACKOFF_CAP_S,
    DEFAULT_BACKOFF_MAX_TOTAL_S,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RPM,
    RateLimiter,
    _retry_call,
    get_rate_limiter_for_deployment,
)

try:
    import openai
except ModuleNotFoundError:  # pragma: no cover - provider construction requires dependency.
    openai = None  # type: ignore[assignment]


class AzureModelError(Exception):
    """Raised when the Azure Responses API returns an unrecoverable error."""


class AzureProviderOutputError(AzureModelError):
    """A provider response cannot safely authorise one structured model turn."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


# Background-job error codes (``job.error.code``, an openai
# ``ResponseError``) that represent a transient, Azure-side condition worth
# retrying. Every other code (invalid_prompt, image_*, vector_store_timeout,
# …) is a genuine request problem — retrying it just burns the retry budget
# on a call that will never succeed.
_RETRYABLE_JOB_ERROR_CODES = frozenset({"rate_limit_exceeded", "server_error"})

_JSON_OBJECT_RESPONSE_INSTRUCTION = (
    "Return exactly one valid JSON object. Do not include markdown fences, "
    "prose, or multiple candidate responses."
)


class _JobStatusFailure(Exception):
    """Internal marker: a background job reached a terminal failure status.

    Carries the job's ``error.code`` (e.g. ``"rate_limit_exceeded"``, per
    the ``ResponseError`` shape observed in the 15-agent batch rate-limit
    storm) so :func:`is_retryable_azure_error` can classify it without
    re-parsing the formatted error string. Raised only as the ``__cause__``
    of an :class:`AzureModelError` — never surfaced directly.
    """

    def __init__(self, code: str | None) -> None:
        super().__init__(code or "unknown")
        self.code = code


def is_retryable_azure_error(exc: BaseException) -> bool:
    """Classify an exception from a single Azure model-call attempt as transient.

    Transient (worth retrying): HTTP 429 (rate limit) or 5xx from the SDK,
    an SDK-level connection/timeout error, or a background job that ended
    with a retryable ``error.code`` (``rate_limit_exceeded`` /
    ``server_error``).

    Non-transient (must raise immediately): auth failures, bad requests,
    content filter rejections, and any other 4xx or unrecognized job error
    code. These will never succeed no matter how many times they're retried,
    so retrying them would only waste the retry budget and delay a real
    failure signal.

    Looks at ``exc.__cause__`` first because both HTTP-layer failures
    (``responses.create``/``responses.retrieve`` wrap the raw openai/httpx
    exception via ``raise AzureModelError(...) from exc``) and job-status
    failures (wrapped via ``from _JobStatusFailure(code)``) preserve the
    original signal there; falls back to *exc* itself for anything raised
    without a cause.
    """
    cause = exc.__cause__ if exc.__cause__ is not None else exc

    if isinstance(cause, _JobStatusFailure):
        return cause.code in _RETRYABLE_JOB_ERROR_CODES

    status_code = getattr(cause, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429 or 500 <= status_code < 600

    # SDK connection/timeout errors carry no HTTP status at all but are
    # transient by nature (openai.APITimeoutError subclasses this too).
    if openai is not None and isinstance(cause, openai.APIConnectionError):
        return True

    return False


def _normalize_endpoint(raw: str) -> str:
    """Strip a full Azure endpoint URL down to ``scheme://host``.

    The env var may be ``https://host/openai/responses?api-version=...``;
    the working call uses ``{host}/openai/v1/``.
    """
    value = raw.strip()
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return value.rstrip("/")


def _prompt_cache_mode_from_env() -> str:
    """Return the supported cache-key mode for the Azure Responses route.

    Azure enables prompt caching provider-side.  This route additionally sends
    a stable cache key by default so requests sharing an immutable
    ``instructions`` prefix are more likely to stay cache-affine.  Operators
    can disable only that routing hint with ``AETHER_PROMPT_CACHE_MODE=off``.
    Extended retention is deliberately not exposed here: the active mini
    deployment is safe only with the normal in-memory retention path.
    """
    mode = os.environ.get("AETHER_PROMPT_CACHE_MODE", "stable_prefix").strip().lower()
    if mode not in {"stable_prefix", "off"}:
        raise AzureModelError(
            "AETHER_PROMPT_CACHE_MODE must be 'stable_prefix' or 'off'"
        )
    return mode


def _prompt_cache_namespace_from_env() -> str:
    """Return the bounded, operator-controlled cache affinity namespace.

    This namespace is deliberately independent of task material.  It scopes
    cache routing by an operator-selected protocol generation, while the
    provider itself still decides whether the immutable leading prompt tokens
    match and are cacheable.  It is not evidence of a cache hit.
    """
    namespace = os.environ.get("AETHER_PROMPT_CACHE_NAMESPACE", "aether-next-v1").strip()
    if not namespace or len(namespace) > 128:
        raise AzureModelError(
            "AETHER_PROMPT_CACHE_NAMESPACE must be non-empty and at most 128 characters"
        )
    return namespace


def _stable_prompt_cache_key(*, deployment: str, role: str, namespace: str) -> str:
    """Build an opaque task-independent cache-routing shard.

    The key contains only deployment, fixed role, and an operator namespace.
    It never includes task prompt, EnvMap, architecture/config summaries,
    receipts, or dynamic Responses input.  This avoids needless per-task key
    partitioning without claiming cross-task prefix/cache reuse.
    """
    material = "\x00".join((deployment, role, namespace)).encode("utf-8")
    # Responses prompt_cache_key is capped at 64 characters.  The fixed
    # namespace prefix plus 48 hex chars remains deterministic and leaves
    # ample collision resistance for a routing shard.
    return "aether-next-" + hashlib.sha256(material).hexdigest()[:48]


def _usage_field(value: Any, name: str, default: Any = None) -> Any:
    """Read *name* from either an SDK object or a dict-shaped test payload."""
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _usage_telemetry(response: Any) -> dict[str, Any]:
    """Extract provider-reported usage without turning absence into zero.

    A missing usage object or cached-token field is *unmeasured*, not a cache
    miss.  This distinction is essential for later cost/result analysis.
    """
    usage = _usage_field(response, "usage")
    if usage is None:
        return {
            "usage_status": "omitted",
            "cache_metrics_status": "unmeasured",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        }
    input_details = _usage_field(usage, "input_tokens_details", {}) or {}
    output_details = _usage_field(usage, "output_tokens_details", {}) or {}
    missing = object()
    cached = _usage_field(input_details, "cached_tokens", missing)
    return {
        "usage_status": "reported",
        "cache_metrics_status": "reported" if cached is not missing else "unmeasured",
        "input_tokens": _usage_field(usage, "input_tokens"),
        "output_tokens": _usage_field(usage, "output_tokens"),
        "total_tokens": _usage_field(usage, "total_tokens"),
        "cached_input_tokens": None if cached is missing else cached,
        "reasoning_tokens": _usage_field(output_details, "reasoning_tokens"),
    }


def _split_messages(messages: list[dict[str, str]]) -> tuple[str, str]:
    """Return (instructions, input_text) for the Responses API.

    Guarantees *input_text* is non-empty whenever there is any message
    content, because the Responses API requires a non-empty ``input``.

    Mapping rules:

    * ``role:system`` messages → ``instructions`` (joined with blank lines).
    * All other roles → ``input`` (joined with blank lines).
    * If ``input`` is empty but system messages exist, the **last** system
      message is promoted to ``input`` and the earlier ones stay in
      ``instructions``.  (In the solver flow the appended solver prompt is
      last — this keeps it as the actual ask while the prefix sections
      become standing context.)
    * A single system message with no other content goes entirely into
      ``input`` with a minimal generic instruction.
    * An empty *messages* list returns a safe fallback so the API never
      receives an empty ``input``.
    """
    system_parts: list[str] = []
    input_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append(content)
        else:
            input_parts.append(content)

    instructions = "\n\n".join(system_parts)
    input_text = "\n\n".join(input_parts)

    if input_text:
        # Happy path: non-system messages produced input.
        return instructions or "You are a helpful assistant.", input_text

    if len(system_parts) > 1:
        # All-system case (the solver bug): promote the last system message
        # to input; the rest remain as instructions.
        return "\n\n".join(system_parts[:-1]), system_parts[-1]

    if len(system_parts) == 1:
        # Exactly one system message and nothing else.
        return "You are a helpful assistant.", system_parts[0]

    # No messages at all — return safe minimal values.
    return "You are a helpful assistant.", "Proceed."


def _split_responses_input(
    messages: list[dict[str, str]],
) -> tuple[str, str | list[dict[str, str]]]:
    """Split messages while preserving non-system roles for Responses input.

    ``_split_messages`` remains available for legacy callers and pure string
    tests. Live Responses calls must retain the distinction between a prior
    assistant turn and the user observation that follows it; flattening both
    into one string makes a bounded verifier repair round ambiguous to the
    model.
    """
    system_parts: list[str] = []
    input_messages: list[dict[str, str]] = []
    allowed_roles = {"user", "assistant", "developer"}
    for msg in messages:
        role = str(msg.get("role", "user") or "user")
        content = str(msg.get("content", "") or "")
        if role == "system":
            system_parts.append(content)
            continue
        if role not in allowed_roles:
            role = "user"
        input_messages.append({"role": role, "content": content})

    instructions = "\n\n".join(system_parts)
    if input_messages:
        return instructions or "You are a helpful assistant.", input_messages
    if len(system_parts) > 1:
        return "\n\n".join(system_parts[:-1]), [{
            "role": "user",
            "content": system_parts[-1],
        }]
    if len(system_parts) == 1:
        return "You are a helpful assistant.", [{
            "role": "user",
            "content": system_parts[0],
        }]
    return "You are a helpful assistant.", [{
        "role": "user",
        "content": "Proceed.",
    }]


def _responses_input_text(input_payload: str | list[dict[str, str]]) -> str:
    """Canonical text form used only for telemetry sizes and hashes."""
    if isinstance(input_payload, str):
        return input_payload
    return json.dumps(input_payload, sort_keys=True, separators=(",", ":"))


def _prepare_json_object_instructions(instructions: str) -> str:
    """Make Azure JSON-mode's textual requirement explicit before dispatch.

    Azure rejects ``text.format=json_object`` requests unless the model-facing
    material mentions JSON.  Aether's structured-output boundary is generic,
    so the provider adapter owns this invariant rather than relying on task or
    role prompts to happen to contain that word.  The returned string is used
    for the initial request and every retry of the same logical call.
    """
    prepared = "\n\n".join(part for part in (
        str(instructions).strip(),
        "[structured_output_contract] " + _JSON_OBJECT_RESPONSE_INSTRUCTION,
    ) if part)
    if "json" not in prepared.lower():
        # Fail locally before a provider request can spend budget. This should
        # be unreachable while the constant above remains valid, but protects
        # future edits to the request builder.
        raise AzureModelError(
            "json_object_request_contract_invalid: explicit JSON instruction missing"
        )
    return prepared


def _prepare_json_object_input(
    input_payload: str | list[dict[str, str]],
) -> str | list[dict[str, str]]:
    """Place the JSON-mode contract in Responses ``input`` as well.

    Some Azure Responses routes enforce the textual JSON-mode requirement
    against ``input`` messages specifically, rather than against the separate
    ``instructions`` field.  Keeping the same generic contract in both
    model-visible surfaces makes the request valid for either interpretation
    without relying on incidental wording in a task or role prompt.
    """
    contract_message = {
        "role": "developer",
        "content": "[structured_output_contract] " + _JSON_OBJECT_RESPONSE_INSTRUCTION,
    }
    if isinstance(input_payload, str):
        return contract_message["content"] + "\n\n" + input_payload
    return [contract_message, *input_payload]


def _strict_structured_json(text: str) -> tuple[str, str]:
    """Return canonical JSON plus the exact accepted body.

    Exactly one top-level object is accepted.  One optional enclosing JSON
    fence is permitted; leading/trailing prose, arrays, comments, or a second
    object are rejected by the ordinary strict JSON decoder.
    """
    raw = str(text or "").strip()
    if not raw:
        raise AzureProviderOutputError("provider_empty_assistant_message")
    body = raw
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"} or lines[-1].strip() != "```":
            raise AzureProviderOutputError("provider_invalid_json_fence")
        body = "\n".join(lines[1:-1]).strip()
        if "```" in body:
            raise AzureProviderOutputError("provider_nested_json_fence")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AzureProviderOutputError(
            "provider_structured_output_invalid_json",
            f"line={exc.lineno} column={exc.colno} {exc.msg}",
        ) from exc
    if not isinstance(parsed, dict):
        raise AzureProviderOutputError(
            "provider_structured_output_not_object",
            type(parsed).__name__,
        )
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical, body


def _raw_assistant_messages(response: Any) -> tuple[tuple[dict[str, Any], ...], bool]:
    """Extract assistant message items without consulting response.output_text.

    The boolean reports a mixed executable/tool-call item.  Provider reasoning
    items are harmless metadata and are ignored, while any function/computer
    call mixed with text makes the response ambiguous and fail-closed.
    """
    rows: list[dict[str, Any]] = []
    mixed_call = False
    for index, item in enumerate(_usage_field(response, "output", ()) or ()):
        item_type = str(_usage_field(item, "type", "") or "")
        role = str(_usage_field(item, "role", "") or "")
        if item_type.endswith("_call") or item_type in {
            "function_call", "computer_call", "custom_tool_call",
        }:
            mixed_call = True
        if item_type != "message" or role != "assistant":
            continue
        parts: list[str] = []
        for chunk in _usage_field(item, "content", ()) or ():
            piece = _usage_field(chunk, "text")
            if piece is None:
                piece = _usage_field(chunk, "output_text")
            if piece:
                parts.append(str(piece))
        message = "".join(parts)
        if not message:
            continue
        rows.append({
            "index": index,
            "item_id": str(_usage_field(item, "id", "") or ""),
            "item_type": item_type,
            "text": message,
            "text_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            "text_bytes": len(message.encode("utf-8")),
        })
    return tuple(rows), mixed_call


def canonicalize_structured_output(
    response: Any,
    *,
    allow_verifier_inspection_sequence: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Select one structured assistant message, fail-closed by default.

    The verifier protocol has one narrowly defined multi-message recovery:
    when a response contains a valid read-only inspection envelope followed
    by another distinct JSON message, return the earliest inspection envelope
    so the runtime can execute it and request a fresh verdict.  Later messages
    remain quarantined in the receipt.  All other ambiguity is rejected.
    """
    messages, mixed_call = _raw_assistant_messages(response)
    receipt: dict[str, Any] = {
        "response_id": str(_usage_field(response, "id", "") or ""),
        "raw_provider_status": str(_usage_field(response, "status", "") or ""),
        "extraction_path": "response.output[].message.content[].text",
        "candidate_message_count": len(messages),
        "candidate_hashes": tuple(row["text_sha256"] for row in messages),
        "provider_duplicate_output": False,
    }
    if mixed_call:
        raise AzureProviderOutputError("provider_mixed_message_and_tool_output")
    if not messages:
        raise AzureProviderOutputError("provider_no_assistant_message")
    canonical_rows: list[tuple[dict[str, Any], str]] = []
    for row in messages:
        canonical, _body = _strict_structured_json(str(row["text"]))
        canonical_rows.append((row, canonical))
    canonical_forms = {canonical for _row, canonical in canonical_rows}
    if len(canonical_forms) != 1:
        if allow_verifier_inspection_sequence:
            for selected_index, (source, canonical) in enumerate(canonical_rows):
                if not _is_verifier_inspection_request(canonical):
                    continue
                receipt.update({
                    "canonical_item_index": source["index"],
                    "canonical_item_id": source["item_id"],
                    "canonical_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    "provider_multi_message_recovery": "verifier_inspection_sequence",
                    "quarantined_candidate_hashes": [
                        row["text_sha256"]
                        for row, _candidate in canonical_rows[selected_index + 1:]
                    ],
                })
                return canonical, receipt
        # Keep the ambiguity fail-closed, but retain enough non-content
        # metadata to distinguish a provider transport duplication from a
        # genuinely different model decision.  Never persist model text here.
        detail = json.dumps({
            "candidate_count": len(canonical_rows),
            "candidate_hashes": [row["text_sha256"] for row, _canonical in canonical_rows],
            "candidate_bytes": [row["text_bytes"] for row, _canonical in canonical_rows],
            "candidate_item_ids": [row["item_id"] for row, _canonical in canonical_rows],
            "candidate_top_level_keys": [sorted(json.loads(canonical).keys()) for _row, canonical in canonical_rows],
            "candidate_verdicts": [json.loads(canonical).get("verdict") for _row, canonical in canonical_rows],
        }, sort_keys=True, separators=(",", ":"))
        raise AzureProviderOutputError("multiple_distinct_assistant_outputs", detail)
    source, canonical = canonical_rows[0]
    receipt.update({
        "canonical_item_index": source["index"],
        "canonical_item_id": source["item_id"],
        "canonical_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "provider_duplicate_output": len(messages) > 1,
        "provider_duplicate_semantic_equivalent": len(messages) > 1,
    })
    return canonical, receipt


def _is_verifier_inspection_request(canonical: str) -> bool:
    """Recognize the generic read-only inspection envelope, without task data."""
    try:
        value = json.loads(canonical)
    except json.JSONDecodeError:
        return False
    if not isinstance(value, dict) or value.get("kind") != "inspect":
        return False
    requests = value.get("requests")
    if not isinstance(requests, list) or not requests:
        return False
    return all(isinstance(item, dict) and str(item.get("kind", "")).strip() for item in requests)


def _extract_plain_output_text(response: Any) -> str:
    """Extract one unambiguous plain assistant message for the vision lane."""
    messages, mixed_call = _raw_assistant_messages(response)
    if mixed_call:
        raise AzureProviderOutputError("provider_mixed_message_and_tool_output")
    if not messages:
        raise AzureProviderOutputError("provider_no_assistant_message")
    unique = {str(row["text"]) for row in messages}
    if len(unique) != 1:
        raise AzureProviderOutputError("multiple_distinct_assistant_outputs")
    return str(messages[0]["text"])


def _extract_output_text(response: Any) -> str:
    """Compatibility name for strict structured-output extraction."""
    return canonicalize_structured_output(response)[0]


def make_azure_callable(
    *,
    deployment_env: str,
    key_env: str,
    endpoint_env: str = "AZURE_OPENAI_ENDPOINT",
    effort: str = "medium",
    role: str = "unspecified",
    poll_interval_s: float | None = None,
    poll_timeout_s: float | None = None,
    max_retries: int | None = None,
    backoff_base_s: float | None = None,
    backoff_cap_s: float | None = None,
    backoff_max_total_s: float | None = None,
    max_rpm: float | None = None,
) -> "AzureModelCallable":
    """Build a ``ModelCallable`` backed by an Azure OpenAI deployment.

    Env vars are read at *build time* (not per-call), so a missing var
    raises immediately rather than mid-run.

    The callable uses the Responses API in **background mode**: create
    with ``background=True``, then poll ``retrieve`` until the status
    leaves ``queued`` / ``in_progress``.  This avoids stream-hang issues
    on long generations while remaining correct for short solver turns.

    Transient failures (HTTP 429/5xx, connection errors, or a background
    job that ends with a retryable error code) are retried in place with
    exponential backoff + jitter — see ``providers/model_retry.py``. Every
    retry/backoff knob below falls back to an ``AETHER_MODEL_*`` env var
    when left ``None``, matching the existing ``poll_interval_s`` /
    ``poll_timeout_s`` pattern, and every default preserves prior behavior
    at low volume (a handful of bounded retries) while an optional
    requests-per-minute gate (``max_rpm`` / ``AETHER_MODEL_MAX_RPM``,
    default unlimited) can pace bursts client-side.
    """
    endpoint = _normalize_endpoint(os.environ[endpoint_env])
    deployment = os.environ[deployment_env]
    api_key = os.environ[key_env]
    resolved_poll_interval_s = (
        float(os.environ.get("AETHER_MODEL_POLL_INTERVAL_S", "10"))
        if poll_interval_s is None
        else poll_interval_s
    )
    resolved_poll_timeout_s = (
        float(os.environ.get("AETHER_MODEL_POLL_TIMEOUT_S", "1200"))
        if poll_timeout_s is None
        else poll_timeout_s
    )
    resolved_max_retries = (
        int(os.environ.get("AETHER_MODEL_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)))
        if max_retries is None
        else max_retries
    )
    resolved_backoff_base_s = (
        float(os.environ.get("AETHER_MODEL_BACKOFF_BASE_S", str(DEFAULT_BACKOFF_BASE_S)))
        if backoff_base_s is None
        else backoff_base_s
    )
    resolved_backoff_cap_s = (
        float(os.environ.get("AETHER_MODEL_BACKOFF_CAP_S", str(DEFAULT_BACKOFF_CAP_S)))
        if backoff_cap_s is None
        else backoff_cap_s
    )
    resolved_backoff_max_total_s = (
        float(os.environ.get("AETHER_MODEL_BACKOFF_MAX_TOTAL_S", str(DEFAULT_BACKOFF_MAX_TOTAL_S)))
        if backoff_max_total_s is None
        else backoff_max_total_s
    )
    resolved_max_rpm = (
        float(os.environ.get("AETHER_MODEL_MAX_RPM", str(DEFAULT_MAX_RPM)))
        if max_rpm is None
        else max_rpm
    )

    if openai is None:
        raise AzureModelError("openai package is required for AzureModelCallable")
    client = openai.OpenAI(
        api_key=api_key,
        base_url=f"{endpoint}/openai/v1/",
        timeout=resolved_poll_timeout_s + 60,
        max_retries=2,
    )

    return AzureModelCallable(
        client=client,
        deployment=deployment,
        effort=effort,
        role=role,
        prompt_cache_mode=_prompt_cache_mode_from_env(),
        prompt_cache_namespace=_prompt_cache_namespace_from_env(),
        poll_interval_s=resolved_poll_interval_s,
        poll_timeout_s=resolved_poll_timeout_s,
        max_retries=resolved_max_retries,
        backoff_base_s=resolved_backoff_base_s,
        backoff_cap_s=resolved_backoff_cap_s,
        backoff_max_total_s=resolved_backoff_max_total_s,
        rate_limiter=get_rate_limiter_for_deployment(deployment, resolved_max_rpm),
    )


class AzureModelCallable:
    """A ``ModelCallable`` wrapping Azure OpenAI Responses API (background).

    Transient failures (HTTP 429/5xx, SDK connection errors, or a
    background job that ends with a retryable error code) are retried in
    place with exponential backoff + jitter via ``_retry_call`` — see
    :func:`is_retryable_azure_error` for the exact classification. Every
    other failure (auth, bad request, content filter, an unrecognized job
    error code) raises :class:`AzureModelError` on the first attempt with no
    retry.
    """

    def __init__(
        self,
        *,
        client: openai.OpenAI,
        deployment: str,
        effort: str,
        role: str = "unspecified",
        prompt_cache_mode: str = "stable_prefix",
        prompt_cache_namespace: str = "aether-next-v1",
        poll_interval_s: float,
        poll_timeout_s: float,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
        backoff_max_total_s: float | None = DEFAULT_BACKOFF_MAX_TOTAL_S,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._deployment = deployment
        self._effort = effort
        self._role = role
        if prompt_cache_mode not in {"stable_prefix", "off"}:
            raise ValueError("prompt_cache_mode must be 'stable_prefix' or 'off'")
        self._prompt_cache_mode = prompt_cache_mode
        if not prompt_cache_namespace or len(prompt_cache_namespace) > 128:
            raise ValueError("prompt_cache_namespace must be non-empty and at most 128 characters")
        self._prompt_cache_namespace = prompt_cache_namespace
        self._poll_interval_s = max(1.0, poll_interval_s)
        self._poll_timeout_s = max(30.0, poll_timeout_s)
        self._max_retries = max(0, max_retries)
        self._backoff_base_s = max(0.0, backoff_base_s)
        self._backoff_cap_s = max(0.0, backoff_cap_s)
        self._backoff_max_total_s = backoff_max_total_s
        self._rate_limiter = rate_limiter
        # Injectable for tests only; production callers get real time.sleep
        # / random.random via the defaults above.
        self._retry_sleep = sleep
        self._rand = rand
        self.last_call_telemetry: dict[str, Any] = {}
        self._telemetry_events: list[dict[str, Any]] = []
        self._telemetry_lock = threading.Lock()
        self._next_logical_call_id = 0

    def drain_telemetry(self) -> tuple[dict[str, Any], ...]:
        """Return and clear immutable provider-attempt telemetry receipts.

        Each row has a ``logical_call_id`` and ``attempt_ordinal``.  This
        deliberately retains failed retries as well as completed attempts; it
        is provider telemetry, not a billing estimate.
        """
        with self._telemetry_lock:
            events = tuple(self._telemetry_events)
            self._telemetry_events.clear()
            return events

    def _allocate_logical_call_id(self) -> int:
        with self._telemetry_lock:
            self._next_logical_call_id += 1
            return self._next_logical_call_id

    def _record_attempt(self, event: dict[str, Any]) -> None:
        """Atomically retain one immutable provider-attempt receipt."""
        snapshot = dict(event)
        with self._telemetry_lock:
            self.last_call_telemetry = snapshot
            self._telemetry_events.append(snapshot)

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        return self._call(messages, max_output_tokens=max_output_tokens, telemetry_scope=None)

    def call_with_telemetry_scope(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
        run_id: str,
        task_id: str | None,
    ) -> str:
        """Call with immutable task/run attribution supplied by ModelHooks."""
        return self._call(
            messages,
            max_output_tokens=max_output_tokens,
            telemetry_scope={"run_id": run_id, "task_id": task_id},
        )

    def _call(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int,
        telemetry_scope: dict[str, str | None] | None,
    ) -> str:
        """Send *messages* and return the model's output text.

        Maps the ``messages`` list (with ``role``/``content`` dicts) to the
        Responses API ``instructions`` + ``input`` parameters via
        :func:`_split_messages`, which guarantees a non-empty ``input``, then
        drives one create+poll attempt at a time through ``_retry_call``: a
        transient failure starts a fresh attempt (a new background job —
        a failed job cannot be resumed) after a bounded, jittered backoff;
        a non-transient failure or retry exhaustion propagates the same
        :class:`AzureModelError` a caller would see without retry at all.
        """
        instructions, user_input = _split_responses_input(messages)
        logical_call_id = self._allocate_logical_call_id()
        attempts = 0

        def _attempt() -> str:
            nonlocal attempts
            attempts += 1
            return self._call_once(
                instructions,
                user_input,
                max_output_tokens,
                logical_call_id=logical_call_id,
                attempt_ordinal=attempts,
                telemetry_scope=telemetry_scope,
            )

        return _retry_call(
            _attempt,
            is_retryable=is_retryable_azure_error,
            max_retries=self._max_retries,
            base_s=self._backoff_base_s,
            cap_s=self._backoff_cap_s,
            max_total_s=self._backoff_max_total_s,
            sleep=self._retry_sleep,
            rand=self._rand,
        )

    def preflight_request(self, *, max_output_tokens: int, logical_role: str) -> dict[str, Any]:
        """Describe the native request contract without making a provider call."""
        return {
            "provider": "azure_openai_responses",
            "model": self._deployment,
            "provider_role": self._role,
            "logical_role": logical_role,
            "effort": self._effort,
            "max_output_tokens": int(max_output_tokens),
            "background": True,
            "structured_output_mode": "json_object",
            "explicit_json_instruction": True,
            "json_instruction_in_input": True,
            "certification": "native_json_object_request_contract",
        }

    def _call_once(
        self,
        instructions: str,
        user_input: str | list[dict[str, str]],
        max_output_tokens: int,
        *,
        logical_call_id: int,
        attempt_ordinal: int,
        telemetry_scope: dict[str, str | None] | None,
    ) -> str:
        """Run exactly one create+poll attempt. Never retries internally —
        that's ``__call__``'s job via ``_retry_call``."""
        # Preflight at the exact provider boundary. Each bounded retry enters
        # here independently, so no retry variant can bypass JSON-mode's
        # textual request requirement.
        instructions = _prepare_json_object_instructions(instructions)
        user_input = _prepare_json_object_input(user_input)
        event: dict[str, Any] = {
            "event_kind": "provider_attempt",
            "logical_call_id": logical_call_id,
            "attempt_ordinal": attempt_ordinal,
            "provider": "azure_openai_responses",
            "deployment": self._deployment,
            "role": self._role,
            "status": "in_progress",
            "attempt_phase": "create",
            "instructions_chars": len(instructions),
            "input_chars": len(_responses_input_text(user_input)),
            "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            "input_sha256": hashlib.sha256(
                _responses_input_text(user_input).encode("utf-8")
            ).hexdigest(),
            "max_output_tokens": max_output_tokens,
            "prompt_cache_key_mode": self._prompt_cache_mode,
            "prompt_cache_namespace": self._prompt_cache_namespace,
            "prompt_cache_retention": "in_memory" if self._prompt_cache_mode == "stable_prefix" else None,
            "usage_status": "unmeasured",
            "cache_metrics_status": "unmeasured",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        }
        if telemetry_scope is not None:
            event.update(telemetry_scope)
        started = time.monotonic()
        request: dict[str, Any] = {
            "model": self._deployment,
            "instructions": instructions,
            "input": user_input,
            "reasoning": {"effort": self._effort},
            "max_output_tokens": max_output_tokens,
            # The runtime consumes exactly one structured model turn at a
            # time. JSON mode is a provider-side contract for that turn; it
            # complements (and does not replace) the fail-closed parser below.
            "text": {"format": {"type": "json_object"}},
            "background": True,
        }
        if self._prompt_cache_mode == "stable_prefix":
            request["prompt_cache_key"] = _stable_prompt_cache_key(
                deployment=self._deployment,
                role=self._role,
                namespace=self._prompt_cache_namespace,
            )
            # Never request extended retention from this generic mini route.
            request["prompt_cache_retention"] = "in_memory"
        try:
            if self._rate_limiter is not None:
                self._rate_limiter.acquire()
            job = self._client.responses.create(**request)
            job_id = getattr(job, "id", None)
            if not job_id:
                raise AzureModelError("responses.create returned no job id")
            event["job_id"] = str(job_id)
            event["attempt_phase"] = "poll"

            # Poll until terminal status.
            elapsed = 0.0
            while getattr(job, "status", None) in ("queued", "in_progress"):
                if elapsed >= self._poll_timeout_s:
                    raise AzureModelError(
                        f"background job {job_id} timed out after {elapsed:.0f}s "
                        f"(status={getattr(job, 'status', None)})"
                    )
                time.sleep(self._poll_interval_s)
                elapsed += self._poll_interval_s
                try:
                    job = self._client.responses.retrieve(job_id)
                except Exception as exc:
                    raise AzureModelError(
                        f"responses.retrieve failed for {job_id}: {exc}"
                    ) from exc

            status = getattr(job, "status", None)
            event.update({
                "attempt_phase": "terminal",
                "job_status": str(status),
                "poll_elapsed_s": elapsed,
                **_usage_telemetry(job),
            })
            if status == "completed":
                try:
                    text, output_receipt = canonicalize_structured_output(
                        job,
                        allow_verifier_inspection_sequence=self._role == "verifier",
                    )
                except AzureProviderOutputError as exc:
                    event["provider_output_error"] = exc.code
                    raise
                event.update(output_receipt)
                event["status"] = "completed"
                return text

            if status == "incomplete":
                # Partial text is evidence, never an executable model turn.
                # Even syntactically valid JSON may be a truncated semantic
                # decision, so quarantine the whole provider response.
                event["provider_output_error"] = "provider_output_incomplete"
                event["status"] = "incomplete"
                raise AzureProviderOutputError(
                    "provider_output_incomplete",
                    str(getattr(job, "incomplete_details", None) or ""),
                )

            error_obj = getattr(job, "error", None)
            detail = error_obj or getattr(job, "incomplete_details", None) or ""
            code = getattr(error_obj, "code", None)
            raise AzureModelError(
                f"background job {job_id} ended with status={status}: {detail}"
            ) from _JobStatusFailure(code)
        except Exception as exc:
            event.update({
                "status": "failed",
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:1000],
            })
            if isinstance(exc, AzureProviderOutputError):
                event["provider_output_error"] = exc.code
            # The runner's wall-clock interrupt is control flow, not a
            # provider failure.  Match ModelHooks' canonical handling without
            # importing the runner here (which would create a provider/runner
            # dependency cycle).  The finally block still records this
            # interrupted provider attempt for cost/latency audit.
            if exc.__class__.__name__ == "KernelRunTimeout":
                raise
            if isinstance(exc, AzureModelError):
                raise
            raise AzureModelError(f"responses.create failed: {exc}") from exc
        finally:
            event["elapsed_s"] = round(time.monotonic() - started, 3)
            self._record_attempt(event)


class AzureVisionCallable:
    """Vision transcription callable: (prompt, image_b64, media_type) -> text.

    Sends a multimodal Responses API request with an inline data-URL image.
    """

    def __init__(self, client: Any, deployment: str) -> None:
        self._client = client
        self._deployment = deployment
        self._telemetry_events: list[dict[str, Any]] = []
        self._telemetry_lock = threading.Lock()
        self._next_logical_call_id = 0

    def drain_telemetry(self) -> tuple[dict[str, Any], ...]:
        """Return and clear immutable vision-call telemetry receipts."""
        with self._telemetry_lock:
            events = tuple(self._telemetry_events)
            self._telemetry_events.clear()
            return events

    def _record_telemetry(self, event: dict[str, Any]) -> None:
        with self._telemetry_lock:
            self._telemetry_events.append(dict(event))

    def _allocate_logical_call_id(self) -> int:
        with self._telemetry_lock:
            self._next_logical_call_id += 1
            return self._next_logical_call_id

    def __call__(self, prompt: str, image_b64: str, media_type: str) -> str:
        return self._call(prompt, image_b64, media_type, telemetry_scope=None)

    def call_with_telemetry_scope(
        self,
        prompt: str,
        image_b64: str,
        media_type: str,
        *,
        run_id: str,
        task_id: str | None,
    ) -> str:
        return self._call(
            prompt,
            image_b64,
            media_type,
            telemetry_scope={"run_id": run_id, "task_id": task_id},
        )

    def _call(
        self,
        prompt: str,
        image_b64: str,
        media_type: str,
        *,
        telemetry_scope: dict[str, str | None] | None,
    ) -> str:
        event: dict[str, Any] = {
            "event_kind": "provider_attempt",
            "logical_call_id": self._allocate_logical_call_id(),
            "attempt_ordinal": 1,
            "provider": "azure_openai_responses_vision",
            "deployment": self._deployment,
            "role": "vision",
            "status": "in_progress",
            "attempt_phase": "create",
            "input_chars": len(prompt),
            "image_base64_chars": len(image_b64),
            "max_output_tokens": 8000,
            # This synchronous multimodal route sends no cache key; do not
            # imply cache support or a cache miss from absent provider fields.
            "prompt_cache_key_mode": "not_requested",
            "prompt_cache_retention": None,
            "usage_status": "unmeasured",
            "cache_metrics_status": "unmeasured",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        }
        if telemetry_scope is not None:
            event.update(telemetry_scope)
        started = time.monotonic()
        try:
            response = self._client.responses.create(
                model=self._deployment,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": f"data:{media_type};base64,{image_b64}",
                        },
                    ],
                }],
                max_output_tokens=8000,
            )
            event.update({
                "attempt_phase": "terminal",
                "job_id": str(getattr(response, "id", "")) or None,
                "job_status": str(getattr(response, "status", "completed")),
                **_usage_telemetry(response),
            })
            text = _extract_plain_output_text(response)
            if not text:
                raise AzureModelError("vision response completed but produced no output text")
            event["status"] = "completed"
            return text
        except Exception as exc:
            event.update({
                "status": "failed",
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:1000],
            })
            raise
        finally:
            event["elapsed_s"] = round(time.monotonic() - started, 3)
            self._record_telemetry(event)


def make_azure_vision_callable(
    *,
    deployment_env: str,
    key_env: str,
    endpoint_env: str = "AZURE_OPENAI_ENDPOINT",
) -> AzureVisionCallable:
    """Build a vision transcription callable from Azure env vars (build-time)."""
    endpoint = _normalize_endpoint(os.environ[endpoint_env])
    api_key = os.environ[key_env]
    deployment = os.environ[deployment_env]
    if openai is None:
        raise AzureModelError("openai package is required for AzureVisionCallable")
    client = openai.OpenAI(
        api_key=api_key,
        base_url=f"{endpoint}/openai/v1/",
        timeout=300,
        max_retries=2,
    )
    return AzureVisionCallable(client, deployment)
