from __future__ import annotations

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.completion import CompletionGate
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    CompletionPolicy,
    EnvMap,
    RuntimeConfigIR,
)
from aether_next.verifier import (
    CompletionEvidenceEntry,
    ModelVerifierResult,
    VerifierFinding,
)


def _compiled():
    env = EnvMap(
        task_prompt="Implement the exact public protocol.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "files", tool_names=("read_file", "write_file")),
        },
    )
    ir = RuntimeConfigIR(
        architect_summary="Implement and prove protocol.",
        solver_identity_prompt="Use independent proof.",
        selected_capabilities=("shell", "filesystem"),
        completion_policy=CompletionPolicy(
            require_authoritative_check=False,
            allow_evidence_fallback=True,
            require_all_obligations=False,
            require_recent_progress=False,
            require_clean_integrity=True,
        ),
        inspection_plan=("inspect contract",),
        proof_plan=("run independent client",),
        semantic_clause_coverage=({
            "clause_id": "protocol",
            "solver_handling": "implement key/value fields exactly",
            "verifier_check": "independent client round trip",
        },),
        semantic_verifier_checks=({
            "clause_id": "protocol",
            "inspection_route": "overlay_run_command:python3 independent_client.py",
            "fallback_route": None,
            "falsification_check": "required request fails or response differs",
            "required_evidence_class": "exact_contract",
        },),
        semantic_false_positive_traps=("port open",),
    )
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)


def test_completed_verifier_clause_evidence_becomes_durable_proof() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt(
        receipt_id="inspection-client",
        step=4,
        kind="inspection_record",
        success=True,
        summary="independent client round trip passed",
        payload={
            "inspection_id": "inspection-client",
            "request_id": "client",
            "route_kind": "overlay_run_command",
            "route": "overlay_run_command:python3 independent_client.py",
            "target_identity": "target:independent-client",
            "target_generation": "result:client-pass",
            "task_state_generation": ledger.task_state_generation(),
            "tool_identity": "test.verifier_overlay",
            "result_hash": "client-pass-hash",
            "evidence_ceiling": "exact_contract",
            "eligible_for_proof": True,
        },
    ))
    ledger.apply_verifier_result(
        ModelVerifierResult(
            verdict="completed",
            confidence="high",
            summary="all clauses independently verified",
            completion_evidence=(CompletionEvidenceEntry(
                requirement="exact public protocol",
                observed="independent client set and read the same value",
                falsification_check="wrong field or response would fail",
                inspection_refs=("inspection-client",),
                clause_ids=("protocol",),
                evidence_class="exact_contract",
            ),),
        ),
        step=4,
        compiled=compiled,
    )
    proof = ledger.latest_receipt("proof_evidence")
    assert proof is not None
    assert proof.payload["clause_id"] == "protocol"
    assert proof.payload["provenance"] == "verifier_inspection"
    assert proof.payload["inspection_refs"] == ["inspection-client"]
    assert CompletionGate().evaluate(compiled, ledger, []).ready is True


def test_completed_verdict_without_clause_ids_does_not_bypass_contract() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    ledger.apply_verifier_result(
        ModelVerifierResult(
            verdict="completed",
            summary="looks correct",
            completion_evidence=(CompletionEvidenceEntry(
                requirement="something",
                observed="shape exists",
                falsification_check="none",
                inspection_refs=("inspection-shape",),
                clause_ids=(),
                evidence_class="shape",
            ),),
        ),
        step=4,
        compiled=compiled,
    )
    decision = CompletionGate().evaluate(compiled, ledger, [])
    assert decision.ready is False
    assert any(blocker.code == "missing_clause_evidence" for blocker in decision.blockers)


def test_clause_scoped_verifier_finding_becomes_durable_disproof() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    ledger.apply_verifier_result(
        ModelVerifierResult(
            verdict="needs_repair",
            findings=(VerifierFinding(
                finding_id="wrong-field",
                created_step=3,
                verdict="needs_repair",
                priority="blocking",
                summary="required field value is missing; candidate exposes val",
                evidence=("independent client rejected value",),
                repair_instruction="rename the request field and rerun the client",
                applies_to=("protocol",),
            ),),
        ),
        step=3,
        compiled=compiled,
    )
    proof = ledger.latest_receipt("proof_evidence")
    assert proof is not None
    assert proof.success is False
    assert proof.payload["finding_id"] == "wrong-field"
    decision = CompletionGate().evaluate(compiled, ledger, [])
    assert decision.ready is False
    assert any(blocker.code == "clause_disproved" for blocker in decision.blockers)
