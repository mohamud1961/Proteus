"""Truthful parsing and reconciliation of official grader output.

Terminal-Bench graders may run more than one pytest phase.  A later focused
phase must never overwrite an earlier repository-suite failure.  The official
reward remains authoritative, while parsed details are retained as evidence
and checked for contradictions.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

_PYTEST_COUNT = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>failed|passed|error|errors|skipped|xfailed|xpassed|warning|warnings)\b",
    re.IGNORECASE,
)
_PYTEST_SUMMARY_LINE = re.compile(
    r"(?:=+\s*)?(?P<body>(?:\d+\s+(?:failed|passed|error|errors|skipped|xfailed|xpassed|warning|warnings)"
    r"(?:,?\s*|\s+))+)(?:\s+in\s+[0-9.]+s)?(?:\s*=+)?$",
    re.IGNORECASE,
)


def _normalise_kind(kind: str) -> str:
    value = kind.lower()
    if value == "errors":
        return "error"
    if value == "warnings":
        return "warning"
    return value


def parse_pytest_phases(output: str) -> list[dict[str, Any]]:
    """Return every pytest summary phase visible in *output*, in order."""
    phases: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(output.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = _PYTEST_SUMMARY_LINE.search(line)
        if match is None:
            continue
        counts: dict[str, int] = {}
        for count_match in _PYTEST_COUNT.finditer(match.group("body")):
            kind = _normalise_kind(count_match.group("kind"))
            counts[kind] = counts.get(kind, 0) + int(count_match.group("count"))
        if not counts:
            continue
        phases.append(
            {
                "phase_index": len(phases) + 1,
                "line_number": line_number,
                "summary": line,
                "counts": counts,
                "passed_count": counts.get("passed", 0),
                "failed_count": counts.get("failed", 0) + counts.get("error", 0),
            }
        )
    return phases


def parse_ctrf_detail(ctrf_text: str | None) -> dict[str, Any] | None:
    """Parse optional CTRF detail without treating it as the whole grader."""
    if not ctrf_text or not ctrf_text.strip():
        return None
    try:
        ctrf = json.loads(ctrf_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"status": "invalid_json"}
    tests = ctrf.get("results", {}).get("tests", [])
    if not isinstance(tests, list):
        return {"status": "invalid_shape"}
    passed = [str(item.get("name", "?")) for item in tests if item.get("status") == "passed"]
    failed = [str(item.get("name", "?")) for item in tests if item.get("status") == "failed"]
    return {
        "status": "parsed",
        "passed_count": len(passed),
        "failed_count": len(failed),
        "passed_names": passed[:20],
        "failed_names": failed[:20],
    }


def build_grader_detail(
    *,
    reward: float,
    grader_exit: int,
    stdout: str,
    stderr: str = "",
    ctrf_text: str | None = None,
) -> dict[str, Any]:
    """Build a loss-aware grader summary reconciled with official reward.

    The reward is authoritative.  Parsed phases and CTRF are supporting
    evidence.  Any disagreement is explicit and cannot be laundered into an
    all-passing structured summary.
    """
    phases = parse_pytest_phases("\n".join(part for part in (stdout, stderr) if part))
    aggregate: dict[str, int] = {}
    for phase in phases:
        for kind, count in phase["counts"].items():
            aggregate[kind] = aggregate.get(kind, 0) + int(count)

    ctrf = parse_ctrf_detail(ctrf_text)
    official_pass = reward >= 1.0
    parsed_failure_count = aggregate.get("failed", 0) + aggregate.get("error", 0)
    parsed_test_count = sum(
        aggregate.get(kind, 0)
        for kind in ("passed", "failed", "error", "skipped", "xfailed", "xpassed")
    )
    contradictions: list[str] = []
    if official_pass and parsed_failure_count > 0:
        contradictions.append("official_pass_with_visible_failed_phase")
    if not official_pass and parsed_test_count > 0 and parsed_failure_count == 0:
        contradictions.append("official_fail_with_only_visible_passing_phases")
    if grader_exit == 0 and not official_pass:
        contradictions.append("zero_exit_with_failing_official_reward")
    if grader_exit != 0 and official_pass:
        contradictions.append("nonzero_exit_with_passing_official_reward")
    if ctrf and ctrf.get("status") == "parsed":
        ctrf_failed = int(ctrf.get("failed_count", 0) or 0)
        if official_pass and ctrf_failed > 0:
            contradictions.append("official_pass_with_ctrf_failures")
        if not official_pass and ctrf_failed == 0 and int(ctrf.get("passed_count", 0) or 0) > 0:
            contradictions.append("official_fail_with_ctrf_all_pass")

    return {
        "official_status": "pass" if official_pass else "fail",
        "official_reward": reward,
        "grader_exit": grader_exit,
        "phases": phases,
        "phase_count": len(phases),
        "aggregate_counts": aggregate,
        "passed_count": aggregate.get("passed", 0),
        "failed_count": parsed_failure_count,
        "ctrf": ctrf,
        "consistent_with_official_reward": not contradictions,
        "contradictions": contradictions,
        "detail_status": "parsed" if not contradictions else "reward_contradiction",
    }


def grader_detail_is_safe(detail: Mapping[str, Any]) -> bool:
    """Whether a structured detail can be presented without contradiction."""
    return bool(detail.get("consistent_with_official_reward", False))
