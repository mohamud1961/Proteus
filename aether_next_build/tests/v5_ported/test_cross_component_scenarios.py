from aether_next import (
    EvidenceRecord,
    HarnessRuntime,
    OutcomeOwner,
    RouteAction,
    TaskJudgement,
    VerificationOutcome,
    VerificationStatus,
    VerifierActivation,
)


def test_primary_verifier_tool_failure_can_recover_via_compiled_fallback(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    check = runtime.compiled.config.verifier_strategy.clause_checks[0]
    activation = VerifierActivation("recover", max_model_turns=4)
    activation.begin_model_turn()
    primary = activation.next_clause_route(check)
    primary_id = activation.request_inspections(({"route": primary},))[0]
    fallback = activation.next_clause_route(check, failed_route=primary)
    fallback_id = activation.request_inspections(({"route": fallback},))[0]
    assert primary_id != fallback_id
    assert fallback == check.fallback_route
    # The lane remains Verifier-owned throughout recovery; Solver state is untouched.
    before = list(world.active_findings)
    tooling = VerificationOutcome(TaskJudgement.NOT_JUDGED, VerificationStatus.FAILED, OutcomeOwner.VERIFIER_TOOLING, "primary route unavailable")
    assert runtime.apply_verifier_outcome(tooling, packet_signature="recover-packet") is RouteAction.RETRY_VERIFIER
    assert world.active_findings == before


def test_completion_after_fallback_requires_real_fallback_inspection(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    activation = VerifierActivation("complete", max_model_turns=4)
    activation.begin_model_turn()
    ids = activation.request_inspections((
        {"route": "inspect_artifact:/app/out.txt"},
        {"route": "run_command:python-byte-compare"},
    ))
    outcome = VerificationOutcome(
        TaskJudgement.COMPLETED,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.NONE,
        "exact state independently established",
        evidence=(
            EvidenceRecord(ids[0], ("c_file",), "exact_contract", "path exists", "remove path"),
            EvidenceRecord(ids[1], ("c_value",), "independent_semantic", "bytes are alpha", "change a byte"),
        ),
    )
    assert runtime.apply_verifier_outcome(outcome, activation=activation) is RouteAction.TERMINATE_SUCCESS


def test_config_failure_reconfiguration_changes_real_behavior_not_only_hash(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    blocker = VerificationOutcome(TaskJudgement.NOT_JUDGED, VerificationStatus.FAILED, OutcomeOwner.HARNESS_CONFIG, "long task needs batch lifecycle")
    assert runtime.apply_verifier_outcome(blocker) is RouteAction.RECONFIGURE
    old_mode = runtime.compiled.process_mode
    old_prefix = runtime.context.prefix.sha256
    runtime.reconfigure(config_factory(mode="batch_job"))
    assert runtime.compiled.process_mode != old_mode
    assert runtime.context.prefix.sha256 != old_prefix
    assert runtime.config_realisation_payload()["process_mode"] == "batch_job"
