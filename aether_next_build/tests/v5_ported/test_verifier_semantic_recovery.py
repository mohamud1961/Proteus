import pytest

from aether_next import (
    EvidenceRecord,
    HarnessRuntime,
    OutcomeOwner,
    RouteAction,
    TaskJudgement,
    VerificationOutcome,
    VerificationRouter,
    VerificationStatus,
    VerifierActivation,
)


def _activation_with_two_inspections():
    activation = VerifierActivation("vx", max_model_turns=3)
    activation.begin_model_turn()
    ids = activation.request_inspections(({"route": "read_file:/app/out.txt"}, {"route": "run_command:python-byte-compare"}))
    return activation, ids


def test_completion_rejects_evidence_below_compiled_threshold(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    activation, ids = _activation_with_two_inspections()
    outcome = VerificationOutcome(
        TaskJudgement.COMPLETED,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.NONE,
        "looks correct",
        evidence=(
            EvidenceRecord(ids[0], ("c_file",), "exact_contract", "exists", "remove file"),
            EvidenceRecord(ids[1], ("c_value",), "behavioral", "self-test passed", "change byte"),
        ),
    )
    with pytest.raises(ValueError, match="evidence too weak for c_value"):
        runtime.apply_verifier_outcome(outcome, activation=activation)


def test_completion_rejects_unknown_inspection_reference(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    activation, ids = _activation_with_two_inspections()
    outcome = VerificationOutcome(
        TaskJudgement.COMPLETED,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.NONE,
        "correct",
        evidence=(
            EvidenceRecord(ids[0], ("c_file",), "exact_contract", "exists", "remove"),
            EvidenceRecord("invented", ("c_value",), "independent_semantic", "alpha", "change"),
        ),
    )
    with pytest.raises(ValueError, match="unknown inspection"):
        runtime.apply_verifier_outcome(outcome, activation=activation)


def test_verifier_activation_uses_fallback_route_once(contract, config_factory):
    from aether_next.config import compile_workbench_config

    compiled = compile_workbench_config(config_factory(), contract)
    check = compiled.config.verifier_strategy.clause_checks[0]
    activation = VerifierActivation("fallback", max_model_turns=4)
    activation.begin_model_turn()
    primary = activation.next_clause_route(check)
    assert primary == check.inspection_route
    activation.request_inspections(({"route": primary},))
    fallback = activation.next_clause_route(check, failed_route=primary)
    assert fallback == check.fallback_route
    activation.request_inspections(({"route": fallback},))
    assert activation.next_clause_route(check, failed_route=fallback) is None


def test_tooling_failure_retries_same_packet_but_new_packet_gets_fresh_budget():
    router = VerificationRouter(max_tooling_retries=1)
    outcome = VerificationOutcome(TaskJudgement.NOT_JUDGED, VerificationStatus.FAILED, OutcomeOwner.VERIFIER_TOOLING, "tool missing")
    assert router.route(outcome, reconfiguration_available=False, packet_signature="p1") is RouteAction.RETRY_VERIFIER
    assert router.route(outcome, reconfiguration_available=False, packet_signature="p1") is RouteAction.TERMINAL_INFRASTRUCTURE
    assert router.route(outcome, reconfiguration_available=False, packet_signature="p2") is RouteAction.RETRY_VERIFIER


def test_verifier_tooling_failure_never_mutates_solver_findings(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    before = list(world.active_findings)
    outcome = VerificationOutcome(TaskJudgement.NOT_JUDGED, VerificationStatus.FAILED, OutcomeOwner.VERIFIER_TOOLING, "overlay python absent")
    assert runtime.apply_verifier_outcome(outcome, packet_signature="same") is RouteAction.RETRY_VERIFIER
    assert world.active_findings == before


def test_legacy_completed_cannot_bypass_evidence_contract():
    from aether_next import adapt_legacy_verdict

    with pytest.raises(ValueError, match="requires evidence migration"):
        adapt_legacy_verdict("completed", summary="old clean")


def test_executable_verifier_recovery_uses_fallback_after_primary_tool_failure(contract, config_factory):
    from aether_next import execute_clause_inspection_with_recovery
    from aether_next.config import compile_workbench_config

    check = compile_workbench_config(config_factory(), contract).config.verifier_strategy.clause_checks[0]
    activation = VerifierActivation("exec-recovery", max_model_turns=4)
    activation.begin_model_turn()

    def executor(route: str):
        if route == check.inspection_route:
            raise FileNotFoundError("primary overlay route unavailable")
        return {"observed": "fallback succeeded"}

    results = execute_clause_inspection_with_recovery(activation, check, executor)
    assert [item.route for item in results] == [check.inspection_route, check.fallback_route]
    assert [item.success for item in results] == [False, True]
    assert len(activation.inspection_ids) == 2


def test_tooling_failure_can_escalate_to_reconfiguration_when_policy_allows(contract, world, config_factory):
    raw = config_factory()
    raw["reconfigure_policy"]["allowed_owners"] = ["harness_config", "verifier_tooling"]
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    outcome = VerificationOutcome(TaskJudgement.NOT_JUDGED, VerificationStatus.FAILED, OutcomeOwner.VERIFIER_TOOLING, "both routes unavailable")
    assert runtime.apply_verifier_outcome(outcome, packet_signature="tooling") is RouteAction.RETRY_VERIFIER
    assert runtime.apply_verifier_outcome(outcome, packet_signature="tooling") is RouteAction.RECONFIGURE
    runtime.reconfigure(config_factory(mode="batch_job"))
    assert runtime.compiled.process_mode.value == "batch_job"


def test_verifier_cannot_claim_stronger_evidence_than_tool_ceiling(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    activation = VerifierActivation("ceiling", max_model_turns=3)
    activation.begin_model_turn()
    ids = activation.request_inspections((
        {"route": "inspect_metadata", "evidence_class_ceiling": "metadata_proxy"},
        {"route": "independent_compare", "evidence_class_ceiling": "independent_semantic"},
    ))
    outcome = VerificationOutcome(
        TaskJudgement.COMPLETED,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.NONE,
        "overclaimed",
        evidence=(
            EvidenceRecord(ids[0], ("c_file",), "exact_contract", "metadata says present", "remove"),
            EvidenceRecord(ids[1], ("c_value",), "independent_semantic", "alpha", "change"),
        ),
    )
    with pytest.raises(ValueError, match="exceeds inspection ceiling"):
        runtime.apply_verifier_outcome(outcome, activation=activation)
