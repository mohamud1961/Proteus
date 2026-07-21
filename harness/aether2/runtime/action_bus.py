"""Typed action bus for terminal-first harness execution."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

ACTION_TYPES = (
    "command",
    "script",
    "native_tool_call",
    "start_service",
    "probe_service",
    "verify",
    "finalize",
)


def infer_action_type(*, tool_name: str, command: str) -> str:
    normalized_tool = tool_name.strip().lower()
    normalized_command = command.strip().lower()
    if normalized_tool and normalized_tool != "raw_bash":
        if normalized_tool == "register_service":
            return "start_service"
        if normalized_tool == "probe_service":
            return "probe_service"
        return "native_tool_call"
    if normalized_command.startswith("python") and "<<" in normalized_command:
        return "script"
    if "verifier" in normalized_command or re.search(r"\b(pytest|unittest|bash\s+.*test)", normalized_command):
        return "verify"
    if re.search(r"\b(finalize|submission|final_answer)\b", normalized_command):
        return "finalize"
    if normalized_command.startswith(("curl", "wget")) or "/health" in normalized_command:
        return "probe_service"
    if (
        "--port" in normalized_command
        or re.search(r"\b(launch_service|runserver|uvicorn|gunicorn|http\.server|flask run|serve)\b", normalized_command)
        or normalized_command.endswith("&")
    ):
        return "start_service"
    return "command"


def extract_command(arguments: Any) -> str:
    if isinstance(arguments, dict):
        value = arguments.get("command")
        return value if isinstance(value, str) else ""
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict) and isinstance(parsed.get("command"), str):
            return parsed["command"]
        return text
    return ""


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    action_type: str
    tool_name: str
    command: str
    step: int | None
    tool_index: int | None
    phase: str


class ActionBus:
    def __init__(self, *, run_id: str):
        self.run_id = run_id
        self._counter = 0
        self._records: list[ActionRecord] = []

    def record_from_tool_call(
        self,
        *,
        tool_call: Any,
        step: int | None,
        tool_index: int | None,
        phase: str = "execute",
    ) -> ActionRecord:
        self._counter += 1
        action_id = f"{self.run_id}-a{self._counter:04d}"
        tool_name = "raw_bash"
        command = ""
        if isinstance(tool_call, dict):
            maybe_name = tool_call.get("name")
            if isinstance(maybe_name, str) and maybe_name:
                tool_name = maybe_name
            command = extract_command(tool_call.get("arguments"))
        action_type = infer_action_type(tool_name=tool_name, command=command)
        record = ActionRecord(
            action_id=action_id,
            action_type=action_type,
            tool_name=tool_name,
            command=command,
            step=step,
            tool_index=tool_index,
            phase=phase,
        )
        self._records.append(record)
        return record

    def record_system_action(
        self,
        *,
        action_type: str,
        phase: str,
        command: str = "",
        step: int | None = None,
        tool_index: int | None = None,
    ) -> ActionRecord:
        if action_type not in ACTION_TYPES:
            raise ValueError(f"unsupported action_type: {action_type}")
        self._counter += 1
        action_id = f"{self.run_id}-a{self._counter:04d}"
        record = ActionRecord(
            action_id=action_id,
            action_type=action_type,
            tool_name="system",
            command=command,
            step=step,
            tool_index=tool_index,
            phase=phase,
        )
        self._records.append(record)
        return record

    def export_summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "action_count": len(self._records),
            "records": [record.__dict__.copy() for record in self._records],
        }
