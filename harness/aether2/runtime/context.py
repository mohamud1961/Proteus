"""Cached prefix transcript management."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Mapping

from harness.aether2.runtime.adaptive_profile_helpers import redact_host_run_paths


_MODEL_METADATA_KEYS = frozenset(
    {
        "adaptive_profile",
        "benchmark",
        "condition",
        "output_root",
        "profile_name",
        "row_id",
        "run_id",
        "source_path",
        "suite",
        "task_id",
    }
)
_DROP_MODEL_METADATA_KEYS = frozenset({
    "raw_log_path",
    "run_decision_path",
    "artifacts_dir",
    "host_state_root",
    "local_mirror_root",
    "status_path",
    "registry_path",
})
_METADATA_ASSIGNMENT_RE = re.compile(
    r"(?P<label>\b(?:adaptive_profile|benchmark|condition|output_root|profile_name|row_id|run_id|source_path|suite|task_id)\b\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_BARE_BENCHMARK_TOKEN_RE = re.compile(r"\b(?:official_tasks|receipt_driven_full|terminal-bench)\b")


def _collect_redaction_literals(payload: Any) -> tuple[str, ...]:
    literals: list[str] = []

    def _remember(value: Any) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text or text in {"current_run", "[redacted_metadata]", "[host_run_path]"}:
            return
        if text not in literals:
            literals.append(text)

    def _walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key) in _MODEL_METADATA_KEYS:
                    _remember(item)
                _walk(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)
            return
        if isinstance(value, str):
            for match in _METADATA_ASSIGNMENT_RE.finditer(value):
                _remember(match.group("value").strip().strip("\"'`"))

    _walk(payload)
    return tuple(sorted(literals, key=len, reverse=True))


def sanitize_visible_text(text: str, *, redaction_literals: tuple[str, ...] = ()) -> str:
    sanitized = redact_host_run_paths(text)
    sanitized = _METADATA_ASSIGNMENT_RE.sub(lambda match: f"{match.group('label')}[redacted_metadata]", sanitized)
    sanitized = _BARE_BENCHMARK_TOKEN_RE.sub("[redacted_metadata]", sanitized)
    for literal in redaction_literals:
        sanitized = sanitized.replace(literal, "[redacted_metadata]")
    return sanitized


def sanitize_model_visible_payload(
    payload: Any,
    *,
    redaction_literals: tuple[str, ...] | None = None,
) -> Any:
    literals = redaction_literals if redaction_literals is not None else _collect_redaction_literals(payload)
    if isinstance(payload, Mapping):
        sanitized: dict[str, Any] = {}
        for key, value in payload.items():
            key_text = str(key)
            if key_text in _DROP_MODEL_METADATA_KEYS:
                continue
            if key_text == "run_id":
                sanitized[key_text] = "current_run"
                continue
            cleaned_value = sanitize_model_visible_payload(value, redaction_literals=literals)
            if key_text in _MODEL_METADATA_KEYS and isinstance(cleaned_value, str):
                sanitized[key_text] = "[redacted_metadata]"
            else:
                sanitized[key_text] = cleaned_value
        return sanitized
    if isinstance(payload, list):
        return [sanitize_model_visible_payload(item, redaction_literals=literals) for item in payload]
    if isinstance(payload, tuple):
        return [sanitize_model_visible_payload(item, redaction_literals=literals) for item in payload]
    if isinstance(payload, str):
        return sanitize_visible_text(payload, redaction_literals=literals)
    return payload


def _normalize_message(message: Mapping[str, Any], *, redaction_literals: tuple[str, ...] = ()) -> dict[str, Any]:
    role = str(message.get("role", "system"))
    content = message.get("content", "")
    if isinstance(content, str):
        content = sanitize_visible_text(content, redaction_literals=redaction_literals)
    else:
        content = json.dumps(
            sanitize_model_visible_payload(content, redaction_literals=redaction_literals),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    normalized = {"role": role, "content": content}
    for key in ("tool_call_id", "name"):
        if key in message and message[key] is not None:
            normalized[key] = sanitize_visible_text(str(message[key]), redaction_literals=redaction_literals)
    if "tool_calls" in message and message["tool_calls"] is not None:
        tool_calls = message["tool_calls"]
        if isinstance(tool_calls, list):
            normalized["tool_calls"] = [
                sanitize_model_visible_payload(
                    dict(item) if isinstance(item, Mapping) else {"value": str(item)},
                    redaction_literals=redaction_literals,
                )
                for item in tool_calls
            ]
        else:
            normalized["tool_calls"] = sanitize_model_visible_payload(
                tool_calls,
                redaction_literals=redaction_literals,
            )
    return normalized


def _merge_redaction_literals(*payloads: Any, existing: tuple[str, ...] = ()) -> tuple[str, ...]:
    merged = set(existing)
    for payload in payloads:
        merged.update(_collect_redaction_literals(payload))
    return tuple(sorted(merged, key=len, reverse=True))


def _render_json_block(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class Prefix:
    messages: tuple[dict[str, Any], ...]
    frozen_bytes: bytes
    token_estimate: int


class ContextManager:
    def __init__(self, *, delta_state: Any | None = None, compaction_generation: int = 0) -> None:
        self.delta_state = delta_state
        self.compaction_generation = int(compaction_generation)
        self.prefix: Prefix | None = None
        self.system_prompt = ""
        self.task_instruction = ""
        self.top_contract: dict[str, Any] = {}
        self.orientation: dict[str, Any] = {}
        self.tool_schemas: list[dict[str, Any]] = []
        self.extra_prefix_messages: list[dict[str, Any]] = []
        self.transcript: list[dict[str, Any]] = []
        self._tail_state = ""
        self._tail_render = ""
        self._tail_payload: dict[str, Any] = {}
        self._completion_contract: dict[str, Any] = {}
        self._frozen_success_contract: dict[str, Any] = {}
        self._frozen_success_contract_text = ""
        self._prefix_digest: str | None = None
        self._task_instruction_digest: str | None = None
        self._orientation_digest: str | None = None
        self._tool_schema_digest: str | None = None
        self._model_visible_literals: tuple[str, ...] = ()

    def build_prefix(
        self,
        *,
        system_prompt: str,
        task_instruction: str,
        orientation: Mapping[str, Any],
        tool_schemas: list[dict[str, Any]],
        frozen_success_contract: Mapping[str, Any] | str | None = None,
        extra_prefix_messages: list[Mapping[str, Any]] | None = None,
    ) -> Prefix:
        self.system_prompt = system_prompt
        self.task_instruction = task_instruction
        frozen_contract_payload, frozen_contract_text = _normalize_frozen_success_contract(
            frozen_success_contract
        )
        self._model_visible_literals = _collect_redaction_literals(
            [
                system_prompt,
                task_instruction,
                orientation,
                tool_schemas,
                frozen_contract_payload,
                extra_prefix_messages or [],
            ]
        )
        self._frozen_success_contract = frozen_contract_payload
        self._frozen_success_contract_text = frozen_contract_text
        self.top_contract = {
            "task_instruction": task_instruction,
            "frozen_success_contract": dict(self._frozen_success_contract),
        }
        self.orientation = sanitize_model_visible_payload(
            dict(orientation), redaction_literals=self._model_visible_literals
        )
        self.tool_schemas = sanitize_model_visible_payload(
            [dict(schema) for schema in tool_schemas], redaction_literals=self._model_visible_literals
        )
        self.extra_prefix_messages = [
            _normalize_message(message, redaction_literals=self._model_visible_literals)
            for message in (extra_prefix_messages or [])
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sanitize_visible_text(system_prompt, redaction_literals=self._model_visible_literals)},
            {"role": "user", "content": sanitize_visible_text(task_instruction, redaction_literals=self._model_visible_literals)},
            {
                "role": "system",
                "content": "[orientation_snapshot]\n" + _render_json_block(self.orientation),
            },
            {
                "role": "system",
                "content": "[tool_schemas]\n" + _render_json_block(self.tool_schemas),
            },
            *self.extra_prefix_messages,
        ]
        if self._frozen_success_contract_text:
            messages.insert(
                3,
                {
                    "role": "system",
                    "content": "[frozen_success_contract]\n"
                    + sanitize_visible_text(
                        self._frozen_success_contract_text,
                        redaction_literals=self._model_visible_literals,
                    ),
                },
            )
        frozen_bytes = _render_json_block(messages).encode("utf-8")
        self.prefix = Prefix(
            messages=tuple(
                _normalize_message(message, redaction_literals=self._model_visible_literals)
                for message in messages
            ),
            frozen_bytes=frozen_bytes,
            token_estimate=math.ceil(len(frozen_bytes.decode("utf-8")) / 4),
        )
        self._prefix_digest = _digest_bytes(frozen_bytes)
        self._task_instruction_digest = _digest_value(task_instruction)
        self._orientation_digest = _digest_value(self.orientation)
        self._tool_schema_digest = _digest_value(self.tool_schemas)
        return self.prefix

    def append_turn(self, *messages: Mapping[str, Any]) -> None:
        if messages:
            self._model_visible_literals = _merge_redaction_literals(
                *messages,
                existing=self._model_visible_literals,
            )
        self.transcript.extend(
            _normalize_message(message, redaction_literals=self._model_visible_literals) for message in messages
        )

    def set_completion_contract(self, completion_contract: Mapping[str, Any] | None) -> None:
        if completion_contract is not None:
            self._model_visible_literals = _merge_redaction_literals(
                completion_contract,
                existing=self._model_visible_literals,
            )
        self._completion_contract = _normalize_tail_mapping(
            sanitize_model_visible_payload(completion_contract, redaction_literals=self._model_visible_literals)
        )

    def render_tail(
        self,
        tail_state: Mapping[str, Any] | None = None,
        *,
        completion_contract: Mapping[str, Any] | None = None,
    ) -> str:
        if completion_contract is not None:
            self.set_completion_contract(completion_contract)
        if tail_state is not None:
            self._model_visible_literals = _merge_redaction_literals(
                tail_state,
                existing=self._model_visible_literals,
            )
        payload = _normalize_tail_mapping(
            sanitize_model_visible_payload(tail_state, redaction_literals=self._model_visible_literals)
        )
        if self._completion_contract:
            payload["completion_contract"] = dict(self._completion_contract)
        rendered_state = _render_json_block(payload)
        if rendered_state != self._tail_state:
            self._tail_state = rendered_state
            self._tail_render = "[tail_telemetry]\n" + rendered_state
            self._tail_payload = payload
        return self._tail_render

    def assert_prefix_unchanged(self) -> None:
        if self.prefix is None:
            raise AssertionError("prefix has not been built")
        rebuilt = ContextManager(delta_state=self.delta_state)
        rebuilt_prefix = rebuilt.build_prefix(
            system_prompt=self.system_prompt,
            task_instruction=self.task_instruction,
            orientation=self.orientation,
            tool_schemas=self.tool_schemas,
            frozen_success_contract=self._frozen_success_contract,
            extra_prefix_messages=self.extra_prefix_messages,
        )
        if rebuilt_prefix.frozen_bytes != self.prefix.frozen_bytes:
            raise AssertionError("immutable prefix changed")

    def message_history(self) -> list[dict[str, Any]]:
        if self.prefix is None:
            raise AssertionError("prefix has not been built")
        return [*self.prefix.messages, *self.transcript]

    def immutable_top_contract(self) -> dict[str, Any]:
        return dict(self.top_contract)

    def current_completion_contract(self) -> dict[str, Any]:
        return dict(self._completion_contract)

    def current_frozen_success_contract(self) -> dict[str, Any]:
        return dict(self._frozen_success_contract)

    def current_frozen_success_contract_text(self) -> str:
        return self._frozen_success_contract_text

    def current_tail_payload(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._tail_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True))

    def digest_snapshot(self) -> dict[str, Any]:
        if self.prefix is None:
            raise AssertionError("prefix has not been built")
        return {
            "immutable_prefix_digest": self._prefix_digest,
            "task_instruction_digest": self._task_instruction_digest,
            "orientation_digest": self._orientation_digest,
            "tool_schema_digest": self._tool_schema_digest,
            "tail_digest": _digest_value(self._tail_payload),
            "completion_contract_digest": _digest_value(self._completion_contract),
            "frozen_success_contract_digest": _digest_value(self._frozen_success_contract),
            "compaction_generation": self.compaction_generation,
        }


def _normalize_tail_mapping(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    return json.loads(_render_json_block(dict(payload)))


def _normalize_frozen_success_contract(
    payload: Mapping[str, Any] | str | None,
) -> tuple[dict[str, Any], str]:
    if payload is None:
        return {}, ""
    if isinstance(payload, str):
        text = payload
        verbatim_lines = [line for line in text.splitlines() if line.strip()]
        return (
            {
                "source": "explicit_text",
                "contract_text": text,
                "verbatim_lines": verbatim_lines,
            },
            text,
        )
    normalized = json.loads(_render_json_block(dict(payload)))
    contract_text = normalized.get("contract_text")
    if not isinstance(contract_text, str) or not contract_text:
        verbatim_lines = normalized.get("verbatim_lines")
        if isinstance(verbatim_lines, list):
            contract_text = "\n".join(str(line) for line in verbatim_lines)
        else:
            contract_text = ""
    if not contract_text:
        return {}, ""
    if "source" not in normalized:
        normalized["source"] = "explicit"
    normalized["contract_text"] = contract_text
    if "verbatim_lines" not in normalized:
        normalized["verbatim_lines"] = [line for line in contract_text.splitlines() if line.strip()]
    return normalized, contract_text


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_value(value: Any) -> str:
    return _digest_bytes(_render_json_block(value).encode("utf-8"))
