import pytest

from aether_next import (
    EvidenceRecord,
    Finding,
    HarnessRuntime,
    OutcomeOwner,
    RouteAction,
    TaskJudgement,
    VerificationOutcome,
    VerificationRouter,
    VerificationStatus,
    VerifierActivation,
    validate_outcome,
)


def complete_outcome():
    return VerificationOutcome(
        TaskJudgement.COMPLETED,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.NONE,
        "all clauses satisfied",
        evidence=(
            EvidenceRecord("i1", ("c_file",), "exact_contract", "file exists", "remove file"),
            EvidenceRecord("i2", ("c_value",), "independent_semantic", "bytes equal alpha", "change byte"),
        ),
    )


def test_completed_routes_to_immediate_success(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    assert runtime.apply_verifier_outcome(complete_outcome()) is RouteAction.TERMINATE_SUCCESS


def test_real_state_defect_routes_to_solver(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    outcome = VerificationOutcome(
        TaskJudgement.NEEDS_REPAIR,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.SOLVER_STATE,
        "wrong content",
        findings=(Finding("f1", "c_value", "beta", "alpha", "write alpha"),),
    )
    assert runtime.apply_verifier_outcome(outcome) is RouteAction.RETURN_TO_SOLVER


def test_verifier_tooling_failure_never_routes_to_solver(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    outcome = VerificationOutcome(
        TaskJudgement.NOT_JUDGED,
        VerificationStatus.FAILED,
        OutcomeOwner.VERIFIER_TOOLING,
        "python absent in verifier overlay",
    )
    first = runtime.apply_verifier_outcome(outcome)
    second = runtime.apply_verifier_outcome(outcome)
    assert first is RouteAction.RETRY_VERIFIER
    assert second is RouteAction.TERMINAL_INFRASTRUCTURE
    assert RouteAction.RETURN_TO_SOLVER not in {first, second}


def test_harness_config_failure_routes_to_reconfiguration_once(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    outcome = VerificationOutcome(
        TaskJudgement.NOT_JUDGED,
        VerificationStatus.FAILED,
        OutcomeOwner.HARNESS_CONFIG,
        "required inspection route absent",
    )
    assert runtime.apply_verifier_outcome(outcome) is RouteAction.RECONFIGURE


def test_provider_failure_has_bounded_retry():
    router = VerificationRouter(max_provider_retries=1)
    outcome = VerificationOutcome(TaskJudgement.NOT_JUDGED, VerificationStatus.FAILED, OutcomeOwner.PROVIDER, "429")
    assert router.route(outcome, reconfiguration_available=False) is RouteAction.RETRY_VERIFIER
    assert router.route(outcome, reconfiguration_available=False) is RouteAction.TERMINAL_INFRASTRUCTURE


def test_completed_requires_evidence_for_every_clause(contract):
    partial = VerificationOutcome(
        TaskJudgement.COMPLETED,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.NONE,
        "partial",
        evidence=(EvidenceRecord("i1", ("c_file",), "exact_contract", "exists", "remove"),),
    )
    with pytest.raises(ValueError, match="lacks clause evidence:c_value"):
        validate_outcome(partial, required_clause_ids=contract.clause_ids)


def test_needs_repair_cannot_be_owned_by_tooling():
    with pytest.raises(ValueError, match="only for conclusive Solver-state defects"):
        validate_outcome(
            VerificationOutcome(TaskJudgement.NEEDS_REPAIR, VerificationStatus.FAILED, OutcomeOwner.VERIFIER_TOOLING, "tool failed")
        )


def test_final_verifier_turn_cannot_request_more_inspections():
    activation = VerifierActivation("v1", max_model_turns=2)
    activation.begin_model_turn()
    assert activation.request_inspections(({"kind": "read_file"},)) == ("v1:inspection:1",)
    activation.begin_model_turn()
    with pytest.raises(RuntimeError, match="final Verifier turn"):
        activation.request_inspections(({"kind": "read_file"},))


def test_activation_preserves_inspection_ids_until_final_outcome():
    activation = VerifierActivation("v2", max_model_turns=3)
    activation.begin_model_turn()
    ids = activation.request_inspections(({"kind": "read_file"}, {"kind": "probe_port"}))
    activation.begin_model_turn()
    outcome = activation.finish(VerificationOutcome(TaskJudgement.NOT_JUDGED, VerificationStatus.INCONCLUSIVE, OutcomeOwner.PROTOCOL, "needs format correction"))
    assert ids == ("v2:inspection:1", "v2:inspection:2")
    assert activation.inspection_ids == list(ids)
    assert activation.closed is True
    assert outcome.owner is OutcomeOwner.PROTOCOL
