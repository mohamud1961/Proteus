"""Requirement extraction and relevance helpers for the Aether-2 control loop.

Responsibilities:
- Parse task instructions into a list of stated requirements.
- Filter harness-wrapper and noise lines from requirements.
- Compute token-level relevance between observations and requirements.
- Navigate requirement entries inside an evidence ledger.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from harness.aether2.traces.delta import (
    build_evidence_ledger,
    ensure_stated_requirements,
)
from harness.aether2.runtime.context import ContextManager

__all__ = [
    "UNASSIGNED_ACTIVITY_REQUIREMENT",
    "_current_evidence_ledger",
    "_extract_stated_requirements",
    "_extract_verifier_task_contract",
    "_is_harness_wrapper_requirement_line",
    "_is_noise_requirement_line",
    "_ledger_requirement_text",
    "_ledger_requirements",
    "_observation_relevance_tokens",
    "_primary_requirement",
    "_relevant_requirement",
    "_requirement_relevance_tokens",
    "_unresolved_requirements",
    "_tail_evidence_ledger",
]

_REQUIREMENT_PREVIEW_LIMIT = 4

# wrapper/boilerplate lines (pure headings, separators, "thanks"/sign-off
# style lines) so they do not become noisy requirement entries.
_REQUIREMENT_LINE_NOISE_PATTERNS = (
    "thank you",
    "thanks",
    "good luck",
    "please",
)

_WRAPPER_REQUIREMENT_PATTERNS = (
    "current working directory is",
    "writable task workspace",
    "official verifier",
    "hidden verifier",
    "hidden grader",
    "hidden tests",
    "solution.sh",
    "task_done",
    "plan-only diagnostic",
    "plausible file",
    "plausible process",
    "independent verifier",
    "if the task asks for",
    "for qemu/telnet",
    "for vnc/desktop",
    "for media/transcription",
    "for long-running jobs",
    "strong checks",
    "strong enough",
    "receipt-backed evidence",
)

# W1.1: a generic bucket for tool activity that does not visibly relate to any
# stated requirement (e.g. exploratory commands, environment probes). Keeping
# this separate avoids forcing every observation onto the first unresolved
# requirement, which previously made unrelated activity look like progress on
# that requirement.
UNASSIGNED_ACTIVITY_REQUIREMENT = "unassigned activity (not linked to a stated requirement)"


def _is_noise_requirement_line(line: str) -> bool:
    """Conservative filter for wrapper/boilerplate lines.

    Only filters lines that are clearly structural noise: markdown heading
    markers, pure separator/punctuation lines, or very short sign-off style
    lines. Never filters anything that looks like it carries a path, command,
    constraint, or behavioral detail.
    """

    stripped = line.strip()
    if not stripped:
        return True
    # Markdown headings (e.g. "# Task", "## Notes") are structural, not
    # individually actionable requirements.
    if stripped.lstrip("#").strip() != stripped and stripped.startswith("#"):
        return True
    # Pure separator/punctuation lines (e.g. "---", "===", "***").
    if all(char in "-=*_~. " for char in stripped):
        return True
    # Short sign-off/wrapper lines with no path-, command-, or constraint-like
    # content (no slashes, backticks, digits, or quotes) are likely boilerplate.
    lowered = stripped.lower()
    if len(stripped) <= 40 and not any(token in stripped for token in ("/", "`", "\"", "'", ":")) and not any(char.isdigit() for char in stripped):
        if any(phrase in lowered for phrase in _REQUIREMENT_LINE_NOISE_PATTERNS):
            return True
    return False


def _is_harness_wrapper_requirement_line(line: str) -> bool:
    """Return true for wrapper doctrine that should not become task contract.

    These lines describe harness operating policy rather than the user-authored
    success condition. Real task constraints are still preserved
    unless they contain explicit harness-control vocabulary such as task_done,
    hidden grader/verifier files, or generic "if the task asks for..." doctrine.
    """

    lowered = line.lower()
    if "you can run" in lowered and "verify" in lowered:
        return True
    return any(pattern in lowered for pattern in _WRAPPER_REQUIREMENT_PATTERNS)


def _extract_stated_requirements(task_instruction: str) -> list[str]:
    """Parse task instruction into a list of stated requirement strings."""

    requirements: list[str] = []
    for raw_line in task_instruction.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _is_noise_requirement_line(line):
            continue
        if line.startswith(("-", "*")):
            requirement = line[1:].strip()
        else:
            digits = []
            for char in line:
                if char.isdigit():
                    digits.append(char)
                    continue
                if char in {".", ")"} and digits:
                    requirement = line[len(digits) + 1 :].strip()
                    break
                requirement = line
                break
            else:
                requirement = line
        if _is_harness_wrapper_requirement_line(requirement):
            continue
        if len(requirement) > 300:
            requirement = requirement[:297].rstrip() + "..."
        if requirement and requirement not in requirements:
            requirements.append(requirement)
    if requirements:
        return requirements
    trimmed = " ".join(task_instruction.split())
    if not trimmed:
        return ["Complete the stated task contract."]
    if len(trimmed) > 300:
        trimmed = trimmed[:297].rstrip() + "..."
    return [trimmed]


def _extract_verifier_task_contract(task_instruction: str) -> str:
    """Compact task contract for fresh-context verification.

    The executor still receives the full task instruction, including any
    harness wrapper. The verifier receives only the requirement projection so
    harness-side doctrine cannot be reinterpreted as success criteria.
    """

    return "\n".join(_extract_stated_requirements(task_instruction))


def _requirement_relevance_tokens(text: str) -> set[str]:
    """Return normalized token set for token-level relevance matching."""

    tokens: set[str] = set()
    for raw_token in re.findall(r"[A-Za-z0-9_./\\-]+", text.lower()):
        token = raw_token.strip("./\\-_")
        if len(token) >= 3:
            tokens.add(token)
        # Path-like tokens: also index the basename and extension.
        if "/" in raw_token or "\\" in raw_token:
            base = raw_token.replace("\\", "/").rsplit("/", 1)[-1]
            base = base.strip("./\\-_")
            if len(base) >= 3:
                tokens.add(base)
    return tokens


def _observation_relevance_tokens(*, tool_name: str, arguments: Mapping[str, Any], artifact_paths: list[str]) -> set[str]:
    """Return token set for an observation (tool call + artifact paths)."""

    tokens: set[str] = set()
    for path in artifact_paths:
        tokens |= _requirement_relevance_tokens(path)
    for key in ("path", "cmd", "session_id", "job_id"):
        raw = arguments.get(key)
        if isinstance(raw, str) and raw.strip():
            tokens |= _requirement_relevance_tokens(raw)
    return tokens


def _relevant_requirement(
    ledger: Mapping[str, Any],
    fallback_requirements: list[str],
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    artifact_paths: list[str],
) -> str:
    """Pick the requirement an observation visibly relates to.

    Matches on shared path/command/identifier tokens between the observation
    and each requirement's text. Falls back to `UNASSIGNED_ACTIVITY_REQUIREMENT`
    when no stated requirement shares any visible token with the observation,
    instead of always attaching to the first unresolved requirement.
    """

    observation_tokens = _observation_relevance_tokens(
        tool_name=tool_name, arguments=arguments, artifact_paths=artifact_paths
    )
    if observation_tokens:
        for item in _ledger_requirements(ledger):
            requirement_text = _ledger_requirement_text(item)
            if requirement_text == UNASSIGNED_ACTIVITY_REQUIREMENT:
                continue
            if observation_tokens & _requirement_relevance_tokens(requirement_text):
                return requirement_text
    if tool_name == "task_done":
        return _primary_requirement(ledger, fallback_requirements)
    return UNASSIGNED_ACTIVITY_REQUIREMENT


def _current_evidence_ledger(context: ContextManager) -> dict[str, Any]:
    """Return the current evidence ledger, seeded with any stated requirements."""

    snapshot = context.delta_state
    if snapshot is None:
        return build_evidence_ledger()
    ledger = getattr(snapshot, "evidence_ledger", {}) or {}
    requirements = _extract_stated_requirements(context.task_instruction)
    return ensure_stated_requirements(ledger, requirements)


def _ledger_requirements(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the list of requirement entries from a ledger mapping."""

    raw_requirements = ledger.get("requirements", [])
    if not isinstance(raw_requirements, list):
        return []
    return [item for item in raw_requirements if isinstance(item, Mapping)]


def _ledger_requirement_text(requirement: Mapping[str, Any]) -> str:
    """Extract the plain-text requirement string from a requirement entry."""

    return str(requirement.get("requirement", "")).strip()


def _primary_requirement(ledger: Mapping[str, Any], fallback_requirements: list[str]) -> str:
    """Return the first unresolved requirement, or first requirement, or fallback."""

    unresolved = _unresolved_requirements(ledger)
    if unresolved:
        return _ledger_requirement_text(unresolved[0])
    requirements = _ledger_requirements(ledger)
    if requirements:
        return _ledger_requirement_text(requirements[0])
    return fallback_requirements[0]


def _unresolved_requirements(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return requirement entries that are not yet proven with no open blockers."""

    unresolved: list[dict[str, Any]] = []
    for item in _ledger_requirements(ledger):
        if _ledger_requirement_text(item) == UNASSIGNED_ACTIVITY_REQUIREMENT:
            continue
        status = str(item.get("status", "unproven"))
        blockers = item.get("verifier_blockers", []) or []
        next_required = item.get("next_required_evidence", []) or []
        if status != "proven" or blockers or next_required:
            unresolved.append(dict(item))
    return unresolved


def _tail_evidence_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact tail-state view of the ledger for context injection."""

    requirement_rows: list[dict[str, Any]] = []
    for item in _ledger_requirements(ledger)[:_REQUIREMENT_PREVIEW_LIMIT]:
        requirement_rows.append(
            {
                "requirement": _ledger_requirement_text(item),
                "status": str(item.get("status", "unproven")),
                "evidence_strength": str(item.get("evidence_strength", "none")),
                "failed_checks": list(item.get("failed_checks", []) or [])[:2],
                "open_risks": list(item.get("open_risks", []) or [])[:2],
                "verifier_blockers": list(item.get("verifier_blockers", []) or [])[:2],
                "next_required_evidence": list(item.get("next_required_evidence", []) or [])[:2],
            }
        )
    return {
        "requirements": requirement_rows,
        "repeated_failure_families": list(ledger.get("repeated_failure_families", []) or [])[:4],
    }
