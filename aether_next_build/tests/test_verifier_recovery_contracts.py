from __future__ import annotations

from dataclasses import dataclass

import pytest

from aether_next.verifier import CompletionEvidenceEntry, ModelVerifierResult, VerifierFinding
from aether_next.kernel_verifier import _verified_blocker_receipt
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.verifier_recovery import (
    CompiledEvidenceRequirement,
    EvidenceClass,
    VerifierRecoveryAction,
    VerifierRecoveryRouter,
    execute_primary_then_fallback,
    findings_for_solver_context,
    validate_compiled_evidence,
)


def test_primary_failure_executes_one_compiled_fallback_only() -> None:
    seen: list[str] = []

    def execute(route: str) -> None:
        seen.append(route)
        if route == "primary":
            raise OSError("primary unavailable")

    attempts = execute_primary_then_fallback(
        primary_route="primary",
        fallback_route="fallback",
        executor=execute,
        inspection_id_factory=lambda route: f"inspection:{route}",
    )
    assert seen == ["primary", "fallback"]
    assert [item.success for item in attempts] == [False, True]
    assert attempts[0].inspection_id == "inspection:primary"


def test_both_routes_fail_without_solver_repair() -> None:
    attempts = execute_primary_then_fallback(
        primary_route="primary",
        fallback_route="fallback",
        executor=lambda route: (_ for _ in ()).throw(RuntimeError(route)),
    )
    assert len(attempts) == 2
    assert all(not item.success for item in attempts)
    router = VerifierRecoveryRouter(max_packet_retries=1)
    blocked = ModelVerifierResult("blocked_by_tooling", summary="both routes failed")
    assert router.route(blocked, packet_signature="p") is VerifierRecoveryAction.RETRY_VERIFIER
    assert router.route(blocked, packet_signature="p") is VerifierRecoveryAction.TERMINAL_INFRASTRUCTURE


def test_reconfiguration_requires_verified_allowed_owner() -> None:
    blocked = ModelVerifierResult("blocked_by_harness_config", summary="missing verifier route")
    router = VerifierRecoveryRouter(max_packet_retries=0)
    assert router.route(
        blocked,
        packet_signature="cfg",
        blocker_owner="harness_config",
        blocker_verified=False,
        allowed_reconfigure_owners=("harness_config",),
    ) is VerifierRecoveryAction.TERMINAL_INFRASTRUCTURE
    assert router.route(
        blocked,
        packet_signature="cfg-2",
        blocker_owner="harness_config",
        blocker_verified=True,
        allowed_reconfigure_owners=("harness_config",),
    ) is VerifierRecoveryAction.RECONFIGURE


def test_only_conclusive_solver_defects_return_to_solver() -> None:
    router = VerifierRecoveryRouter(max_packet_retries=1)
    assert router.route(ModelVerifierResult("needs_repair", findings=(VerifierFinding("f", 1, "needs_repair", "blocking", "wrong", evidence=("x",)),)), packet_signature="s") is VerifierRecoveryAction.RETURN_TO_SOLVER
    assert router.route(ModelVerifierResult("uncertain_missing_evidence", summary="missing"), packet_signature="s2") is VerifierRecoveryAction.RETRY_VERIFIER


@dataclass(frozen=True)
class Evidence:
    clause_ids: tuple[str, ...]
    evidence_class: str
    inspection_refs: tuple[str, ...]
    falsification_check: str = "change the source"


def test_compiled_evidence_rejects_proxy_unknown_id_and_ceiling() -> None:
    errors = validate_compiled_evidence(
        [Evidence(("c_value",), "metadata_proxy", ("unknown",))],
        requirements=(CompiledEvidenceRequirement("c_value", EvidenceClass.INDEPENDENT_SEMANTIC),),
        known_inspection_ids=("i1",),
        inspection_ceilings={"i1": EvidenceClass.METADATA_PROXY},
    )
    codes = {error.code for error in errors}
    assert {"unknown_inspection_id", "weak_evidence"} <= codes


def test_compiled_evidence_accepts_all_clauses_with_independent_observations() -> None:
    errors = validate_compiled_evidence(
        [
            Evidence(("c_file",), "exact_contract", ("i1",)),
            Evidence(("c_value",), "independent_semantic", ("i2",)),
        ],
        requirements=(
            CompiledEvidenceRequirement("c_file", EvidenceClass.EXACT_CONTRACT),
            CompiledEvidenceRequirement("c_value", EvidenceClass.INDEPENDENT_SEMANTIC),
        ),
        known_inspection_ids=("i1", "i2"),
        inspection_ceilings={"i1": EvidenceClass.EXACT_CONTRACT, "i2": EvidenceClass.INDEPENDENT_SEMANTIC},
    )
    assert errors == ()


def test_findings_feedback_is_solver_only_and_completion_clears() -> None:
    finding = VerifierFinding("f1", 2, "needs_repair", "blocking", "wrong bytes", evidence=("read result",), repair_instruction="rewrite")
    repair = ModelVerifierResult("needs_repair", findings=(finding,))
    assert findings_for_solver_context(repair)[0]["finding_id"] == "f1"
    assert findings_for_solver_context(ModelVerifierResult("blocked_by_tooling", summary="tool unavailable")) == []
    assert findings_for_solver_context(ModelVerifierResult("completed", summary="all clauses verified")) == []


def test_reconfigure_authorization_requires_harness_receipt_and_packet_signature() -> None:
    ledger = ExecutionLedger()
    ledger.record(Receipt(
        "verified", 2, "verifier_blocker_verified", True,
        "verified owner", failure_class="verifier_tooling",
        payload={"blocker_owner": "verifier_tooling", "packet_signature": "p2"},
    ))
    assert _verified_blocker_receipt(ledger, step=2, packet_signature="p1") == ("", False)
    assert _verified_blocker_receipt(ledger, step=2, packet_signature="p2") == ("verifier_tooling", True)
