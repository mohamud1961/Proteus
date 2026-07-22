from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .kernel import KernelResult
    from .ledger import Receipt


LIMITER_LABELS = (
    "none",
    "timeout_resource_failure",
    "model_limit",
    "harness_runtime_failure",
    "harness_tooling_failure",
    "harness_context_failure",
    "verification_failure",
    "substrate_missing",
    "environment_runner_failure",
    "safety_policy_failure",
)


@dataclass(frozen=True)
class LimiterClassification:
    label: str
    confidence: str
    evidence: tuple[str, ...]
    detail: str = ""


class HarnessLimiterClassifier:
    """Post-run classifier: determines WHY a run ended."""

    def classify(self, result: "KernelResult") -> LimiterClassification:
        receipts = result.receipts

        if result.status == "completed":
            return LimiterClassification(
                label="none",
                confidence="high",
                evidence=(),
                detail="completed",
            )

        if result.status == "config_invalid":
            return LimiterClassification(
                label="harness_runtime_failure",
                confidence="high",
                evidence=tuple(result.blockers),
                detail=f"config_invalid: {', '.join(result.blockers)}",
            )

        if result.status == "timeout":
            return LimiterClassification(
                label="timeout_resource_failure",
                confidence="high",
                evidence=tuple(result.blockers),
                detail=(
                    "agent phase terminated by wall clock; final state was still "
                    "scored by the official grader (graded_after_timeout)"
                ),
            )

        if result.status == "verifier_stalemate":
            stalemate = tuple(
                r.receipt_id for r in receipts if r.kind == "verifier_stalemate"
            )
            return LimiterClassification(
                label="verification_failure",
                confidence="high",
                evidence=stalemate or tuple(result.blockers),
                detail=(
                    "bounded verifier stalemate: identical findings survived "
                    "repeated verification rounds; disagreement recorded, not adjudicated"
                ),
            )

        if result.status == "solver_submit_stalemate":
            stalemate = tuple(
                r.receipt_id for r in receipts if r.kind == "solver_submit_stalemate"
            )
            feedback_delivered = any(
                r.kind == "model_verifier_result" for r in receipts
            )
            # The verifier did its job here (it raised findings and refused to
            # rubber-stamp).  If the workbench was otherwise clean and feedback
            # was delivered, repeatedly submitting instead of repairing is the
            # model's behavior; otherwise fall back to a harness/context label.
            if feedback_delivered and self._model_limit_evidence_bar_for_stalemate(receipts):
                return LimiterClassification(
                    label="model_limit",
                    confidence="medium",
                    evidence=stalemate or tuple(result.blockers),
                    detail=(
                        "solver kept submitting without new evidence despite "
                        "delivered, legible completion findings on a clean workbench"
                    ),
                )
            return LimiterClassification(
                label="harness_context_failure",
                confidence="medium",
                evidence=stalemate or tuple(result.blockers),
                detail=(
                    "solver submit stalemate without proven-clean feedback "
                    "delivery; insufficient evidence to blame the model"
                ),
            )

        # Check for safety blocks.
        safety_receipts = [r for r in receipts if r.kind == "safety_block"]
        if safety_receipts:
            return LimiterClassification(
                label="safety_policy_failure",
                confidence="high",
                evidence=tuple(r.receipt_id for r in safety_receipts),
                detail=f"{len(safety_receipts)} safety block(s)",
            )

        # Check for substrate/bootstrap failures.
        substrate_receipts = [
            r for r in receipts
            if not r.success and (
                r.failure_class in {"missing_capability", "substrate"}
                or (r.kind == "bootstrap" and not r.success)
            )
        ]
        if substrate_receipts:
            return LimiterClassification(
                label="substrate_missing",
                confidence="high",
                evidence=tuple(r.receipt_id for r in substrate_receipts),
                detail=f"{len(substrate_receipts)} substrate/bootstrap failure(s)",
            )

        # Check for harness runtime failures (integrity, action/turn validation,
        # unknown actions).
        runtime_kinds = {"integrity_block", "action_validation", "turn_validation", "unknown_action"}
        runtime_receipts = [r for r in receipts if r.kind in runtime_kinds and not r.success]
        if runtime_receipts:
            non_runtime = [r for r in receipts if r.kind not in runtime_kinds and r.success]
            if len(runtime_receipts) >= len(non_runtime):
                return LimiterClassification(
                    label="harness_runtime_failure",
                    confidence="high",
                    evidence=tuple(r.receipt_id for r in runtime_receipts),
                    detail=f"{len(runtime_receipts)} runtime validation failure(s)",
                )

        # Check for no-progress (repeated identical failures with no state change).
        if self._is_no_progress(receipts):
            return LimiterClassification(
                label="harness_context_failure",
                confidence="medium",
                evidence=tuple(
                    r.receipt_id for r in receipts
                    if not r.success and not r.state_change
                )[-6:],
                detail="repeated identical failures with no state change",
            )

        # Check for verification failure vs model_limit on incomplete runs.
        check_receipts = [r for r in receipts if r.kind == "check_result"]
        has_state_changes = any(r.state_change for r in receipts)
        has_real_diversity = self._has_real_action_diversity(receipts)
        has_harness_blocks = (
            bool(runtime_receipts or safety_receipts or substrate_receipts)
            or self._has_harness_block_receipts(receipts)
        )

        if check_receipts:
            failed_checks = [r for r in check_receipts if not r.success]
            if failed_checks and has_state_changes and not has_harness_blocks and self._model_limit_evidence_bar(receipts):
                return LimiterClassification(
                    label="model_limit",
                    confidence="high",
                    evidence=tuple(r.receipt_id for r in failed_checks),
                    detail="model produced wrong solution rejected by checks",
                )

        # Incomplete with genuine progress + diverse real actions + no harness blocks.
        if (
            result.status == "incomplete"
            and has_state_changes
            and has_real_diversity
            and not has_harness_blocks
            and self._model_limit_evidence_bar(receipts)
        ):
            return LimiterClassification(
                label="model_limit",
                confidence="medium",
                evidence=tuple(r.receipt_id for r in receipts if r.state_change)[-4:],
                detail="genuine progress and diverse actions but no passing check",
            )

        # Fallback: insufficient evidence to blame the model.  Empty receipts,
        # only validation/no-op receipts, or no real action diversity all land
        # here -- the harness did not surface a real attempt.
        return LimiterClassification(
            label="harness_context_failure",
            confidence="low",
            evidence=tuple(r.receipt_id for r in receipts)[-4:],
            detail="insufficient evidence to attribute to model; harness did not surface a real attempt",
        )

    @staticmethod
    def _is_no_progress(receipts: tuple["Receipt", ...]) -> bool:
        """Detect repeated identical failing receipts with no state change."""
        if len(receipts) < 3:
            return False
        tail = receipts[-6:]
        failing = [r for r in tail if not r.success and not r.state_change]
        if len(failing) < 3:
            return False
        failure_classes = Counter(r.failure_class or r.kind for r in failing)
        most_common_count = failure_classes.most_common(1)[0][1]
        return most_common_count >= 3

    # Real action kinds that demonstrate the model was given a working runtime
    # and actually got to act.  Meta/validation/reconfigure receipts don't count.
    _REAL_ACTION_KINDS = frozenset({
        "read_file", "write_file", "run_command", "inspect_artifact",
        "bootstrap_acquire", "launch_process", "probe_service",
        "run_experiment",
    })

    # Receipt kinds that indicate harness-side blocks (safety, integrity,
    # validation, substrate).  Presence of any of these means we cannot
    # attribute the outcome solely to the model.
    _HARNESS_BLOCK_KINDS = frozenset({
        "safety_block", "integrity_block", "action_validation",
        "turn_validation", "unknown_action", "bootstrap",
        "solver_parse_error", "unsupported_solver_reconfigure",
        "report_blocker", "context_floor_failure", "harness_context_failure",
        "verifier_required_for_completion", "model_verifier_error",
        "model_verifier_skipped",
    })

    _MODEL_LIMIT_DISQUALIFYING_FAILURE_CLASSES = frozenset({
        "solver_protocol_error", "missing_context_handle", "context_floor_failure",
        "harness_context_failure", "solver_reported_blocker",
        "unsupported_solver_reconfigure", "verifier_missing",
        "environment_probe_untrusted", "unprobed_env_fact",
        "timeout", "missing_capability", "substrate",
    })

    @classmethod
    def _has_real_action_diversity(cls, receipts: tuple["Receipt", ...]) -> bool:
        """>=2 distinct real action kinds among the receipts."""
        real_kinds = {r.kind for r in receipts if r.kind in cls._REAL_ACTION_KINDS}
        return len(real_kinds) >= 2

    @classmethod
    def _has_harness_block_receipts(cls, receipts: tuple["Receipt", ...]) -> bool:
        """Any failed safety/integrity/validation/substrate receipts."""
        return any(
            r.kind in cls._HARNESS_BLOCK_KINDS and not r.success
            for r in receipts
        )

    @classmethod
    def _model_limit_evidence_bar_for_stalemate(cls, receipts: tuple["Receipt", ...]) -> bool:
        """Evidence bar for submit-stalemate attribution, ignoring the
        stalemate receipt itself (it describes the outcome being classified,
        not a harness defect)."""
        filtered = tuple(r for r in receipts if r.kind != "solver_submit_stalemate")
        return cls._model_limit_evidence_bar(filtered)

    @classmethod
    def _model_limit_evidence_bar(cls, receipts: tuple["Receipt", ...]) -> bool:
        """Conservative local evidence bar before blaming model capability.

        A model-limit label is only meaningful after the harness proves the model
        had stable tools, context, completion feedback when relevant, and no silent
        protocol/config/runtime failures.  This local check is intentionally
        conservative; insufficient evidence should fall back to a harness/context
        label rather than overclaiming model failure.
        """
        if not receipts:
            return False
        for receipt in receipts:
            if receipt.kind == "no_progress_control":
                payload = receipt.payload or {}
                if payload.get("consequence") not in {"advisory", "none", ""}:
                    return False
            if receipt.kind in cls._HARNESS_BLOCK_KINDS and not receipt.success:
                return False
            if (receipt.failure_class or "") in cls._MODEL_LIMIT_DISQUALIFYING_FAILURE_CLASSES:
                return False
        # At least one real action must have executed successfully; otherwise a
        # failed visible check could just mean the model never got a usable turn.
        return any(r.kind in cls._REAL_ACTION_KINDS and r.success for r in receipts)


def reconcile_grader_alignment(
    *,
    reward: float | None,
    grader_error: str | None,
    kernel_status: str,
    verifier_verdict: str | None = None,
) -> dict[str, str]:
    """Reconcile the official grader's verdict against the kernel's own completion
    status, at the post-run record layer only -- never inside the completion gate
    or verifier packet, which must stay grader-blind.

    Without this, a row can read reward=1.0 (grader: task done) with
    status=incomplete/classifier=model_limit (kernel: task not done), which looks
    like a capability failure but is actually the harness's own completion/finding
    logic failing to recognize success the grader already confirmed -- exactly what
    happened for openssl-selfsigned-cert in the Stage 1 repair-slice rerun. The
    inverse (kernel says completed, grader disagrees) is a false-clean, as happened
    for filter-js-from-html in the same rerun.

    Existing fields (status, classifier_label, kernel_status) are left untouched --
    they remain the kernel's own grader-blind account of what happened. These three
    new fields are the only place grader truth and kernel judgment are compared.
    """
    if grader_error is not None or reward is None:
        official_grader_status = "unavailable"
    elif reward >= 1.0:
        official_grader_status = "pass"
    else:
        official_grader_status = "fail"

    normalized_verifier_verdict = str(verifier_verdict or "").strip()
    if normalized_verifier_verdict:
        internal_completion_status = "completed" if normalized_verifier_verdict == "completed" else "incomplete"
    else:
        internal_completion_status = "completed" if kernel_status == "completed" else "incomplete"

    if official_grader_status == "unavailable":
        verifier_alignment_status = "not_applicable"
    elif official_grader_status == "pass" and internal_completion_status == "completed":
        verifier_alignment_status = "aligned"
    elif official_grader_status == "pass" and internal_completion_status != "completed":
        verifier_alignment_status = "verifier_completion_miss"
    elif official_grader_status == "fail" and internal_completion_status == "completed":
        verifier_alignment_status = "verifier_false_clean"
    else:
        verifier_alignment_status = "aligned"

    return {
        "official_grader_status": official_grader_status,
        "internal_completion_status": internal_completion_status,
        "verifier_alignment_status": verifier_alignment_status,
    }
