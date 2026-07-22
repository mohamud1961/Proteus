"""Root-cause fixes from the 2026-07-05 real run (log-summary-date-ranges):

1. The architect must demand method-diverse, adversarial self-verification —
   the live failure was a solver validating wrong counts with the same
   interpretation that produced them.
2. Shape-only visible smoke checks compile as runnable but never as
   authoritative semantic proof.
3. A solver that keeps submitting past legible completion findings on a clean
   workbench is classified model_limit, not verification_failure — the
   verifier did its job.
"""
from __future__ import annotations

from aether_next.classifier import HarnessLimiterClassifier
from aether_next.kernel import KernelResult
from aether_next.ledger import Receipt
from aether_next.workbench_hooks import WORKBENCH_ARCHITECT_SYSTEM_PROMPT


def test_architect_prompt_carries_independent_verification_discipline() -> None:
    prompt = WORKBENCH_ARCHITECT_SYSTEM_PROMPT
    for required in (
        "raw-input inspection",
        "genuinely\n  independent of the production method",
        "manual spot-audit",
        "same method or assumption that produced it",
        "same-method self-confirmation trap",
    ):
        assert required in prompt, f"architect prompt lost discipline clause: {required!r}"


def test_visible_smoke_checks_compile_runnable_but_not_authoritative() -> None:
    from aether_next.runtime_ir import CapabilityDescriptor, EnvMap
    from aether_next.smoke_compile import compile_visible_smoke_tests
    from aether_next.workbench_config import parse_harness_config_ir
    import json

    config = parse_harness_config_ir(json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "smoke flag test",
        "success_definition": "out.csv is semantically correct",
        "solver_system_prompt": {
            "role": "solver", "workflow": ["inspect", "build"],
            "self_verification": ["audit"], "memory_use": ["auto"],
            "stop_conditions": ["ready"],
        },
        "verifier_system_prompt": {
            "role": "verifier", "success_criteria": ["correct"],
            "required_evidence": ["state"], "false_positive_traps": ["shape"],
            "verdict_guidance": ["state"], "feedback_guidance": ["concrete"],
        },
        "evidence_requirements": ["current out.csv state"],
        "false_positive_risks": ["shape-only green"],
        "minimum_completion_evidence": ["state"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "run_command"]},
        "context_policy": {"mode": "retrieval_augmented"},
        "model_verifier_policy": {"enabled": True},
        "verification_policy": {
            "visible_smoke_tests": [{"type": "file_exists", "path": "out.csv"}],
        },
    }))
    env = EnvMap(
        task_prompt="produce out.csv",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands"),
            "filesystem": CapabilityDescriptor("filesystem", "Files"),
        },
    )
    result = compile_visible_smoke_tests(config, env)
    assert result.checks, "smoke spec should compile into a runnable check"
    for check in result.checks:
        assert check.authoritative is False
        assert "shape-only" in check.label


def _receipts_for_stalemate(*, with_verifier_result: bool) -> tuple[Receipt, ...]:
    rows = [
        Receipt("w1", 0, "write_file", True, "wrote out.csv", state_change=True,
                payload={"path": "out.csv"}),
        Receipt("c1", 0, "run_command", True, "ran recount", payload={"command": "python3 recount.py"}),
    ]
    if with_verifier_result:
        rows.append(Receipt(
            "v1", 1, "model_verifier_result", False,
            "model verifier verdict: needs_repair", failure_class="needs_repair",
            payload={"verdict": "needs_repair"},
        ))
    rows.append(Receipt(
        "s1", 4, "solver_submit_stalemate", False,
        "solver submit stalemate", failure_class="solver_submit_stalemate",
        payload={"rounds": 3},
    ))
    return tuple(rows)


def test_submit_stalemate_on_clean_workbench_is_model_limit() -> None:
    result = KernelResult(
        status="solver_submit_stalemate", step=4, reconfigurations=0,
        blockers=("f-1",), receipts=_receipts_for_stalemate(with_verifier_result=True),
    )
    classification = HarnessLimiterClassifier().classify(result)
    assert classification.label == "model_limit"
    assert classification.label != "verification_failure"


def test_submit_stalemate_without_delivered_feedback_is_not_blamed_on_model() -> None:
    result = KernelResult(
        status="solver_submit_stalemate", step=4, reconfigurations=0,
        blockers=("f-1",), receipts=_receipts_for_stalemate(with_verifier_result=False),
    )
    classification = HarnessLimiterClassifier().classify(result)
    assert classification.label == "harness_context_failure"


def test_turn_parser_infers_act_and_tolerates_missing_boilerplate() -> None:
    """Protocol ergonomics: dozens of live solver turns were burned on
    'missing required field: kind' (with actions present) and missing
    intent/expected_observation boilerplate.  The payload implies the turn."""
    import pytest
    from aether_next.model_hooks import ModelOutputError, parse_solver_turn

    turn = parse_solver_turn(
        '{"actions":[{"kind":"run_command","arguments":{"command":"ls"}}]}'
    )
    assert turn.kind == "act"
    assert turn.actions[0].action_id  # autofilled
    assert not turn.validate()  # no boilerplate errors
    assert not turn.actions[0].validate()

    # Submission is never inferred: no kind and no actions stays a hard error.
    with pytest.raises(ModelOutputError):
        parse_solver_turn('{"summary":"done i think"}')


def test_architect_prompt_requires_transcript_producing_self_checks() -> None:
    for required in (
        "PRINT the observed evidence",
        'never a bare "OK"/"PASS"',
        "your check output is your evidence",
    ):
        assert required in WORKBENCH_ARCHITECT_SYSTEM_PROMPT, required
