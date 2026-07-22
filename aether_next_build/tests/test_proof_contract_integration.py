from __future__ import annotations

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.completion import CompletionGate
from aether_next.ledger import ExecutionLedger
from aether_next.proof_contract import record_clause_evidence
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    CompletionPolicy,
    EnvMap,
    RuntimeConfigIR,
)


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Implement an exact local request/response protocol.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "run commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor(
                "filesystem", "read and write files", tool_names=("read_file", "write_file")
            ),
        },
    )


def _runtime(route: str) -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="Implement and prove the exact protocol contract.",
        solver_identity_prompt="Use the exact public contract and test it independently.",
        selected_capabilities=("shell", "filesystem"),
        completion_policy=CompletionPolicy(
            require_authoritative_check=False,
            allow_evidence_fallback=True,
            require_all_obligations=False,
            require_recent_progress=False,
            require_clean_integrity=True,
        ),
        inspection_plan=("inspect the independent client contract",),
        proof_plan=("run the independent client after implementation",),
        semantic_clause_coverage=({
            "clause_id": "protocol:set",
            "solver_handling": "implement key and value fields exactly",
            "verifier_check": "independent client sets and reads the same value",
        },),
        semantic_verifier_checks=({
            "clause_id": "protocol:set",
            "inspection_route": route,
            "fallback_route": None,
            "falsification_check": "required value field is rejected or round trip differs",
            "required_evidence_class": "exact_contract",
        },),
        semantic_false_positive_traps=("open port is not protocol proof",),
    )


def _compile(route: str):
    env = _env()
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(_runtime(route), env)


def test_compiler_rejects_port_liveness_as_exact_protocol_proof() -> None:
    with pytest.raises(ValueError) as exc_info:
        _compile("probe_port:5328")
    assert "proof_route_strength_insufficient" in str(exc_info.value)
    assert "metadata_proxy" in str(exc_info.value)


def test_compiler_records_certified_exact_protocol_contract() -> None:
    compiled = _compile("overlay_run_command:python3 independent_client.py")
    assert len(compiled.proof_contract) == 1
    clause = compiled.proof_contract[0]
    assert clause["clause_id"] == "protocol:set"
    assert clause["route_evidence_ceiling"] == "exact_contract"
    assert compiled.config_realization["certified_proof_contract"] == list(compiled.proof_contract)


def test_completion_blocks_live_port_proxy_for_exact_protocol_clause() -> None:
    compiled = _compile("overlay_run_command:python3 independent_client.py")
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    record_clause_evidence(
        ledger,
        receipt_id="port-open",
        step=1,
        clause_id="protocol:set",
        route="probe_port:5328",
        evidence_class="metadata_proxy",
        provenance="independent_interface_probe",
        supports_clause=True,
        observation="port is open",
    )
    decision = CompletionGate().evaluate(compiled, ledger, [])
    assert decision.ready is False
    assert any(blocker.code == "insufficient_clause_evidence" for blocker in decision.blockers)


def test_completion_blocks_same_schema_self_test() -> None:
    compiled = _compile("overlay_run_command:python3 independent_client.py")
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    record_clause_evidence(
        ledger,
        receipt_id="same-schema-client",
        step=1,
        clause_id="protocol:set",
        route="overlay_run_command:python3 independent_client.py",
        evidence_class="exact_contract",
        provenance="solver_authored_check",
        supports_clause=True,
        observation="candidate-generated client passed candidate schema",
    )
    decision = CompletionGate().evaluate(compiled, ledger, [])
    assert decision.ready is False
    assert any(blocker.code == "insufficient_clause_evidence" for blocker in decision.blockers)


def test_completion_accepts_independent_exact_protocol_probe() -> None:
    compiled = _compile("overlay_run_command:python3 independent_client.py")
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
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
    decision = CompletionGate().evaluate(compiled, ledger, [])
    assert decision.ready is True
    assert decision.blockers == ()


def test_later_independent_disproof_blocks_completion_again() -> None:
    compiled = _compile("overlay_run_command:python3 independent_client.py")
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
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
        receipt_id="contradiction",
        step=3,
        clause_id="protocol:set",
        route="overlay_run_command:python3 independent_client.py",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=False,
        observation="required field value is rejected; candidate exposes val",
    )
    decision = CompletionGate().evaluate(compiled, ledger, [])
    assert decision.ready is False
    assert any(blocker.code == "clause_disproved" for blocker in decision.blockers)
