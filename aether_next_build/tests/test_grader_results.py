from __future__ import annotations

import json

from aether_next.runners.grader_results import (
    build_grader_detail,
    grader_detail_is_safe,
    parse_pytest_phases,
)


def test_parse_pytest_phases_keeps_earlier_failure_and_later_pass() -> None:
    output = """
=================== test session starts ===================
FAILED tests/test_headers.py::test_control_characters
================ 1 failed, 366 passed in 2.31s ================
other grader work
====================== 6 passed in 0.11s ======================
"""
    phases = parse_pytest_phases(output)
    assert [phase["failed_count"] for phase in phases] == [1, 0]
    assert [phase["passed_count"] for phase in phases] == [366, 6]


def test_official_failure_retains_all_phases_and_never_becomes_all_pass() -> None:
    detail = build_grader_detail(
        reward=0.0,
        grader_exit=1,
        stdout="1 failed, 366 passed in 2.31s\n6 passed in 0.11s\n",
    )
    assert detail["official_status"] == "fail"
    assert detail["phase_count"] == 2
    assert detail["failed_count"] == 1
    assert detail["passed_count"] == 372
    assert detail["consistent_with_official_reward"] is True
    assert grader_detail_is_safe(detail) is True


def test_final_all_pass_ctrf_cannot_overwrite_visible_earlier_failure() -> None:
    ctrf = json.dumps(
        {
            "results": {
                "tests": [
                    {"name": f"focused_{index}", "status": "passed"}
                    for index in range(6)
                ]
            }
        }
    )
    detail = build_grader_detail(
        reward=0.0,
        grader_exit=1,
        stdout="1 failed, 366 passed in 2.31s\n6 passed in 0.11s\n",
        ctrf_text=ctrf,
    )
    assert detail["official_status"] == "fail"
    assert detail["failed_count"] == 1
    assert detail["ctrf"]["failed_count"] == 0
    assert "official_fail_with_ctrf_all_pass" in detail["contradictions"]
    assert grader_detail_is_safe(detail) is False


def test_reward_contradiction_is_explicit_and_fail_closed() -> None:
    detail = build_grader_detail(
        reward=0.0,
        grader_exit=0,
        stdout="6 passed in 0.11s\n",
    )
    assert detail["official_status"] == "fail"
    assert detail["detail_status"] == "reward_contradiction"
    assert "official_fail_with_only_visible_passing_phases" in detail["contradictions"]
    assert "zero_exit_with_failing_official_reward" in detail["contradictions"]
    assert grader_detail_is_safe(detail) is False


def test_official_pass_with_visible_failure_is_explicit() -> None:
    detail = build_grader_detail(
        reward=1.0,
        grader_exit=0,
        stdout="1 failed, 3 passed in 0.20s\n",
    )
    assert detail["official_status"] == "pass"
    assert "official_pass_with_visible_failed_phase" in detail["contradictions"]
    assert grader_detail_is_safe(detail) is False
