from __future__ import annotations

from aether_next.ledger import ExecutionLedger
from aether_next.proof_contract import (
    certify_proof_contract,
    evaluate_proof_contract,
    record_clause_evidence,
)


def _coverage():
    return ({
        "clause_id": "protocol:set",
        "solver_handling": "implement the exact independent client contract",
        "verifier_check": "the required client can set and read a value",
    },)


def _checks(route: str, evidence_class: str = "exact_contract"):
    return ({
        "clause_id": "protocol:set",
        "inspection_route": route,
        "fallback_route": None,
        "falsification_check": "required client request is rejected or round trip differs",
        "required_evidence_class": evidence_class,
    },)


def test_port_probe_cannot_certify_exact_protocol_contract() -> None:
    clauses, issues = certify_proof_contract(_coverage(), _checks("probe_port:5328"))
    assert clauses == ()
    assert [issue.code for issue in issues] == ["proof_route_strength_insufficient"]
    assert "metadata_proxy" in issues[0].detail


def test_process_probe_cannot_certify_behavioral_protocol_contract() -> None:
    clauses, issues = certify_proof_contract(
        _coverage(), _checks("probe_process:service", "behavioral")
    )
    assert clauses == ()
    assert [issue.code for issue in issues] == ["proof_route_strength_insufficient"]


def test_independent_client_route_certifies_exact_protocol_contract() -> None:
    clauses, issues = certify_proof_contract(
        _coverage(), _checks("overlay_run_command:python3 independent_client.py")
    )
    assert issues == ()
    assert len(clauses) == 1
    assert clauses[0].route_evidence_ceiling == "exact_contract"
    assert clauses[0].requires_independent_evidence is True


def test_live_port_receipt_does_not_satisfy_certified_protocol_clause() -> None:
    clauses, issues = certify_proof_contract(
        _coverage(), _checks("overlay_run_command:python3 independent_client.py")
    )
    assert issues == ()
    ledger = ExecutionLedger()
    record_clause_evidence(
        ledger,
        receipt_id="port-open",
        step=1,
        clause_id="protocol:set",
        route="probe_port:5328",
        evidence_class="metadata_proxy",
        provenance="independent_interface_probe",
        supports_clause=True,
        observation="port 5328 is open",
    )
    decision = evaluate_proof_contract(clauses, ledger)[0]
    assert decision.satisfied is False
    assert decision.code == "insufficient_clause_evidence"


def test_same_wrong_schema_client_is_not_independent_proof() -> None:
    clauses, issues = certify_proof_contract(
        _coverage(), _checks("overlay_run_command:python3 independent_client.py")
    )
    assert issues == ()
    ledger = ExecutionLedger()
    record_clause_evidence(
        ledger,
        receipt_id="self-client",
        step=1,
        clause_id="protocol:set",
        route="overlay_run_command:python3 independent_client.py",
        evidence_class="exact_contract",
        provenance="solver_authored_check",
        supports_clause=True,
        observation="client generated from candidate schema passed",
    )
    decision = evaluate_proof_contract(clauses, ledger)[0]
    assert decision.satisfied is False
    assert decision.code == "insufficient_clause_evidence"


def test_independent_exact_client_proves_clause() -> None:
    clauses, issues = certify_proof_contract(
        _coverage(), _checks("overlay_run_command:python3 independent_client.py")
    )
    assert issues == ()
    ledger = ExecutionLedger()
    record_clause_evidence(
        ledger,
        receipt_id="independent-client",
        step=2,
        clause_id="protocol:set",
        route="overlay_run_command:python3 independent_client.py",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=True,
        observation="independent client set key/value and read the same value",
    )
    decision = evaluate_proof_contract(clauses, ledger)[0]
    assert decision.satisfied is True
    assert decision.code == "clause_proved"


def test_later_disproof_reopens_previously_proved_clause() -> None:
    clauses, issues = certify_proof_contract(
        _coverage(), _checks("overlay_run_command:python3 independent_client.py")
    )
    assert issues == ()
    ledger = ExecutionLedger()
    record_clause_evidence(
        ledger,
        receipt_id="pass",
        step=2,
        clause_id="protocol:set",
        route="overlay_run_command:python3 independent_client.py",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=True,
        observation="round trip passed",
    )
    record_clause_evidence(
        ledger,
        receipt_id="fail",
        step=3,
        clause_id="protocol:set",
        route="overlay_run_command:python3 independent_client.py",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=False,
        observation="required field value is rejected; candidate exposes val",
    )
    decision = evaluate_proof_contract(clauses, ledger)[0]
    assert decision.satisfied is False
    assert decision.code == "clause_disproved"
    assert "value" in decision.detail
