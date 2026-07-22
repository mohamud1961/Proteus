"""Tests for ExecutionLedger — artifact reconciliation via existence checks."""
from __future__ import annotations

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.context_compiler import ContextCompiler
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    CheckSpec,
    CompletionPolicy,
    DeliverableSpec,
    EnvMap,
    EvalIndex,
    ObjectiveGraph,
    ProofObligation,
    RuntimeConfigIR,
)


def _make_objective(path: str) -> ObjectiveGraph:
    """Build an ObjectiveGraph requiring a single deliverable with an artifact obligation."""
    return ObjectiveGraph(
        deliverables=(DeliverableSpec(path=path, required=True),),
        obligations=(
            ProofObligation(
                obligation_id=f"artifact:{path}",
                kind="artifact",
                description=f"File {path} must exist",
                target=path,
            ),
        ),
    )


def _check_receipt(
    *,
    receipt_id: str = "r-check-1",
    step: int = 1,
    success: bool = True,
    command: str = "test -e ssl/server.key",
    check_id: str = "chk-exist-1",
) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        step=step,
        kind="check_result",
        success=success,
        summary=f"check {'passed' if success else 'failed'}: {command}",
        payload={
            "check_id": check_id,
            "command": command,
            "passed": success,
            "origin": "contract",
        },
    )


class TestExistenceCheckSatisfiesArtifact:
    def test_passing_existence_check_marks_artifact_present(self) -> None:
        """A passing 'test -e <path>' check_result must add the path to
        current_artifacts and satisfy the artifact obligation."""
        ledger = ExecutionLedger()
        ledger.ensure_objective(_make_objective("ssl/server.key"))

        assert "ssl/server.key" not in ledger.current_artifacts()
        assert "artifact:ssl/server.key" not in ledger.satisfied_obligation_ids()

        ledger.record(_check_receipt(
            command="test -e ssl/server.key",
            success=True,
        ))

        assert "ssl/server.key" in ledger.current_artifacts()
        assert "artifact:ssl/server.key" in ledger.satisfied_obligation_ids()

    def test_failed_existence_check_does_not_mark_present(self) -> None:
        """A failed 'test -e <path>' check must NOT mark the artifact present."""
        ledger = ExecutionLedger()
        ledger.ensure_objective(_make_objective("ssl/server.key"))

        ledger.record(_check_receipt(
            receipt_id="r-fail-1",
            command="test -e ssl/server.key",
            success=False,
        ))

        assert "ssl/server.key" not in ledger.current_artifacts()
        assert "artifact:ssl/server.key" not in ledger.satisfied_obligation_ids()

    def test_non_existence_check_not_affected(self) -> None:
        """A passing check whose command is not 'test -e ...' must not
        accidentally add artifacts."""
        ledger = ExecutionLedger()
        ledger.ensure_objective(_make_objective("result.json"))

        ledger.record(Receipt(
            receipt_id="r-other-1",
            step=1,
            kind="check_result",
            success=True,
            summary="schema check passed",
            payload={
                "check_id": "chk-schema-1",
                "command": "python3 -c \"import json; json.load(open('result.json'))\"",
                "passed": True,
                "origin": "contract",
            },
        ))

        assert "result.json" not in ledger.current_artifacts()

    def test_write_file_still_works(self) -> None:
        """Existing artifact_paths handling must remain functional."""
        ledger = ExecutionLedger()
        ledger.ensure_objective(_make_objective("out.txt"))

        ledger.record(Receipt(
            receipt_id="r-write-1",
            step=1,
            kind="write_file",
            success=True,
            summary="wrote out.txt",
            payload={"artifact_paths": ["out.txt"]},
        ))

        assert "out.txt" in ledger.current_artifacts()
        assert "artifact:out.txt" in ledger.satisfied_obligation_ids()


class TestEnrichedSolverContext:
    def test_context_surfaces_repeats_read_files_failure_kind_and_stuck(self) -> None:
        envmap = EnvMap(
            task_prompt="Write results.json.",
            workspace_root="/app",
            capabilities={
                "shell": CapabilityDescriptor(capability_id="shell", summary="Run commands"),
                "filesystem": CapabilityDescriptor(capability_id="filesystem", summary="Read/write files"),
            },
        )
        check = CheckSpec(
            check_id="chk-results",
            label="exists:results.json",
            command="test -e results.json",
            origin="contract",
        )
        objective = _make_objective("results.json")
        eval_index = EvalIndex(checks=(check,))
        ir = RuntimeConfigIR(
            architect_summary="summary",
            solver_identity_prompt="solver",
            selected_capabilities=("shell", "filesystem"),
            completion_policy=CompletionPolicy(require_authoritative_check=True),
            check_plan=(check.check_id,),
            inspection_plan=("inspect",),
            proof_plan=("prove",),
        )
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        compiled = compiler.compile(ir, envmap, objective_graph=objective, eval_index=eval_index)

        ledger = ExecutionLedger()
        ledger.ensure_objective(objective)
        ledger.record(Receipt(
            receipt_id="read-1",
            step=1,
            kind="read_file",
            success=True,
            summary="read input.txt",
            payload={"path": "input.txt"},
        ))
        ledger.record(Receipt(
            receipt_id="read-2",
            step=2,
            kind="read_file",
            success=True,
            summary="read input.txt again",
            payload={"path": "input.txt"},
        ))
        ledger.record(Receipt(
            receipt_id="cmd-1",
            step=3,
            kind="run_command",
            success=False,
            summary="command failed",
            failure_class="command_failure",
            payload={"command": "test -e results.json"},
        ))
        ledger.record(_check_receipt(
            receipt_id="check-1",
            step=4,
            success=False,
            command="test -e results.json",
            check_id=check.check_id,
        ))

        packet = ContextCompiler().compile(compiled, ledger, alerts=[])

        assert packet["pending_checks"][0]["failure_kind"] == "check_failed"
        assert "Create or write the required artifact" in packet["pending_checks"][0]["repair_hint"]
        assert packet["files_already_read"][0]["path"] == "input.txt"
        assert packet["files_already_read"][0]["read_count"] == 2
        assert packet["repeated_actions"][0]["action"] == "read_file:input.txt"
        assert packet["stuck"]["no_progress"] is True
