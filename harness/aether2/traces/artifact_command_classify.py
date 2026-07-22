"""Command classification and path extraction for kernel artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

_ARTIFACT_COMMAND_KIND_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("artifact_verify", ("sha256sum", "md5sum", "cksum", "stat", "wc"), "artifact_verify_marker"),
    ("artifact_transform", ("tee", "cp", "mv"), "artifact_transform_marker"),
    ("artifact_read", ("cat", "jq", "head", "tail", "sed", "grep", "less", "more"), "artifact_read_marker"),
    ("artifact_discovery", ("find", "ls", "tree", "du", "file"), "artifact_discovery_marker"),
)

_COMMAND_PATH_FRAGMENT_RE = re.compile(
    r"(?:\./|\.\./|~/|/|[A-Za-z0-9_.-]+/)[^\s'\"`<>|;&(){}\[\],]+"
)


def _shell_tokens(command: str) -> list[str]:
    if not command:
        return []
    try:
        import shlex
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _dedupe_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value:
            out.append(value)
    return out


def _normalize_path_ref(path: str) -> str:
    candidate = path.strip()
    candidate = candidate.strip("'\" ,;:()[]{}")
    if candidate.endswith("/."):
        candidate = candidate[:-2]
    if candidate.endswith("/.."):
        candidate = candidate[:-3]
    return candidate


def _path_ref_is_safe(path: str) -> bool:
    if not path:
        return False
    if len(path) > 256:
        return False
    if any(ch.isspace() for ch in path):
        return False
    if any(ch in path for ch in ("\x00", "\n", "\r", "\t", "`", "$", "|", "&", ";", "<", ">", "\\", ":")):
        return False
    return True


def _looks_like_path_token(token: str) -> bool:
    if not token:
        return False
    if token.startswith(("/", "./", "../", "~/")):
        return True
    if "/" in token:
        return True
    suffix = Path(token).suffix.lower()
    return suffix in {
        ".csv",
        ".ini",
        ".json",
        ".jsonl",
        ".log",
        ".md",
        ".py",
        ".sh",
        ".txt",
        ".toml",
        ".xml",
        ".yaml",
        ".yml",
    }


def _coerce_artifact_path_ref(path: Path | str) -> str | None:
    candidate = path.as_posix() if isinstance(path, Path) else str(path)
    candidate = _normalize_path_ref(candidate)
    if not candidate or not _path_ref_is_safe(candidate):
        return None
    return candidate


def _invalid_artifact_path_label(path: Path | str) -> str:
    raw = path.as_posix() if isinstance(path, Path) else str(path)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"<invalid_artifact_path_ref:{digest}>"


def _command_has_marker(lower_command: str, lower_tokens: list[str], marker: str) -> bool:
    if marker in lower_tokens:
        return True
    if marker == "tee":
        return " tee " in f" {lower_command} " or lower_command.endswith(" tee")
    if marker == "cp":
        return " cp " in f" {lower_command} " or lower_command.startswith("cp ")
    if marker == "mv":
        return " mv " in f" {lower_command} " or lower_command.startswith("mv ")
    if marker in {"sha256sum", "md5sum", "cksum", "stat", "wc", "find", "ls", "tree", "du", "file"}:
        return marker in lower_tokens or f"{marker} " in lower_command or lower_command.startswith(f"{marker} ")
    if marker in {"cat", "jq", "head", "tail", "sed", "grep", "less", "more"}:
        return marker in lower_tokens or f"{marker} " in lower_command or lower_command.startswith(f"{marker} ")
    return False


def _command_matches_markers(kind: str, lower_command: str, lower_tokens: list[str], markers: tuple[str, ...]) -> bool:
    if any(_command_has_marker(lower_command, lower_tokens, marker) for marker in markers):
        return True
    if kind == "artifact_transform":
        if any(token in {">", ">>", "1>", "1>>", "2>", "2>>"} for token in lower_tokens):
            return True
        if "write_text" in lower_command or "write_bytes" in lower_command or "json.dump" in lower_command:
            return True
        if "open(" in lower_command and any(flag in lower_command for flag in ("'w'", '"w"', "'wb'", '"wb"', "'a'", '"a"')):
            return True
    if kind == "artifact_read":
        if "read_text" in lower_command or "read_bytes" in lower_command or "json.load" in lower_command:
            return True
    if kind == "artifact_verify":
        if "hashlib" in lower_command or "checksum" in lower_command:
            return True
    if kind == "artifact_discovery":
        if "tree" in lower_tokens:
            return True
    return False


def _artifact_command_reason_codes(
    *,
    kind: str,
    lower_command: str,
    lower_tokens: list[str],
    markers: tuple[str, ...],
    reason_prefix: str,
) -> list[str]:
    reason_codes = [
        f"{reason_prefix}:{marker}"
        for marker in markers
        if _command_has_marker(lower_command, lower_tokens, marker)
    ]
    if kind == "artifact_transform":
        if any(token in {">", ">>", "1>", "1>>", "2>", "2>>"} for token in lower_tokens):
            reason_codes.append("artifact_transform_marker:redirection")
        if "write_text" in lower_command or "write_bytes" in lower_command or "json.dump" in lower_command:
            reason_codes.append("artifact_transform_marker:write_api")
        if "open(" in lower_command and any(flag in lower_command for flag in ("'w'", '"w"', "'wb'", '"wb"', "'a'", '"a"')):
            reason_codes.append("artifact_transform_marker:open_write")
    elif kind == "artifact_read":
        if "read_text" in lower_command or "read_bytes" in lower_command or "json.load" in lower_command:
            reason_codes.append("artifact_read_marker:read_api")
    elif kind == "artifact_verify":
        if "hashlib" in lower_command or "checksum" in lower_command:
            reason_codes.append("artifact_verify_marker:hash_api")
    elif kind == "artifact_discovery":
        if "tree" in lower_tokens:
            reason_codes.append("artifact_discovery_marker:tree")
    return _dedupe_strings(reason_codes)


def classify_artifact_command(command: str) -> dict[str, Any]:
    """Classify artifact-related commands using a generic family taxonomy."""
    tokens = _shell_tokens(command)
    lower_tokens = [token.lower() for token in tokens]
    lower_command = command.lower()
    matched_markers: list[str] = []
    selected_kind = "artifact_other"
    selected_reason_prefix = ""
    selected_markers: tuple[str, ...] = ()
    for kind, markers, reason_prefix in _ARTIFACT_COMMAND_KIND_RULES:
        if _command_matches_markers(kind, lower_command, lower_tokens, markers):
            selected_kind = kind
            selected_reason_prefix = reason_prefix
            selected_markers = markers
            break
    if selected_kind == "artifact_other":
        return {
            "kind": selected_kind,
            "reason_codes": ["artifact_command_unclassified"],
            "matched_markers": [],
        }
    matched_markers = _artifact_command_reason_codes(
        kind=selected_kind,
        lower_command=lower_command,
        lower_tokens=lower_tokens,
        markers=selected_markers,
        reason_prefix=selected_reason_prefix,
    )
    return {
        "kind": selected_kind,
        "reason_codes": _dedupe_strings(matched_markers),
        "matched_markers": _dedupe_strings(matched_markers),
    }


def extract_artifact_path_refs(command: str) -> list[str]:
    """Return bounded artifact-path candidates extracted from shell text."""
    if not command:
        return []
    path_refs: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        safe_candidate = _coerce_artifact_path_ref(candidate)
        if safe_candidate is None or not _looks_like_path_token(safe_candidate) or safe_candidate in seen:
            return
        seen.add(safe_candidate)
        path_refs.append(safe_candidate)

    for quoted in re.findall(r"""['"]([^'"\n\r]+)['"]""", command):
        _add(quoted)

    for token in _shell_tokens(command):
        _add(token)
        for fragment in _COMMAND_PATH_FRAGMENT_RE.findall(token):
            _add(fragment)
    return path_refs
