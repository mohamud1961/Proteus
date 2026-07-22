from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from .ledger import ExecutionLedger
from .monitors import MonitorAlert
from .proof_contract import evaluate_proof_contract
from .runtime_ir import CompiledRuntime, MetricThreshold


@dataclass(frozen=True)
class Blocker:
    code: str
    detail: str = ""
    source: str = ""


@dataclass(frozen=True)
class CompletionDecision:
    ready: bool
    blockers: tuple[Blocker, ...] = ()
    used_check_ids: tuple[str, ...] = ()
    satisfied_obligations: tuple[str, ...] = ()
    recommend_reconfigure: bool = False


class FailureParser:
    def classify(self, text: str, *, exit_code: int | None = None) -> str:
        lowered = (text or "").lower()

        if "command not found" in lowered or "not recognized as an internal or external command" in lowered:
            return "missing_capability"
        if (
            "syntax error near unexpected token" in lowered
            or "unterminated quoted string" in lowered
            or "parse error near" in lowered
        ):
            return "check_broken"
        if "no such file or directory" in lowered:
            return "missing_artifact"
        if "interactive_job_requires_session_start" in lowered or "requires session start" in lowered:
            return "mode_mismatch"
        if "tty" in lowered and "required" in lowered:
            return "mode_mismatch"
        if "connection refused" in lowered or "login prompt not found" in lowered or "not ready" in lowered:
            return "service_not_ready"
        if "permission denied" in lowered or "protected path" in lowered or "immutable" in lowered:
            return "integrity_violation"
        if "schema" in lowered and ("missing" in lowered or "invalid" in lowered or "wrong type" in lowered):
            return "schema_mismatch"
        if "timeout" in lowered or "timed out" in lowered:
            return "timeout"
        if self._looks_like_threshold_failure(lowered):
            return "threshold_not_met"
        if "assert" in lowered or "failed" in lowered or "mismatch" in lowered or "traceback" in lowered:
            return "test_failure"
        if exit_code and exit_code != 0:
            return "command_failure"
        return ""

    @staticmethod
    def _looks_like_threshold_failure(text: str) -> bool:
        patterns = (
            r"(accuracy|score|similarity).*(<|below)",
            r"(latency|runtime|size|loss).*(>|above)",
            r"(target|threshold).*(not met|failed)",
        )
        return any(re.search(pattern, text) for pattern in patterns)


class CompletionGate:
    def evaluate(
        self,
        compiled: CompiledRuntime,
        ledger: ExecutionLedger,
        alerts: list[MonitorAlert],
    ) -> CompletionDecision:
        blockers: list[Blocker] = []
        used_check_ids: list[str] = []

        required_artifacts = {
            deliverable.path
            for deliverable in compiled.objective_graph.deliverables
            if deliverable.required
        }
        missing_artifacts = sorted(required_artifacts - ledger.current_artifacts())
        if missing_artifacts:
            blockers.append(
                Blocker(
                    code="missing_artifacts",
                    detail=", ".join(missing_artifacts),
                    source="objective_graph",
                )
            )

        if compiled.completion_policy.require_clean_integrity and ledger.integrity_violations:
            blockers.append(
                Blocker(
                    code="integrity_violation",
                    detail=ledger.integrity_violations[-1],
                    source="integrity_guard",
                )
            )

        planned_checks = compiled.planned_checks()
        if compiled.completion_policy.require_authoritative_check:
            if planned_checks:
                latest_by_id = {outcome.check_id: outcome for outcome in ledger.latest_checks(compiled.check_plan_ids)}
                for check in planned_checks:
                    outcome = latest_by_id.get(check.check_id)
                    if outcome is None:
                        blockers.append(
                            Blocker(
                                code="missing_authoritative_check",
                                detail=check.command,
                                source=check.check_id,
                            )
                        )
                        continue
                    if not outcome.passed:
                        blockers.append(
                            Blocker(
                                code=outcome.blocker_code or "check_failed",
                                detail=outcome.detail or check.command,
                                source=check.check_id,
                            )
                        )
                        continue
                    used_check_ids.append(check.check_id)
            elif not compiled.completion_policy.allow_evidence_fallback:
                blockers.append(
                    Blocker(
                        code="missing_authoritative_check",
                        detail="no visible checks and evidence fallback disabled",
                        source="completion_policy",
                    )
                )

        # Hard rule: when checks are defined but none have passed, block
        # even if individual missing/failed blockers were already added above.
        if (
            compiled.completion_policy.require_authoritative_check
            and planned_checks
            and not used_check_ids
        ):
            blockers.append(
                Blocker(
                    code="no_authoritative_check_passed",
                    detail="checks defined but none passed",
                    source="completion_policy",
                )
            )

        if compiled.objective_graph.output_schema and compiled.objective_graph.output_schema_target:
            schema_receipt = ledger.latest_receipt("schema_validation")
            if schema_receipt is None:
                blockers.append(
                    Blocker(
                        code="schema_unverified",
                        detail=compiled.objective_graph.output_schema_target,
                        source="schema_validation",
                    )
                )
            elif not schema_receipt.success:
                blockers.append(
                    Blocker(
                        code=schema_receipt.failure_class or "schema_mismatch",
                        detail=schema_receipt.summary,
                        source="schema_validation",
                    )
                )

        thresholds = list(compiled.objective_graph.thresholds)
        threshold_blockers = self._threshold_blockers(thresholds, ledger)
        blockers.extend(threshold_blockers)

        # Critical semantic clauses are completion obligations. The kernel does
        # not interpret their domain meaning; it only enforces that a certified
        # route produced sufficiently strong independent evidence and that no
        # later disproof remains active.
        if compiled.proof_contract:
            for clause_decision in evaluate_proof_contract(compiled.proof_contract, ledger):
                if clause_decision.satisfied:
                    continue
                blockers.append(
                    Blocker(
                        code=clause_decision.code,
                        detail=clause_decision.detail,
                        source=clause_decision.clause_id,
                    )
                )

        if compiled.completion_policy.require_all_obligations:
            open_obligations = [
                obligation
                for obligation in ledger.open_obligations()
                if obligation.kind not in {"integrity"}  # handled above
            ]
            if open_obligations:
                blockers.append(
                    Blocker(
                        code="unsatisfied_obligations",
                        detail=", ".join(obligation.obligation_id for obligation in open_obligations),
                        source="objective_graph",
                    )
                )

        if compiled.completion_policy.require_recent_progress and not ledger.recent_progress(4):
            blockers.append(
                Blocker(
                    code="no_recent_progress",
                    detail="no recent state change or proof receipt",
                    source="ledger",
                )
            )

        active_findings = ledger.active_finding_context(len(ledger.all_receipts()))
        blocking_findings = [
            finding for finding in active_findings
            if str(finding.get("priority", "")).strip() == "blocking"
        ]
        if blocking_findings:
            blockers.append(
                Blocker(
                    code="active_verifier_finding",
                    detail=", ".join(str(item.get("finding_id", "")) for item in blocking_findings),
                    source="model_verifier",
                )
            )

        # Automatic-memory and no-progress receipts are advisory in the certified
        # harness.  They are useful audit/context signals, but they cannot be
        # semantic completion blockers: repeated inspection is not model failure
        # until information availability has been proven.

        for alert in alerts:
            if alert.severity not in {"error", "fatal"}:
                continue
            blockers.append(
                Blocker(
                    code=alert.blocker_code or alert.code,
                    detail=alert.message,
                    source=alert.code,
                )
            )

        deduped = self._dedupe_blockers(blockers)
        recommend_reconfigure = any(
            blocker.code in compiled.reconfigure_policy.typed_triggers for blocker in deduped
        ) or any(alert.recommend_reconfigure for alert in alerts)

        return CompletionDecision(
            ready=not deduped,
            blockers=tuple(deduped),
            used_check_ids=tuple(used_check_ids),
            satisfied_obligations=ledger.satisfied_obligation_ids(),
            recommend_reconfigure=recommend_reconfigure,
        )

    def _threshold_blockers(
        self,
        thresholds: Iterable[MetricThreshold],
        ledger: ExecutionLedger,
    ) -> list[Blocker]:
        blockers: list[Blocker] = []
        for threshold in thresholds:
            actual = ledger.metrics.get(threshold.name)
            if actual is None:
                blockers.append(
                    Blocker(
                        code="missing_metric",
                        detail=threshold.name,
                        source="thresholds",
                    )
                )
                continue
            if not self._compare(actual, threshold.comparator, threshold.target):
                blockers.append(
                    Blocker(
                        code="threshold_not_met",
                        detail=f"{threshold.name}={actual} expected {threshold.comparator} {threshold.target}",
                        source="thresholds",
                    )
                )
        return blockers

    @staticmethod
    def _compare(actual: float, comparator: str, target: float | int | str) -> bool:
        try:
            numeric_target = float(target)
        except (TypeError, ValueError):
            if comparator == "==":
                return str(actual) == str(target)
            return False

        if comparator == ">=":
            return actual >= numeric_target
        if comparator == ">":
            return actual > numeric_target
        if comparator == "<=":
            return actual <= numeric_target
        if comparator == "<":
            return actual < numeric_target
        if comparator == "==":
            return actual == numeric_target
        return False

    @staticmethod
    def _dedupe_blockers(blockers: list[Blocker]) -> list[Blocker]:
        seen: set[tuple[str, str, str]] = set()
        deduped: list[Blocker] = []
        for blocker in blockers:
            key = (blocker.code, blocker.detail, blocker.source)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(blocker)
        return deduped
