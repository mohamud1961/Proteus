"""Envelope and tool-call dispatch helpers for the Aether-2 control loop.

Pure extraction from loop.py — zero behaviour change.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from harness.aether2.traces.envelope import ObservationEnvelope, build_envelope
from harness.aether2.runtime.adaptive_profile_helpers import redact_host_run_paths

__all__ = [
    "_action_signature",
    "_build_blind_retry_blocked_envelope",
    "_envelope_failed",
    "_envelope_to_message",
    "_error_raw",
    "_parse_tool_call_arguments",
    "_tool_call_name",
]


def _error_raw(
    tool: str,
    exc: Exception,
    *,
    started_at: float,
    cwd: str,
    kind: str = "runtime_error",
) -> dict[str, Any]:
    """Build a raw error payload dict for a failed tool dispatch."""
    return {
        "tool": tool,
        "exit_code": 1,
        "duration_sec": time.monotonic() - started_at,
        "cwd": cwd,
        "stdout": "",
        "stderr": str(exc),
        "error": {
            "kind": kind,
            "message": str(exc),
            "reason_code": kind,
            "tool_name": tool,
        },
    }


def _action_signature(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Stable string key for a (tool_name, arguments) pair."""
    return f"{tool_name}:" + json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _envelope_failed(envelope: ObservationEnvelope) -> bool:
    """Return True when the envelope carries a non-zero exit or an error."""
    if envelope.error is not None:
        return True
    if envelope.exit_code is not None and envelope.exit_code != 0:
        return True
    return False


def _build_blind_retry_blocked_envelope(
    tool_name: str,
    arguments: Mapping[str, Any],
    cwd: str,
    *,
    raw_log_dir: Path,
    guidance: str | None = None,
) -> ObservationEnvelope:
    """Return a synthetic envelope that blocks a blind-retry of the same failed action."""
    raw = {
        "tool": tool_name,
        "exit_code": 1,
        "duration_sec": 0.0,
        "cwd": cwd,
        "stdout": "",
        "stderr": "blind_retry_blocked_same_failed_command" + (f"\n{guidance}" if guidance else ""),
        "blind_retry_blocked": True,
        "error": {
            "kind": "blind_retry_blocked",
            "message": (
                "This exact action just failed and nothing has changed since. "
                "Try something different before repeating it."
                + (f" Task-specific repeat guidance: {guidance}" if guidance else "")
            ),
            "reason_code": "blind_retry_blocked_same_failed_command",
            "tool_name": tool_name,
        },
    }
    return build_envelope(raw, raw_log_dir=raw_log_dir)


def _parse_tool_call_arguments(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the arguments field of a tool-call object into a plain dict."""
    arguments = tool_call.get("arguments")
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        if not arguments.strip():
            return {}
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _tool_call_name(tool_call: Mapping[str, Any]) -> str | None:
    """Extract the tool name from a raw tool-call object, supporting nested function."""
    name = tool_call.get("name")
    if isinstance(name, str) and name:
        return name
    function = tool_call.get("function")
    if isinstance(function, Mapping):
        nested_name = function.get("name")
        if isinstance(nested_name, str) and nested_name:
            return nested_name
    return None


def _envelope_to_message(tool_name: str, tool_call_id: Any, envelope: ObservationEnvelope) -> dict[str, Any]:
    """Serialise an ObservationEnvelope into an OpenAI-style tool-result message."""
    payload = {
        "tool": envelope.tool,
        "exit_code": envelope.exit_code,
        "duration_sec": envelope.duration_sec,
        "cwd": envelope.cwd,
        "stdout_head": envelope.stdout_head,
        "stdout_tail": envelope.stdout_tail,
        "stderr_head": envelope.stderr_head,
        "stderr_tail": envelope.stderr_tail,
        "truncated": envelope.truncated,
        "raw_log_path": envelope.raw_log_path,
        "files_changed": [item.__dict__ for item in envelope.files_changed],
        "process_delta": envelope.process_delta.__dict__,
        "blind_retry_blocked": envelope.blind_retry_blocked,
        "error": None if envelope.error is None else envelope.error.__dict__,
        "truncation_digest": (
            None
            if envelope.truncation_digest is None
            else {
                "raw_log_path": envelope.truncation_digest.raw_log_path,
                "omitted_count": envelope.truncation_digest.omitted_count,
                "entries": [entry.__dict__ for entry in envelope.truncation_digest.entries],
            }
        ),
    }
    payload = redact_host_run_paths(payload)
    return {
        "role": "tool",
        "name": tool_name,
        "tool_call_id": tool_call_id,
        "content": json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    }
