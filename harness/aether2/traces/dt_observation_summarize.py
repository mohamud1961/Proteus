"""Observation summarization and action kind classification for decision trace extraction."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from harness.aether2.traces.dt_row_loading import _resolved
from harness.aether2.traces.dt_receipts import (
    summarize_text,
    _short_json,
)

_VERIFICATION_EVENT_TYPES = {"verification_completed"}
_CLOSING_EVENT_TYPES = {"loop_completed", "terminal_outcome_finalized", "score_envelope_ready", "runtime_timing_summary"}

_ACTION_KINDS = (
    ("finalize", re.compile(r"\b(task_done|finalize|complete|done)\b", re.I)),
    ("verify", re.compile(r"\b(pytest|uv\s+run\s+pytest|run-tests\.sh|test_outputs\.py)\b", re.I)),
    ("service_probe", re.compile(r"\b(curl|wget|nc\s+-z|http|browser|screenshot|vnc)\b", re.I)),
    ("inspect", re.compile(r"\b(cat|head|tail|grep|find|ls|sed)\b", re.I)),
    ("install", re.compile(r"\b(apt|apt-get|pip|pip3|npm|yarn|cargo|brew|apk|dnf|yum)\b", re.I)),
    ("build", re.compile(r"\b(make|cmake|gcc|g\+\+|cargo|go\s+test|go\s+build)\b", re.I)),
    ("execute", re.compile(r"\b(python|python3|node|ruby|perl|bash|sh)\b", re.I)),
)


def _classify_action_kind(command: str, tool_name: str | None = None) -> str:
    text = command or ""
    if isinstance(tool_name, str) and tool_name:
        folded = tool_name.casefold()
        if folded in {"task_done", "finalize", "finish"}:
            return "finalize"
    for label, pattern in _ACTION_KINDS:
        if pattern.search(text):
            return label
    return "command"


def _summarize_embedded_observation(envelope: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        return {"status": "missing_observation"}
    observation: dict[str, Any] = {
        "exit_code": envelope.get("exit_code"),
        "raw_log_path": envelope.get("raw_log_path"),
    }
    for key in ("stdout_head", "stdout_tail", "stderr_head", "stderr_tail"):
        if key in envelope:
            observation[key] = summarize_text(envelope.get(key), limit=160)
    if "files_changed" in envelope:
        observation["files_changed"] = _short_json(envelope.get("files_changed"), limit=4)
    if "process_delta" in envelope:
        observation["process_delta"] = _short_json(envelope.get("process_delta"), limit=4)
    if envelope.get("error") is not None:
        observation["error"] = _short_json(envelope.get("error"), limit=4)
    return observation


def _summarize_route_observation(details: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {}
    for key in (
        "exit_code",
        "result_class",
        "reason_code",
        "signal_attribution_scope",
        "proxy_runtime_signal_detected",
        "proxy_permission_signal_detected",
        "runtime_signal_detected",
        "permission_signal_detected",
        "tool_call_contract_class",
        "verified",
    ):
        if key in details:
            observation[key] = details.get(key)
    if "layer_statuses" in details:
        observation["layer_statuses"] = _short_json(details.get("layer_statuses"), limit=8)
    if "reason_codes" in details:
        observation["reason_codes"] = _short_json(details.get("reason_codes"), limit=8)
    return observation


def _summarize_route_receipt(event: dict[str, Any], *, source_path: Path) -> dict[str, Any]:
    details = event.get("payload", {}).get("details", {})
    if not isinstance(details, dict):
        details = {}
    summary: dict[str, Any] = {
        "receipt_ref": f"{_resolved(source_path)}#seq={event.get('seq')}",
        "receipt_kind": "route_trace_event",
        "seq": event.get("seq"),
        "event_type": event.get("event_type"),
        "phase": event.get("phase"),
        "step": details.get("step"),
    }
    tool_name = details.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        normalized = details.get("normalized_payload")
        if isinstance(normalized, dict):
            tool_name = normalized.get("tool_name") if isinstance(normalized.get("tool_name"), str) else ""
    summary["tool_name"] = tool_name or ""
    command = _extract_command_from_route_details(details)
    if command:
        summary["visible_action"] = summarize_text(command, limit=160)
    if "result_class" in details:
        summary["result_class"] = details.get("result_class")
    if "reason_code" in details:
        summary["reason_code"] = details.get("reason_code")
    if "exit_code" in details:
        summary["exit_code"] = details.get("exit_code")
    if "signal_attribution_scope" in details:
        summary["signal_attribution_scope"] = details.get("signal_attribution_scope")
    if "tool_call_contract_class" in details:
        summary["tool_call_contract_class"] = details.get("tool_call_contract_class")
    if "proxy_runtime_signal_detected" in details:
        summary["proxy_runtime_signal_detected"] = details.get("proxy_runtime_signal_detected")
    if "proxy_permission_signal_detected" in details:
        summary["proxy_permission_signal_detected"] = details.get("proxy_permission_signal_detected")
    if "runtime_signal_detected" in details:
        summary["runtime_signal_detected"] = details.get("runtime_signal_detected")
    if "permission_signal_detected" in details:
        summary["permission_signal_detected"] = details.get("permission_signal_detected")
    if "reason_codes" in details:
        summary["reason_codes"] = _short_json(details.get("reason_codes"), limit=6)
    if "layer_statuses" in details:
        summary["layer_statuses"] = _short_json(details.get("layer_statuses"), limit=6)
    if event.get("event_type") in _VERIFICATION_EVENT_TYPES | _CLOSING_EVENT_TYPES:
        summary["observation"] = _summarize_route_observation(details)
    return summary


def _summarize_embedded_invocation(invocation: dict[str, Any], *, source_ref: str) -> dict[str, Any]:
    envelope = invocation.get("envelope")
    if not isinstance(envelope, dict):
        envelope = {}
    step = invocation.get("step")
    tool_name = invocation.get("tool_name") or invocation.get("tool") or ""
    command = _extract_command_from_tool_invocation(invocation)
    summary: dict[str, Any] = {
        "receipt_ref": f"{source_ref}#tool_invocation:{step}",
        "receipt_kind": "embedded_tool_invocation",
        "step": step,
        "tool_name": tool_name,
        "visible_action": summarize_text(command, limit=160),
        "observation": _summarize_embedded_observation(envelope),
    }
    if isinstance(invocation.get("arguments"), dict):
        summary["arguments"] = _short_json(invocation.get("arguments"), limit=6)
    return summary


def _extract_command_from_tool_invocation(invocation: dict[str, Any]) -> str:
    arguments = invocation.get("arguments")
    if isinstance(arguments, dict):
        for key in ("command", "cmd", "summary", "path"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _extract_command_from_route_details(details: dict[str, Any]) -> str:
    command = details.get("command")
    if isinstance(command, str) and command:
        return command
    normalized = details.get("normalized_payload")
    if isinstance(normalized, dict):
        value = normalized.get("command")
        if isinstance(value, str) and value:
            return value
    raw_payload = details.get("raw_payload")
    if isinstance(raw_payload, dict):
        arguments = raw_payload.get("arguments")
        if isinstance(arguments, dict):
            value = arguments.get("command")
            if isinstance(value, str) and value:
                return value
        value = raw_payload.get("command")
        if isinstance(value, str) and value:
            return value
    tool_calls = details.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        first = tool_calls[0]
        if isinstance(first, dict):
            arguments = first.get("arguments")
            if isinstance(arguments, dict):
                value = arguments.get("command")
                if isinstance(value, str) and value:
                    return value
    return ""
