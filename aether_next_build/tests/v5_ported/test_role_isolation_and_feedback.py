import json

import pytest

from aether_next import (
    EvidenceRecord,
    Finding,
    HarnessRuntime,
    OutcomeOwner,
    TaskJudgement,
    VerificationOutcome,
    VerificationStatus,
    VerifierActivation,
    build_verifier_packet,
)


def test_solver_prefix_excludes_verifier_prompt_and_strategy(contract, world, config_factory):
    raw = config_factory()
    raw["verifier_system_prompt"] = "SECRET_VERIFIER_PROMPT"
    raw["verifier_strategy"]["false_positive_traps"].append("SECRET_TRAP")
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    request, _ = runtime.request()
    text = request.decode()
    assert "SECRET_VERIFIER_PROMPT" not in text
    assert "SECRET_TRAP" not in text
    assert raw["solver_system_prompt"] in text


def test_verifier_packet_is_state_only_and_excludes_solver_journey(contract, world, config_factory):
    raw = config_factory()
    raw["solver_system_prompt"] = "SECRET_SOLVER_PROMPT"
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    runtime.record_action_result(action_id="journey-step", action_kind="run_command", result={"stdout": "SOLVER_JOURNEY_SECRET"})
    packet = build_verifier_packet(
        contract=contract,
        envmap=runtime.envmap,
        world=world,
        compiled=runtime.compiled,
        snapshot_id="snapshot-1",
    )
    text = packet.json
    assert "SECRET_SOLVER_PROMPT" not in text
    assert "journey-step" not in text
    assert "SOLVER_JOURNEY_SECRET" not in text
    assert raw["verifier_system_prompt"] in text
    assert '"snapshot_id":"snapshot-1"' in text


def test_needs_repair_findings_become_dynamic_solver_context(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    outcome = VerificationOutcome(
        TaskJudgement.NEEDS_REPAIR,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.SOLVER_STATE,
        "wrong exact value",
        findings=(Finding("f-new", "c_value", "beta", "alpha", "write alpha"),),
    )
    runtime.apply_verifier_outcome(outcome)
    request, _ = runtime.request()
    assert world.active_findings[0]["finding_id"] == "f-new"
    assert '"finding_id":"f-new"' in request.decode()


def test_infrastructure_failure_does_not_replace_active_solver_findings(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    before = list(world.active_findings)
    outcome = VerificationOutcome(TaskJudgement.NOT_JUDGED, VerificationStatus.FAILED, OutcomeOwner.VERIFIER_TOOLING, "tool missing")
    runtime.apply_verifier_outcome(outcome, packet_signature="infra")
    assert world.active_findings == before


def test_completion_clears_resolved_findings(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    activation = VerifierActivation("clear", max_model_turns=3)
    activation.begin_model_turn()
    ids = activation.request_inspections((
        {"route": "read_file:/app/out.txt"},
        {"route": "run_command:python-byte-compare"},
    ))
    outcome = VerificationOutcome(
        TaskJudgement.COMPLETED,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.NONE,
        "all correct",
        evidence=(
            EvidenceRecord(ids[0], ("c_file",), "exact_contract", "exists", "remove"),
            EvidenceRecord(ids[1], ("c_value",), "independent_semantic", "alpha", "change"),
        ),
    )
    runtime.apply_verifier_outcome(outcome, activation=activation)
    assert world.active_findings == []


def test_verifier_finding_unknown_clause_is_rejected(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    outcome = VerificationOutcome(
        TaskJudgement.NEEDS_REPAIR,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.SOLVER_STATE,
        "bad",
        findings=(Finding("f", "unknown", "x", "y", "fix"),),
    )
    with pytest.raises(ValueError, match="unknown task clauses"):
        runtime.apply_verifier_outcome(outcome)


def test_verifier_packet_is_frozen_against_later_world_mutation(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    packet = build_verifier_packet(contract=contract, envmap=runtime.envmap, world=world, compiled=runtime.compiled, snapshot_id="snap")
    before = packet.json
    before_hash = packet.sha256
    world.services["web"]["state"] = "mutated-later"
    world.active_findings.append({"finding_id": "late"})
    assert packet.json == before
    assert packet.sha256 == before_hash


def test_context_epoch_event_is_immutable_after_append(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    event = {"kind": "external", "nested": {"value": "before"}}
    runtime.context.append_event(event)
    event["nested"]["value"] = "after"
    request, _ = runtime.request()
    assert '"value":"before"' in request.decode()
    assert '"value":"after"' not in request.decode()
