"""Tests for aether_next.classifier.reconcile_grader_alignment."""
from __future__ import annotations

from aether_next.classifier import reconcile_grader_alignment


def test_grader_pass_and_kernel_completed_is_aligned() -> None:
    result = reconcile_grader_alignment(reward=1.0, grader_error=None, kernel_status="completed")
    assert result == {
        "official_grader_status": "pass",
        "internal_completion_status": "completed",
        "verifier_alignment_status": "aligned",
    }


def test_grader_fail_and_kernel_incomplete_is_aligned() -> None:
    result = reconcile_grader_alignment(reward=0.0, grader_error=None, kernel_status="incomplete")
    assert result == {
        "official_grader_status": "fail",
        "internal_completion_status": "incomplete",
        "verifier_alignment_status": "aligned",
    }


def test_grader_pass_kernel_incomplete_is_verifier_completion_miss() -> None:
    """Regression for the openssl-selfsigned-cert anomaly in the Stage 1
    repair-slice rerun: reward=1.0 (grader passed 6/6) but kernel status stayed
    incomplete/model_limit for all 30 steps because a stale active finding never
    cleared. This must never read as an unexplained capability failure -- it is
    a verifier/completion-gate miss, distinct from the model actually failing.
    """
    result = reconcile_grader_alignment(reward=1.0, grader_error=None, kernel_status="incomplete")
    assert result["official_grader_status"] == "pass"
    assert result["internal_completion_status"] == "incomplete"
    assert result["verifier_alignment_status"] == "verifier_completion_miss"


def test_explicit_verifier_verdict_overrides_kernel_status_proxy() -> None:
    result = reconcile_grader_alignment(
        reward=0.0,
        grader_error=None,
        kernel_status="completed",
        verifier_verdict="needs_repair",
    )
    assert result["official_grader_status"] == "fail"
    assert result["internal_completion_status"] == "incomplete"
    assert result["verifier_alignment_status"] == "aligned"


def test_grader_fail_kernel_completed_is_verifier_false_clean() -> None:
    """Regression for the filter-js-from-html false-clean in the same rerun:
    kernel status=completed but the grader failed both tests. Must be labeled
    verifier_false_clean, not silently reported as a pass.
    """
    result = reconcile_grader_alignment(reward=0.0, grader_error=None, kernel_status="completed")
    assert result["official_grader_status"] == "fail"
    assert result["internal_completion_status"] == "completed"
    assert result["verifier_alignment_status"] == "verifier_false_clean"


def test_grader_unavailable_is_not_applicable() -> None:
    for kernel_status in ("completed", "incomplete", "timeout", "error"):
        result = reconcile_grader_alignment(reward=None, grader_error="reward.txt missing or empty", kernel_status=kernel_status)
        assert result["official_grader_status"] == "unavailable"
        assert result["verifier_alignment_status"] == "not_applicable"


def test_environment_failure_records_are_consistent() -> None:
    """_error_record/_timeout_record (environment/infra failures, grader never ran)
    must carry the same three fields for schema consistency across all row types."""
    from aether_next.runners.docker_runner import _error_record, _timeout_record

    err = _error_record("some-task", "some/image", "container_start_failed", "boom")
    assert err["official_grader_status"] == "unavailable"
    assert err["verifier_alignment_status"] == "not_applicable"

    to = _timeout_record("some-task", "some/image", "kernel_timeout_after_900s", "boom")
    assert to["official_grader_status"] == "unavailable"
    assert to["verifier_alignment_status"] == "not_applicable"
