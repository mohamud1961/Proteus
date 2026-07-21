"""Truncation digest building helpers for continuous HarnessEng execution."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import re
from typing import Any

_DIGEST_MAX_ENTRIES = 24
_DIGEST_MAX_LINE_LENGTH = 240
_DIGEST_CATEGORY_LIMITS = {
    "failed_test": 6,
    "assertion_summary": 6,
    "traceback_frame": 8,
    "traceback_exception": 3,
    "compiler_or_linker": 4,
    "missing_reference": 4,
    "timeout_or_kill": 4,
}

_FAILED_TEST_RE = re.compile(r"^(FAILED\s+\S.*|(?:FAIL|ERROR):\s+\S.*)$")
_TRACEBACK_FRAME_RE = re.compile(r'^File ".*", line \d+(?:, in .*)?$')
_TRACEBACK_EXCEPTION_RE = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Failure|Interrupt|Exit)(?::.*)?$"
)
_ASSERTION_SUMMARY_RE = re.compile(
    r"(^E\s+.+)|(^Assertion(?:Error| failed)?\b.*)|(\bAssertionError\b)|(\bassert\b.+==.+)"
)
_COMPILER_LINKER_RE = re.compile(
    r"(\bfatal error\b)|(\bundefined reference to\b)|(\bcollect2: error:\b)|"
    r"(\blinker command failed\b)|(\b(?:clang|gcc|g\+\+|cc|c\+\+): error:\b)|"
    r"(\bld: (?:fatal|error|cannot find)\b)",
    re.IGNORECASE,
)
_MISSING_REFERENCE_RE = re.compile(
    r"(\bNo such file or directory\b)|(\bModuleNotFoundError\b)|(\bImportError\b)|"
    r"(\bcannot import name\b)|(\bundefined symbol\b)|(\bis not defined\b)",
    re.IGNORECASE,
)
_TIMEOUT_KILL_RE = re.compile(
    r"(\btimed out\b)|(\btimeout\b)|(\bSIGKILL\b)|(\bkilled\b)|(\bterminated\b)|(\boom\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TruncationDigestEntry:
    source: str
    line_number: int | None
    kinds: list[str]
    text: str


@dataclass(frozen=True)
class TruncationDigest:
    raw_log_path: str
    entries: list[TruncationDigestEntry]
    omitted_count: int = 0


def _truncation_digest_to_dict(digest: TruncationDigest) -> dict[str, Any]:
    return asdict(digest)


def _trim_digest_line(text: str) -> str:
    if len(text) <= _DIGEST_MAX_LINE_LENGTH:
        return text
    return text[: _DIGEST_MAX_LINE_LENGTH - 3] + "..."


def _classify_digest_line(line: str, *, in_traceback: bool) -> tuple[list[str], bool]:
    stripped = line.strip()
    if not stripped:
        return [], False

    if stripped == "Traceback (most recent call last):":
        return [], True

    kinds: list[str] = []
    if in_traceback and _TRACEBACK_FRAME_RE.match(stripped):
        kinds.append("traceback_frame")
        return kinds, True

    if in_traceback and _TRACEBACK_EXCEPTION_RE.match(stripped):
        kinds.append("traceback_exception")
        in_traceback = False
    elif in_traceback and not line.startswith((" ", "\t")):
        in_traceback = False

    if _FAILED_TEST_RE.match(stripped):
        kinds.append("failed_test")
    if _ASSERTION_SUMMARY_RE.search(stripped):
        kinds.append("assertion_summary")
    if _COMPILER_LINKER_RE.search(stripped):
        kinds.append("compiler_or_linker")
    if _MISSING_REFERENCE_RE.search(stripped):
        kinds.append("missing_reference")
    if _TIMEOUT_KILL_RE.search(stripped):
        kinds.append("timeout_or_kill")

    return kinds, in_traceback


def _collect_digest_entries(
    *,
    source: str,
    text: str,
    category_counts: Counter[str],
    remaining_slots: int,
) -> tuple[list[TruncationDigestEntry], int]:
    entries: list[TruncationDigestEntry] = []
    omitted_count = 0
    in_traceback = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        kinds, in_traceback = _classify_digest_line(line, in_traceback=in_traceback)
        if not kinds:
            continue

        allowed_kinds = [
            kind for kind in kinds if category_counts[kind] < _DIGEST_CATEGORY_LIMITS.get(kind, 1)
        ]
        if not allowed_kinds:
            omitted_count += 1
            continue
        if remaining_slots <= 0:
            omitted_count += 1
            continue

        entries.append(
            TruncationDigestEntry(
                source=source,
                line_number=line_number,
                kinds=allowed_kinds,
                text=_trim_digest_line(line.strip()),
            )
        )
        for kind in allowed_kinds:
            category_counts[kind] += 1
        remaining_slots -= 1
    return entries, omitted_count


def _build_timeout_meta_entry(
    *,
    error: Any,
    exit_code: int | None,
    category_counts: Counter[str],
    remaining_slots: int,
    existing_entries: list[TruncationDigestEntry],
) -> TruncationDigestEntry | None:
    if remaining_slots <= 0 or category_counts["timeout_or_kill"] >= _DIGEST_CATEGORY_LIMITS["timeout_or_kill"]:
        return None
    if any("timeout_or_kill" in entry.kinds for entry in existing_entries):
        return None

    details: list[str] = []
    if error is not None and getattr(error, "timed_out", False):
        details.append("tool metadata indicates timeout")
    if error is not None and getattr(error, "reason_code", None):
        reason = error.reason_code.lower()
        if "kill" in reason or "timeout" in reason:
            details.append(f"tool metadata reason_code={error.reason_code}")
    if exit_code in {124, 137, 143}:
        details.append(f"tool exited with code {exit_code}")
    if not details:
        return None

    category_counts["timeout_or_kill"] += 1
    return TruncationDigestEntry(
        source="meta",
        line_number=None,
        kinds=["timeout_or_kill"],
        text=_trim_digest_line("; ".join(details)),
    )


def _build_truncation_digest(
    *,
    stdout: str,
    stderr: str,
    raw_log_path: str,
    error: Any,
    exit_code: int | None,
) -> TruncationDigest:
    entries: list[TruncationDigestEntry] = []
    category_counts: Counter[str] = Counter()
    omitted_count = 0

    for source, text in (("stdout", stdout), ("stderr", stderr)):
        source_entries, source_omitted = _collect_digest_entries(
            source=source,
            text=text,
            category_counts=category_counts,
            remaining_slots=_DIGEST_MAX_ENTRIES - len(entries),
        )
        entries.extend(source_entries)
        omitted_count += source_omitted

    meta_entry = _build_timeout_meta_entry(
        error=error,
        exit_code=exit_code,
        category_counts=category_counts,
        remaining_slots=_DIGEST_MAX_ENTRIES - len(entries),
        existing_entries=entries,
    )
    if meta_entry is not None:
        entries.append(meta_entry)

    return TruncationDigest(raw_log_path=raw_log_path, entries=entries, omitted_count=omitted_count)
