"""Helpers and utilities for workspace and environment probing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

NETWORK_PROBE_COMMANDS: tuple[tuple[str, str], ...] = (
    (
        "python3 -c \"import socket; s=socket.create_connection(('pypi.org', 443), 3); s.close(); print('reachable: pypi.org:443')\"",
        "python3 direct DNS+TCP probe",
    ),
    (
        "python -c \"import socket; s=socket.create_connection(('pypi.org', 443), 3); s.close(); print('reachable: pypi.org:443')\"",
        "python direct DNS+TCP probe",
    ),
    (
        "sh -lc 'curl -Is --max-time 3 https://pypi.org | head -n 1'",
        "curl HEAD probe",
    ),
)

_UNKNOWN_NOTE = "not surfaced by executor config or substrate probes"


def _read_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("stdout", "output", "text", "content", "result"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        stderr = result.get("stderr")
        if isinstance(stderr, str):
            return stderr
        return ""
    for attr in ("stdout", "output", "text", "content", "result"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    stderr = getattr(result, "stderr", None)
    if isinstance(stderr, str):
        return stderr
    return ""


def _exit_code(result: Any) -> int | None:
    if result is None:
        return None
    if isinstance(result, dict):
        for key in ("exit_code", "returncode", "code", "status"):
            value = result.get(key)
            if isinstance(value, int):
                return value
        return None
    for attr in ("exit_code", "returncode", "code", "status"):
        value = getattr(result, attr, None)
        if isinstance(value, int):
            return value
    return None


def _probe(executor: Any, command: str, *, cwd: str | None = None) -> tuple[str, bool]:
    raw = executor.run(command, timeout_sec=10, cwd=cwd)
    text = _read_text(raw).strip()
    code = _exit_code(raw)
    success = code == 0 if code is not None else bool(text)
    return text, success


def _probe_candidates(executor: Any, commands: list[str], *, cwd: str | None = None, missing_value: str = "") -> str:
    for command in commands:
        text, success = _probe(executor, command, cwd=cwd)
        if success:
            return text
    return missing_value


def _split_lines(text: str, *, limit: int = 40) -> list[str]:
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[:limit]


def _probe_presence(executor: Any, commands: list[str], *, cwd: str | None = None) -> str:
    return _probe_candidates(executor, commands, cwd=cwd, missing_value="missing")


def _probe_network(executor: Any) -> tuple[bool, str]:
    last_evidence = "blocked"
    for command, label in NETWORK_PROBE_COMMANDS:
        text, success = _probe(executor, command)
        if success:
            evidence = text or f"reachable via {label}"
            return True, evidence
        if text and last_evidence == "blocked":
            last_evidence = text
    return False, last_evidence or "blocked"


def _fact(value: Any, *basis: str, note: str | None = None) -> dict[str, Any]:
    return {
        "known": True,
        "value": value,
        "basis": [item for item in basis if item],
        "note": note,
    }


def _unknown(*basis: str, note: str = _UNKNOWN_NOTE) -> dict[str, Any]:
    return {
        "known": False,
        "value": None,
        "basis": [item for item in basis if item],
        "note": note,
    }


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stable_digest(payload: Any) -> str:
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _as_path_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Path):
        return str(value)
    text = str(value).strip()
    return text


def _translate_path(executor: Any, path: str) -> str:
    if not path:
        return ""
    translate = getattr(executor, "to_container_path", None)
    if not callable(translate):
        return ""
    try:
        translated = translate(path)
    except Exception:
        return ""
    return _as_path_text(translated)


def _relative_to_root(path: str, root: str) -> str:
    if not path or not root:
        return ""
    try:
        return Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False)).as_posix()
    except ValueError:
        return ""


def _append_unique(values: list[str], candidate: str) -> None:
    if candidate and candidate not in values:
        values.append(candidate)


def _probe_writable_paths(executor: Any, candidates: list[str]) -> list[str]:
    writable_paths: list[str] = []
    for path in candidates:
        writable_probe = _probe_candidates(
            executor,
            [
                'sh -lc \'[ -w "$PWD" ] && printf writable || printf read-only\'',
                'python3 -c "import os; print(\'writable\' if os.access(\'.\', os.W_OK) else \'read-only\')"',
            ],
            cwd=path,
        )
        if writable_probe == "writable":
            _append_unique(writable_paths, path)
    return writable_paths


def _parse_listener_addresses(lines: list[str]) -> list[str]:
    listeners: list[str] = []
    for line in lines:
        lowered = line.lower()
        if "local address:port" in lowered or lowered.startswith("active internet"):
            continue
        parts = line.split()
        for index in (3, 4, -1):
            if -len(parts) <= index < len(parts):
                candidate = parts[index]
                if ":" in candidate:
                    _append_unique(listeners, candidate)
                    break
    return listeners


def _backend_kind(executor: Any) -> str:
    value = getattr(executor, "execution_boundary", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    backend = getattr(executor, "backend", None)
    kind = getattr(backend, "kind", None)
    if isinstance(kind, str) and kind.strip():
        return kind.strip()
    return "unknown"
