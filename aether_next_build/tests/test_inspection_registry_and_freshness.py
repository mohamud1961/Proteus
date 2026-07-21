"""Adversarial closure tests for inspection identity and proof freshness."""
from __future__ import annotations

from types import SimpleNamespace

from aether_next.inspection_registry import register_inspection_results
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.proof_contract import (
    CertifiedProofClause,
    evaluate_proof_contract,
    record_clause_evidence,
    record_verifier_result_evidence,
)
from aether_next.verifier import CompletionEvidenceEntry, ModelVerifierResult
from aether_next.verifier_inspector import VerifierInspectionRequest
from aether_next.verifier_recovery import (
    CompiledEvidenceRequirement,
    EvidenceClass,
    validate_compiled_evidence,
)


class _Executor:
    pass


def _clause(route: str = "read_file:out.txt") -> CertifiedProofClause:
    return CertifiedProofClause(
        clause_id="result",
        requirement="out.txt contains the exact result",
        solver_handling="write exact result",
        verifier_route=route,
        fallback_route="",
        falsification_check="a different current value disproves the clause",
        required_evidence_class="exact_contract",
        route_kind=route.split(":", 1)[0],
        route_evidence_ceiling="exact_contract",
        requires_independent_evidence=True,
    )


def test_registry_binds_actual_route_target_generation_tool_hash_and_ceiling() -> None:
    ledger = ExecutionLedger()
    request = VerifierInspectionRequest(
        request_id="read-current",
        kind="read_file",
        path="out.txt",
    )
    rows = register_inspection_results(
        (request,),
        ({
            "request_id": "read-current",
            "kind": "read_file",
            "path": "out.txt",
            "content_hash": "abc123",
            "excerpt": "42",
            "read_only": True,
        },),
        ledger=ledger,
        step=3,
        requester="model_verifier",
        executor=_Executor(),
        overlay=None,
        packet_signature="packet-generation-3",
    )

    row = rows[0]
    inspection_id = row["inspection_id"]
    receipt = next(item for item in ledger.all_receipts() if item.receipt_id == inspection_id)
    assert receipt.kind == "inspection_record"
    assert receipt.payload["route"] == "read_file:out.txt"
    assert receipt.payload["target_identity"] == "path:out.txt"
    assert receipt.payload["target_generation"] == "content_hash:abc123"
    assert receipt.payload["task_state_generation"] == 0
    assert receipt.payload["tool_identity"].endswith(":task_executor")
    assert receipt.payload["result_hash"]
    assert receipt.payload["evidence_ceiling"] == "exact_contract"
    assert row["eligible_for_proof"] is True


def test_overlay_registry_route_binds_exact_executed_command() -> None:
    ledger = ExecutionLedger()
    request = VerifierInspectionRequest(
        request_id="client",
        kind="overlay_run_command",
        command="python3 independent_client.py",
    )
    rows = register_inspection_results(
        (request,),
        ({
            "request_id": "client",
            "kind": "overlay_run_command",
            "command": "python3 independent_client.py",
            "exit_code": 0,
            "stdout": "ok",
            "executed_in": "verifier_overlay",
        },),
        ledger=ledger,
        step=1,
        requester="model_verifier",
        executor=_Executor(),
        overlay=_Executor(),
        packet_signature="packet",
    )
    assert rows[0]["registered_route"] == "overlay_run_command:python3 independent_client.py"
    assert rows[0]["target_identity"] == "command:python3 independent_client.py"


def test_unclassified_inspection_route_cannot_become_proof() -> None:
    ledger = ExecutionLedger()
    request = VerifierInspectionRequest(request_id="unknown", kind="unknown_route")
    rows = register_inspection_results(
        (request,),
        ({"request_id": "unknown", "kind": "unknown_route", "value": "x"},),
        ledger=ledger,
        step=0,
        requester="model_verifier",
        executor=_Executor(),
        overlay=None,
        packet_signature="packet",
    )
    assert rows[0]["evidence_ceiling"] == ""
    assert rows[0]["eligible_for_proof"] is False


def test_missing_inspection_ceiling_fails_closed() -> None:
    evidence = (SimpleNamespace(
        inspection_refs=("inspection:1",),
        clause_ids=("result",),
        evidence_class="exact_contract",
        falsification_check="different result",
    ),)
    errors = validate_compiled_evidence(
        evidence,
        requirements=(CompiledEvidenceRequirement("result", EvidenceClass.EXACT_CONTRACT),),
        known_inspection_ids=("inspection:1",),
        inspection_ceilings={},
    )
    assert any(error.code == "missing_inspection_ceiling" for error in errors)


def test_current_proof_becomes_stale_after_any_task_mutation() -> None:
    ledger = ExecutionLedger()
    clause = _clause()
    record_clause_evidence(
        ledger,
        receipt_id="proof:0",
        step=0,
        clause_id="result",
        route="read_file:out.txt",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=True,
        observation="current out.txt is 42",
        inspection_ids=("inspection:0",),
    )
    assert evaluate_proof_contract((clause,), ledger)[0].satisfied is True

    ledger.record(Receipt(
        receipt_id="mutation:1",
        step=1,
        kind="write_file",
        success=True,
        summary="rewrote out.txt",
        state_change=True,
        payload={"modified_paths": ["out.txt"]},
    ))

    decision = evaluate_proof_contract((clause,), ledger)[0]
    assert decision.satisfied is False
    assert decision.code == "stale_clause_evidence"
    assert "current generation=1" in decision.detail


def test_control_plane_receipts_do_not_invalidate_current_proof() -> None:
    ledger = ExecutionLedger()
    clause = _clause()
    record_clause_evidence(
        ledger,
        receipt_id="proof:0",
        step=0,
        clause_id="result",
        route="read_file:out.txt",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=True,
        observation="current out.txt is 42",
    )
    ledger.record(Receipt(
        receipt_id="observation:1",
        step=1,
        kind="record_observation",
        success=True,
        summary="model restated a belief",
        state_change=True,
    ))
    assert ledger.task_state_generation() == 0
    assert evaluate_proof_contract((clause,), ledger)[0].satisfied is True


def test_proof_bridge_cannot_substitute_compiler_route_for_actual_inspection() -> None:
    ledger = ExecutionLedger()
    ledger.record(Receipt(
        receipt_id="inspection:port",
        step=2,
        kind="inspection_record",
        success=True,
        summary="port is open",
        payload={
            "inspection_id": "inspection:port",
            "route_kind": "probe_port",
            "route": "probe_port:5328",
            "target_identity": "target:5328",
            "target_generation": "result:open",
            "task_state_generation": 0,
            "tool_identity": "test.probe",
            "result_hash": "open-hash",
            "evidence_ceiling": "metadata_proxy",
            "eligible_for_proof": True,
        },
    ))
    compiled = SimpleNamespace(proof_contract=({
        "clause_id": "protocol",
        "verifier_route": "overlay_run_command:python3 independent_client.py",
        "fallback_route": "",
        "required_evidence_class": "exact_contract",
    },))
    result = ModelVerifierResult(
        verdict="completed",
        completion_evidence=(CompletionEvidenceEntry(
            requirement="exact protocol round trip",
            observed="port is open",
            falsification_check="client mismatch",
            inspection_refs=("inspection:port",),
            clause_ids=("protocol",),
            evidence_class="exact_contract",
        ),),
    )

    receipts = record_verifier_result_evidence(
        ledger,
        result=result,
        compiled=compiled,
        step=2,
    )

    assert receipts
    assert receipts[0].success is False
    assert receipts[0].payload["route"] == "unregistered_inspection"
    assert receipts[0].payload["source"] == "model_verifier_completion_evidence_rejected"
