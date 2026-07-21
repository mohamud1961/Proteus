"""Model-invisible receipt capture for HarnessEng Aether-2."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

__all__ = ["ReceiptWriter"]

_MAX_LABEL_LEN = 40
_REDACTED = "[REDACTED]"
_CALL_ROLES = frozenset({"normal", "closing", "compaction", "verifier", "repair"})
_ORIENTATION_PREFIX = "[orientation_snapshot]\n"
_FROZEN_SUCCESS_CONTRACT_PREFIX = "[frozen_success_contract]\n"
_TAIL_BLOCK_PREFIX = "[tail_telemetry]\n"
_FACT_LEDGER_PREFIX = "[deterministic_fact_ledger]\n"
_TOOL_SCHEMAS_PREFIX = "[tool_schemas]\n"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "set-cookie",
    "token",
)
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+\b"),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b"),
)


def _safe_action_name(action: Any) -> str:
    text = _label_for_path(action)
    safe = []
    for char in text:
        if char.isalnum() or char in {"_", "-", "."}:
            safe.append(char)
        else:
            safe.append("_")
    sanitized = "".join(safe)
    if len(sanitized) <= _MAX_LABEL_LEN:
        return sanitized
    digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized[:_MAX_LABEL_LEN]}_{digest}"


def _type_name(value: Any) -> str:
    cls = value.__class__
    return f"{cls.__module__}.{cls.__qualname__}"


def _normalize_key(value: Any) -> str:
    normalized = _normalize_for_json(value)
    if isinstance(normalized, str):
        return normalized
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _stable_sort_key(value: Any) -> str:
    normalized = _normalize_for_json(value)
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _normalize_for_json(value: Any, _stack: set[int] | None = None) -> Any:
    if _stack is None:
        _stack = set()

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"__type__": "builtins.float", "value": repr(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"__type__": "builtins.bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, bytearray):
        return {"__type__": "builtins.bytearray", "base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, memoryview):
        return {"__type__": "builtins.memoryview", "base64": base64.b64encode(value.tobytes()).decode("ascii")}
    if isinstance(value, SimpleNamespace):
        marker = id(value)
        if marker in _stack:
            return {"__type__": _type_name(value), "cycle": True}
        _stack.add(marker)
        try:
            fields_map = {key: _normalize_for_json(val, _stack) for key, val in vars(value).items()}
            return {"__type__": _type_name(value), "fields": fields_map}
        finally:
            _stack.remove(marker)
    if is_dataclass(value) and not isinstance(value, type):
        marker = id(value)
        if marker in _stack:
            return {"__type__": _type_name(value), "cycle": True}
        _stack.add(marker)
        try:
            fields_map = {}
            for field in fields(value):
                fields_map[field.name] = _normalize_for_json(getattr(value, field.name), _stack)
            return {"__type__": _type_name(value), "fields": fields_map}
        finally:
            _stack.remove(marker)
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in _stack:
            return {"__type__": _type_name(value), "cycle": True}
        _stack.add(marker)
        try:
            items: list[tuple[str, Any]] = []
            for key, item in value.items():
                items.append((_normalize_key(key), _normalize_for_json(item, _stack)))
            items.sort(key=lambda pair: pair[0])
            return {key: item for key, item in items}
        finally:
            _stack.remove(marker)
    if isinstance(value, tuple):
        marker = id(value)
        if marker in _stack:
            return {"__type__": _type_name(value), "cycle": True}
        _stack.add(marker)
        try:
            return [_normalize_for_json(item, _stack) for item in value]
        finally:
            _stack.remove(marker)
    if isinstance(value, list):
        marker = id(value)
        if marker in _stack:
            return {"__type__": _type_name(value), "cycle": True}
        _stack.add(marker)
        try:
            return [_normalize_for_json(item, _stack) for item in value]
        finally:
            _stack.remove(marker)
    if isinstance(value, set):
        marker = id(value)
        if marker in _stack:
            return {"__type__": _type_name(value), "cycle": True}
        _stack.add(marker)
        try:
            normalized_items = [_normalize_for_json(item, _stack) for item in value]
            normalized_items.sort(key=_stable_sort_key)
            return normalized_items
        finally:
            _stack.remove(marker)
    if hasattr(value, "__dict__") and vars(value):
        marker = id(value)
        if marker in _stack:
            return {"__type__": _type_name(value), "cycle": True}
        _stack.add(marker)
        try:
            fields_map = {key: _normalize_for_json(val, _stack) for key, val in vars(value).items()}
            return {"__type__": _type_name(value), "fields": fields_map}
        finally:
            _stack.remove(marker)
    return {"__type__": _type_name(value), "repr": repr(value)}


def _redact_text(value: str) -> str:
    redacted = value
    redacted = re.sub(r"(?i)\bauthorization\s*:\s*[^\n]+", f"Authorization={_REDACTED}", redacted)
    redacted = re.sub(
        r"(?i)\b(authorization|api[_-]?key|token|password|secret)\s*[:=]\s*([^\s,;\"']+)",
        lambda match: f"{match.group(1)}={_REDACTED}",
        redacted,
    )
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def _is_sensitive_key(key: str) -> bool:
    folded = key.casefold()
    return any(part in folded for part in _SENSITIVE_KEY_PARTS)


def _scrub_sensitive(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_scrub_sensitive(item) for item in value]
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                scrubbed[key_text] = _REDACTED
            else:
                scrubbed[key_text] = _scrub_sensitive(item)
        return scrubbed
    return value


def _label_for_path(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        for key in ("name", "tool", "tool_name", "action"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw
        return json.dumps(_normalize_for_json(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, SimpleNamespace):
        for key in ("name", "tool", "tool_name", "action"):
            raw = getattr(value, key, None)
            if isinstance(raw, str) and raw.strip():
                return raw
        return json.dumps(_normalize_for_json(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if hasattr(value, "name"):
        raw = getattr(value, "name")
        if isinstance(raw, str) and raw.strip():
            return raw
    return str(value)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _stable_digest(value: Any) -> str:
    normalized = _normalize_for_json(value)
    serialized = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _extract_json_block(request_messages: list[Any], prefix: str) -> Any | None:
    for message in reversed(request_messages):
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.startswith(prefix):
            continue
        raw_json = content[len(prefix) :]
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError:
            return {"parse_error": True, "raw": raw_json}
    return None


def _extract_text_block(request_messages: list[Any], prefix: str) -> str | None:
    for message in reversed(request_messages):
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.startswith(prefix):
            continue
        return content[len(prefix) :]
    return None


def _extract_env_contract_metadata_from_mapping(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, Mapping):
        return None, None
    top_version = payload.get("env_contract_version")
    top_digest = payload.get("env_contract_digest")
    direct_version = payload.get("contract_version")
    direct_digest = payload.get("contract_digest")
    nested = payload.get("env_contract")
    nested_version = nested.get("contract_version") if isinstance(nested, Mapping) else None
    nested_digest = nested.get("contract_digest") if isinstance(nested, Mapping) else None
    version = top_version if isinstance(top_version, str) and top_version else None
    digest = top_digest if isinstance(top_digest, str) and top_digest else None
    if version is None and isinstance(direct_version, str) and direct_version:
        version = direct_version
    if digest is None and isinstance(direct_digest, str) and direct_digest:
        digest = direct_digest
    if version is None and isinstance(nested_version, str) and nested_version:
        version = nested_version
    if digest is None and isinstance(nested_digest, str) and nested_digest:
        digest = nested_digest
    return version, digest


def _extract_env_contract_context(request_messages: list[Any]) -> dict[str, str | None]:
    orientation_snapshot = _extract_json_block(request_messages, _ORIENTATION_PREFIX)
    version, digest = _extract_env_contract_metadata_from_mapping(orientation_snapshot)
    return {"version": version, "digest": digest}


def _extract_frozen_success_contract_context(request_messages: list[Any]) -> dict[str, str | None]:
    frozen_success_contract_text = _extract_text_block(request_messages, _FROZEN_SUCCESS_CONTRACT_PREFIX)
    digest = _stable_digest(frozen_success_contract_text) if frozen_success_contract_text is not None else None
    return {"text": frozen_success_contract_text, "digest": digest}


def _infer_call_role(request_messages: list[Any], call_role: str | None) -> str:
    if call_role is not None:
        if call_role not in _CALL_ROLES:
            raise ValueError(f"unsupported call_role: {call_role}")
        return call_role

    contents = [
        str(message.get("content", ""))
        for message in request_messages
        if isinstance(message, Mapping)
    ]
    if any("verification_report" in content for content in contents):
        return "repair"
    if any("Wall-clock deadline reached." in content for content in contents):
        return "closing"
    return "normal"


@dataclass
class ReceiptWriter:
    """Write per-step receipts and raw payloads beneath a private audit root."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.receipts_dir = self.root / "receipts"
        self.raw_dir = self.root / "raw"
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def record_step(self, step_idx: int, request: Any, response: Any, action: Any, raw_output: Any) -> Path:
        action_name = _safe_action_name(action)
        receipt_id = f"{step_idx:04d}_action_{action_name}"
        receipt_path = self.receipts_dir / f"{receipt_id}.json"
        raw_step_dir = self.raw_dir / receipt_id

        def write_raw_value(path: Path, value: Any) -> None:
            if value is None:
                _write_text(path, "")
                return
            if isinstance(value, bytes):
                _write_bytes(path, value)
                return
            if isinstance(value, bytearray):
                _write_bytes(path, bytes(value))
                return
            if isinstance(value, memoryview):
                _write_bytes(path, value.tobytes())
                return
            if isinstance(value, str):
                _write_text(path, value)
                return
            if isinstance(value, Path):
                _write_text(path, str(value))
                return
            if isinstance(value, (int, bool)) or (isinstance(value, float) and math.isfinite(value)):
                _write_text(path, str(value))
                return
            _write_text(path, json.dumps(_normalize_for_json(value), indent=2, sort_keys=True, ensure_ascii=False))

        raw_payload: dict[str, Any]
        if isinstance(raw_output, Mapping):
            raw_payload = {}
            for key, value in raw_output.items():
                normalized_key = _normalize_key(key)
                raw_payload[normalized_key] = value
                write_raw_value(raw_step_dir / _safe_action_name(normalized_key), value)
        else:
            raw_payload = {"output": raw_output}
            write_raw_value(raw_step_dir / "output", raw_output)

        payload = {
            "action": _normalize_for_json(action),
            "raw_output": _normalize_for_json(raw_payload),
            "receipt_id": receipt_id,
            "request": _normalize_for_json(request),
            "response": _normalize_for_json(response),
            "step": step_idx,
        }
        _write_json(receipt_path, payload)
        return receipt_path

    def record_model_exchange(
        self,
        call_idx: int,
        request_messages: list[Any],
        response: Any,
        *,
        tool_schemas: Any | None = None,
        call_role: str | None = None,
        tail_state: Mapping[str, Any] | None = None,
        ledger_state: Mapping[str, Any] | None = None,
        frozen_success_contract: str | None = None,
        env_contract: Mapping[str, Any] | None = None,
        env_contract_version: str | None = None,
        env_contract_digest: str | None = None,
    ) -> Path:
        """Write a full-fidelity model exchange (request messages + response) for trace forensics.

        Model-invisible: writes ``model_exchange_<N>.json`` beneath the receipts root, capturing
        the complete `request_messages` list verbatim plus the full response text and tool calls.
        """
        receipt_path = self.receipts_dir / f"model_exchange_{call_idx}.json"
        response_text = getattr(response, "text", None)
        response_tool_calls = getattr(response, "tool_calls", None)
        effective_tool_schemas = tool_schemas
        if effective_tool_schemas is None:
            effective_tool_schemas = _extract_json_block(request_messages, _TOOL_SCHEMAS_PREFIX)
        effective_tail_state = dict(tail_state) if tail_state is not None else _extract_json_block(
            request_messages, _TAIL_BLOCK_PREFIX
        )
        effective_ledger_state = dict(ledger_state) if ledger_state is not None else _extract_json_block(
            request_messages, _FACT_LEDGER_PREFIX
        )
        inferred_frozen_success_contract = _extract_frozen_success_contract_context(request_messages)
        inferred_env_contract = _extract_env_contract_context(request_messages)
        explicit_env_contract_version, explicit_env_contract_digest = _extract_env_contract_metadata_from_mapping(
            env_contract
        )
        effective_env_contract_version = (
            env_contract_version
            or explicit_env_contract_version
            or inferred_env_contract["version"]
        )
        effective_env_contract_digest = (
            env_contract_digest
            or explicit_env_contract_digest
            or inferred_env_contract["digest"]
        )
        effective_frozen_success_contract = (
            frozen_success_contract
            if frozen_success_contract is not None
            else inferred_frozen_success_contract["text"]
        )
        normalized_request_messages = _normalize_for_json(request_messages)
        normalized_payload = {
            "call_idx": call_idx,
            "call_role": _infer_call_role(request_messages, call_role),
            "request_context": {
                "env_contract": {
                    "digest": effective_env_contract_digest,
                    "version": effective_env_contract_version,
                },
                "frozen_success_contract": {
                    "digest": _stable_digest(effective_frozen_success_contract)
                    if effective_frozen_success_contract is not None
                    else None,
                    "text": effective_frozen_success_contract,
                },
                "ledger_state": _normalize_for_json(effective_ledger_state),
                "tail_state": _normalize_for_json(effective_tail_state),
                "tool_schema_digest": None
                if effective_tool_schemas is None
                else _stable_digest(effective_tool_schemas),
                "tool_schemas": _normalize_for_json(effective_tool_schemas),
            },
            "request_messages": normalized_request_messages,
            "response": {
                "text": _normalize_for_json(response_text),
                "tool_calls": _normalize_for_json(response_tool_calls),
            },
        }
        payload = {
            "call_idx": normalized_payload["call_idx"],
            "call_role": normalized_payload["call_role"],
            "request_context": _scrub_sensitive(normalized_payload["request_context"]),
            "request_messages": _scrub_sensitive(normalized_payload["request_messages"]),
            "response": _scrub_sensitive(normalized_payload["response"]),
        }
        _write_json(receipt_path, payload)
        return receipt_path

    def record_verifier_command(self, call_idx: int, tool_name: str, arguments: dict[str, Any], envelope: Any) -> Path:
        """Record one verifier inspection call (C7 audit trail), allowed or rejected.

        Written as `verifier_inspection_<N>.json` beneath the receipts root. Every command the
        fresh-context verifier attempts during Layer 2 -- whether it passes the read-only
        allowlist or is rejected -- is recorded here for a full audit trail (best-effort
        structural read-only enforcement is not perfect in a shell, so the audit trail is the
        backstop).
        """
        receipt_path = self.receipts_dir / f"verifier_inspection_{call_idx}.json"
        payload = {
            "call_idx": call_idx,
            "tool_name": tool_name,
            "arguments": _normalize_for_json(arguments),
            "envelope": _normalize_for_json(envelope),
        }
        _write_json(receipt_path, payload)
        return receipt_path
