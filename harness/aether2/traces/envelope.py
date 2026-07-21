"""Typed observation envelope helpers for continuous HarnessEng execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping
import json
import os
import re
import uuid

from harness.aether2.traces.envelope_digest import (
    TruncationDigest,
    TruncationDigestEntry,
    _build_truncation_digest,
    _truncation_digest_to_dict,
)

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_HEAD_TAIL_LIMIT = 2048
_FULL_BOUND_LIMIT = _HEAD_TAIL_LIMIT * 2


@dataclass(frozen=True)
class FileDelta:
    path: str
    hash_before: str | None
    hash_after: str | None
    change_type: str


@dataclass
class ProcessDelta:
    started: list[str] = field(default_factory=list)
    exited: list[str] = field(default_factory=list)
    log_growth: dict[str, int] = field(default_factory=dict)
    jobs_started: list[str] = field(default_factory=list)
    jobs_exited: list[str] = field(default_factory=list)
    sessions_started: list[str] = field(default_factory=list)
    sessions_exited: list[str] = field(default_factory=list)
    services_started: list[str] = field(default_factory=list)
    services_exited: list[str] = field(default_factory=list)
    job_log_growth: dict[str, int] = field(default_factory=dict)
    session_log_growth: dict[str, int] = field(default_factory=dict)
    service_log_growth: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ErrorInfo:
    kind: str
    message: str
    reason_code: str | None = None
    failure_class: str | None = None
    details: str | None = None
    tool_name: str | None = None
    command: str | None = None
    exit_code: int | None = None
    timed_out: bool | None = None


@dataclass(frozen=True)
class ObservationEnvelope:
    tool: str
    exit_code: int | None
    duration_sec: float
    cwd: str
    stdout_head: str
    stdout_tail: str
    stderr_head: str
    stderr_tail: str
    truncated: bool
    raw_log_path: str
    files_changed: list[FileDelta]
    process_delta: ProcessDelta
    blind_retry_blocked: bool
    error: ErrorInfo | None
    truncation_digest: TruncationDigest | None


def collapse_cr_ansi(text: str) -> str:
    """Collapse ANSI escape noise and carriage-return rewrites to the visible final state."""
    if not text:
        return text
    without_ansi = _ANSI_RE.sub("", text)
    normalized = without_ansi.replace("\r\n", "\n")
    rendered_lines: list[str] = []
    current: list[str] = []
    for char in normalized:
        if char == "\r":
            current.clear()
            continue
        if char == "\n":
            rendered_lines.append("".join(current))
            rendered_lines.append("\n")
            current.clear()
            continue
        current.append(char)
    rendered_lines.append("".join(current))
    return "".join(rendered_lines)


def build_envelope(raw: Any, *, raw_log_dir: Path) -> ObservationEnvelope:
    tool = str(_coerce_field(raw, "tool", "unknown"))
    exit_code = _coerce_optional_int(_coerce_field(raw, "exit_code", None))
    duration_sec = float(_coerce_field(raw, "duration_sec", _coerce_field(raw, "duration", 0.0)))
    cwd = str(_coerce_field(raw, "cwd", os.getcwd()))
    stdout = str(_coerce_field(raw, "stdout", _coerce_field(raw, "stdout_text", "")))
    stderr = str(_coerce_field(raw, "stderr", _coerce_field(raw, "stderr_text", "")))
    files_changed = _coerce_file_deltas(_coerce_field(raw, "files_changed", []))
    process_delta = _coerce_process_delta(_coerce_field(raw, "process_delta", None))
    error = _coerce_error_info(_coerce_field(raw, "error", None))
    blind_retry_blocked = _coerce_blind_retry_blocked(raw, error)

    raw_log_dir = Path(raw_log_dir).resolve()
    raw_log_dir.mkdir(parents=True, exist_ok=True)
    raw_log_path = raw_log_dir / f"{_slugify(tool)}_{uuid.uuid4().hex}.json"
    raw_payload = {
        "tool": tool,
        "exit_code": exit_code,
        "duration_sec": duration_sec,
        "cwd": cwd,
        "stdout": stdout,
        "stderr": stderr,
        "files_changed": [asdict(file_delta) for file_delta in files_changed],
        "process_delta": _process_delta_to_dict(process_delta),
        "blind_retry_blocked": blind_retry_blocked,
        "error": None if error is None else _error_info_to_dict(error),
    }

    stdout_clean = collapse_cr_ansi(stdout)
    stderr_clean = collapse_cr_ansi(stderr)
    stdout_head, stdout_tail, stdout_truncated = _bound_stream(stdout_clean)
    stderr_head, stderr_tail, stderr_truncated = _bound_stream(stderr_clean)
    truncation_digest = None
    if stdout_truncated or stderr_truncated:
        truncation_digest = _build_truncation_digest(
            stdout=stdout_clean,
            stderr=stderr_clean,
            raw_log_path=str(raw_log_path),
            error=error,
            exit_code=exit_code,
        )

    raw_payload["truncation_digest"] = (
        None if truncation_digest is None else _truncation_digest_to_dict(truncation_digest)
    )
    raw_log_path.write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return ObservationEnvelope(
        tool=tool,
        exit_code=exit_code,
        duration_sec=duration_sec,
        cwd=cwd,
        stdout_head=stdout_head,
        stdout_tail=stdout_tail,
        stderr_head=stderr_head,
        stderr_tail=stderr_tail,
        truncated=stdout_truncated or stderr_truncated,
        raw_log_path=str(raw_log_path),
        files_changed=files_changed,
        process_delta=process_delta,
        blind_retry_blocked=blind_retry_blocked,
        error=error,
        truncation_digest=truncation_digest,
    )


def _bound_stream(text: str) -> tuple[str, str, bool]:
    length = len(text)
    if length <= _HEAD_TAIL_LIMIT:
        return text, "", False
    if length <= _FULL_BOUND_LIMIT:
        return text[:_HEAD_TAIL_LIMIT], text[_HEAD_TAIL_LIMIT:], False
    return text[:_HEAD_TAIL_LIMIT], text[-_HEAD_TAIL_LIMIT:], True


def _coerce_field(raw: Any, name: str, default: Any) -> Any:
    if isinstance(raw, Mapping) and name in raw:
        return raw[name]
    if hasattr(raw, name):
        return getattr(raw, name)
    return default


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _coerce_file_deltas(value: Any) -> list[FileDelta]:
    if value is None:
        return []
    deltas: list[FileDelta] = []
    for item in value:
        if isinstance(item, FileDelta):
            deltas.append(item)
            continue
        if isinstance(item, Mapping):
            deltas.append(
                FileDelta(
                    path=str(item.get("path", "")),
                    hash_before=_optional_str(item.get("hash_before")),
                    hash_after=_optional_str(item.get("hash_after")),
                    change_type=str(item.get("change_type", "unknown")),
                )
            )
            continue
        deltas.append(
            FileDelta(
                path=str(getattr(item, "path", "")),
                hash_before=_optional_str(getattr(item, "hash_before", None)),
                hash_after=_optional_str(getattr(item, "hash_after", None)),
                change_type=str(getattr(item, "change_type", "unknown")),
            )
        )
    return deltas


def _coerce_process_delta(value: Any) -> ProcessDelta:
    if value is None:
        return ProcessDelta()
    if isinstance(value, ProcessDelta):
        return value
    if isinstance(value, Mapping):
        return ProcessDelta(
            started=[str(item) for item in value.get("started", [])],
            exited=[str(item) for item in value.get("exited", [])],
            log_growth={str(key): int(val) for key, val in value.get("log_growth", {}).items()},
            jobs_started=[str(item) for item in value.get("jobs_started", [])],
            jobs_exited=[str(item) for item in value.get("jobs_exited", [])],
            sessions_started=[str(item) for item in value.get("sessions_started", [])],
            sessions_exited=[str(item) for item in value.get("sessions_exited", [])],
            services_started=[str(item) for item in value.get("services_started", [])],
            services_exited=[str(item) for item in value.get("services_exited", [])],
            job_log_growth={
                str(key): int(val) for key, val in value.get("job_log_growth", {}).items()
            },
            session_log_growth={
                str(key): int(val) for key, val in value.get("session_log_growth", {}).items()
            },
            service_log_growth={
                str(key): int(val) for key, val in value.get("service_log_growth", {}).items()
            },
        )
    return ProcessDelta(
        started=[str(item) for item in getattr(value, "started", [])],
        exited=[str(item) for item in getattr(value, "exited", [])],
        log_growth={
            str(key): int(val) for key, val in getattr(value, "log_growth", {}).items()
        },
        jobs_started=[str(item) for item in getattr(value, "jobs_started", [])],
        jobs_exited=[str(item) for item in getattr(value, "jobs_exited", [])],
        sessions_started=[str(item) for item in getattr(value, "sessions_started", [])],
        sessions_exited=[str(item) for item in getattr(value, "sessions_exited", [])],
        services_started=[str(item) for item in getattr(value, "services_started", [])],
        services_exited=[str(item) for item in getattr(value, "services_exited", [])],
        job_log_growth={
            str(key): int(val) for key, val in getattr(value, "job_log_growth", {}).items()
        },
        session_log_growth={
            str(key): int(val) for key, val in getattr(value, "session_log_growth", {}).items()
        },
        service_log_growth={
            str(key): int(val) for key, val in getattr(value, "service_log_growth", {}).items()
        },
    )


def _coerce_error_info(value: Any) -> ErrorInfo | None:
    if value is None:
        return None
    if isinstance(value, ErrorInfo):
        return value
    if isinstance(value, Mapping):
        return ErrorInfo(
            kind=str(value.get("kind", "unknown")),
            message=str(value.get("message", "")),
            reason_code=_optional_str(value.get("reason_code")),
            failure_class=_optional_str(value.get("failure_class")),
            details=_optional_str(value.get("details")),
            tool_name=_optional_str(value.get("tool_name")),
            command=_optional_str(value.get("command")),
            exit_code=_coerce_optional_int(value.get("exit_code")),
            timed_out=_coerce_optional_bool(value.get("timed_out")),
        )
    return ErrorInfo(
        kind=str(getattr(value, "kind", "unknown")),
        message=str(getattr(value, "message", "")),
        reason_code=_optional_str(getattr(value, "reason_code", None)),
        failure_class=_optional_str(getattr(value, "failure_class", None)),
        details=_optional_str(getattr(value, "details", None)),
        tool_name=_optional_str(getattr(value, "tool_name", None)),
        command=_optional_str(getattr(value, "command", None)),
        exit_code=_coerce_optional_int(getattr(value, "exit_code", None)),
        timed_out=_coerce_optional_bool(getattr(value, "timed_out", None)),
    )


def _coerce_blind_retry_blocked(raw: Any, error: ErrorInfo | None) -> bool:
    if isinstance(raw, Mapping) and "blind_retry_blocked" in raw:
        return bool(raw["blind_retry_blocked"])
    if hasattr(raw, "blind_retry_blocked"):
        return bool(getattr(raw, "blind_retry_blocked"))
    if error is not None and error.reason_code == "blind_retry_blocked_same_failed_command":
        return True
    return False


def _process_delta_to_dict(process_delta: ProcessDelta) -> dict[str, Any]:
    return asdict(process_delta)


def _error_info_to_dict(error: ErrorInfo) -> dict[str, Any]:
    return asdict(error)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return bool(value)


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip())
    return cleaned or "envelope"


__all__ = [
    "ErrorInfo",
    "FileDelta",
    "ObservationEnvelope",
    "ProcessDelta",
    "TruncationDigest",
    "TruncationDigestEntry",
    "build_envelope",
    "collapse_cr_ansi",
]
