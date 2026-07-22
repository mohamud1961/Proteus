"""Tests for CompletionGate — artifact satisfaction via existence checks
and no-check-passed blocker."""
from __future__ import annotations

from aether_next.completion import Blocker, CompletionGate
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.verifier import ModelVerifierResult, VerifierFinding
from aether_next.runtime_ir import (
    CheckSpec,
    CompletionPolicy,
    CompiledRuntime,
    ContextPolicy,
    DeliverableSpec,
    EvalIndex,
    ObjectiveGraph,
    ProcessPolicy,
    HelperToolPolicy,
    BootstrapPolicy,
    ProofObligation,
    ReconfigurePolicy,
    RefusalPolicy,
)


def _compiled(
    *,
    deliverables: tuple[DeliverableSpec, ...] = (),
    obligations: tuple[ProofObligation, ...] = (),
    checks: tuple[CheckSpec, ...] = (),
    check_plan_ids: tuple[str, ...] = (),
    require_authoritative_check: bool = True,
    allow_evidence_fallback: bool = True,
    require_all_obligations: bool = True,
    require_recent_progress: bool = False,
) -> CompiledRuntime:
    """Build a minimal CompiledRuntime for gate tests."""
    return CompiledRuntime(
        task_prompt="test task",
        env_digest="abc123",
        objective_graph=ObjectiveGraph(
            deliverables=deliverables,
            obligations=obligations,
        ),
        eval_index=EvalIndex(checks=checks),
        selected_capabilities=(),
        stable_prefix_sections=(),
        context_policy=ContextPolicy(),
        process_policy=ProcessPolicy(),
        helper_tool_policy=HelperToolPolicy(),
        bootstrap_policy=BootstrapPolicy(),
        completion_policy=CompletionPolicy(
            require_authoritative_check=require_authoritative_check,
            allow_evidence_fallback=allow_evidence_fallback,
            require_all_obligations=require_all_obligations,
            require_recent_progress=require_recent_progress,
            require_clean_integrity=False,
        ),
        refusal_policy=RefusalPolicy(),
        reconfigure_policy=ReconfigurePolicy(),
        enforced_monitors=(),
        check_plan_ids=check_plan_ids,
        forbidden_paths=(),
    )


def _check_spec(path: str, check_id: str = "chk-exist-1") -> CheckSpec:
    return CheckSpec(
        check_id=check_id,
        label=f"exists:{path}",
        command=f"test -e {path}",
        origin="contract",
        authoritative=True,
    )


def _existence_receipt(
    path: str,
    *,
    success: bool = True,
    receipt_id: str = "r-check-1",
    check_id: str = "chk-exist-1",
    step: int = 2,
) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        step=step,
        kind="check_result",
        success=success,
        summary=f"test -e {path} {'passed' if success else 'failed'}",
        state_change=success,
        payload={
            "check_id": check_id,
            "command": f"test -e {path}",
            "passed": success,
            "origin": "contract",
        },
    )


class TestGatePassesWhenExistenceCheckSatisfiesShellCreatedArtifact:
    def test_shell_created_artifact_with_passing_check(self) -> None:
        """An artifact created by shell (not write_file) with a passing
        existence check should satisfy both missing_artifacts and
        unsatisfied_obligations, so the gate returns ready=True."""
        path = "out.txt"
        check = _check_spec(path)
        compiled = _compiled(
            deliverables=(DeliverableSpec(path=path, required=True),),
            obligations=(
                ProofObligation(
                    obligation_id=f"artifact:{path}",
                    kind="artifact",
                    description=f"{path} must exist",
                    target=path,
                ),
            ),
            checks=(check,),
            check_plan_ids=(check.check_id,),
        )

        ledger = ExecutionLedger()
        ledger.ensure_objective(compiled.objective_graph)

        # Simulate a shell command creating the file (no write_file receipt).
        ledger.record(Receipt(
            receipt_id="r-cmd-1",
            step=1,
            kind="run_command",
            success=True,
            summary="openssl genrsa > out.txt",
            state_change=True,
            payload={"command": "openssl genrsa > out.txt"},
        ))

        # Record the passing existence check.
        ledger.record(_existence_receipt(path))

        gate = CompletionGate()
        decision = gate.evaluate(compiled, ledger, [])

        blocker_codes = {b.code for b in decision.blockers}
        assert "missing_artifacts" not in blocker_codes, (
            f"missing_artifacts should not block: {decision.blockers}"
        )
        assert "unsatisfied_obligations" not in blocker_codes, (
            f"unsatisfied_obligations should not block: {decision.blockers}"
        )
        assert decision.ready is True, (
            f"Expected ready=True, blockers={decision.blockers}"
        )


class TestGateBlocksWhenChecksDefinedButNonePassed:
    def test_no_checks_recorded(self) -> None:
        """When planned checks exist but none have been recorded/passed,
        the gate must block with no_authoritative_check_passed."""
        check = _check_spec("result.json")
        compiled = _compiled(
            checks=(check,),
            check_plan_ids=(check.check_id,),
            require_authoritative_check=True,
        )

        ledger = ExecutionLedger()
        ledger.ensure_objective(compiled.objective_graph)

        gate = CompletionGate()
        decision = gate.evaluate(compiled, ledger, [])

        assert decision.ready is False
        blocker_codes = {b.code for b in decision.blockers}
        assert "no_authoritative_check_passed" in blocker_codes or \
               "missing_authoritative_check" in blocker_codes

    def test_all_checks_failed(self) -> None:
        """When planned checks exist and all have been recorded but all
        failed, the gate must block."""
        check = _check_spec("result.json")
        compiled = _compiled(
            checks=(check,),
            check_plan_ids=(check.check_id,),
            require_authoritative_check=True,
        )

        ledger = ExecutionLedger()
        ledger.ensure_objective(compiled.objective_graph)
        ledger.record(_existence_receipt(
            "result.json",
            success=False,
            check_id=check.check_id,
        ))

        gate = CompletionGate()
        decision = gate.evaluate(compiled, ledger, [])

        assert decision.ready is False
        blocker_codes = {b.code for b in decision.blockers}
        assert "no_authoritative_check_passed" in blocker_codes

    def test_no_planned_checks_evidence_fallback_allowed(self) -> None:
        """When there are NO planned checks and evidence fallback is
        allowed, the no_authoritative_check_passed blocker must NOT fire."""
        compiled = _compiled(
            checks=(),
            check_plan_ids=(),
            require_authoritative_check=True,
            allow_evidence_fallback=True,
            require_all_obligations=False,
        )

        ledger = ExecutionLedger()
        gate = CompletionGate()
        decision = gate.evaluate(compiled, ledger, [])

        blocker_codes = {b.code for b in decision.blockers}
        assert "no_authoritative_check_passed" not in blocker_codes


class TestGateBlocksOnVerifierAndAutomaticMemoryEvidence:
    def test_active_blocking_verifier_finding_blocks_completion(self) -> None:
        compiled = _compiled(
            require_authoritative_check=False,
            require_all_obligations=False,
        )
        ledger = ExecutionLedger()
        ledger.ensure_objective(compiled.objective_graph)
        finding = VerifierFinding(
            finding_id="vf-block",
            created_step=1,
            verdict="needs_repair",
            priority="blocking",
            summary="artifact is not semantically verified",
            repair_instruction="repair artifact",
            applies_to=("out.txt",),
        )
        ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=1)

        decision = CompletionGate().evaluate(compiled, ledger, [])

        assert decision.ready is False
        assert "active_verifier_finding" in {b.code for b in decision.blockers}

    def test_automatic_memory_repeat_advisory_does_not_block_completion(self) -> None:
        compiled = _compiled(
            require_authoritative_check=False,
            require_all_obligations=False,
        )
        ledger = ExecutionLedger()
        ledger.ensure_objective(compiled.objective_graph)
        ledger.record(Receipt(
            receipt_id="auto-block",
            step=2,
            kind="automatic_memory_advisory",
            success=True,
            summary="automatic memory noted an exact repeated action",
            failure_class="",
        ))

        decision = CompletionGate().evaluate(compiled, ledger, [])

        assert decision.ready is True
        assert "automatic_memory_repeat_block" not in {b.code for b in decision.blockers}
