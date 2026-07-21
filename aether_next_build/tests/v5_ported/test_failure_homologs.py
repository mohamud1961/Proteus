from copy import deepcopy

import pytest

from aether_next import (
    EvidenceRecord,
    HarnessRuntime,
    OutcomeOwner,
    TaskClause,
    TaskContract,
    TaskJudgement,
    VerificationOutcome,
    VerificationStatus,
    VerifierActivation,
    WorldState,
)


def _config_for(config_factory, clause_specs, checks, selectors=None):
    raw = config_factory(selectors=selectors)
    raw["clause_coverage"] = [
        {"clause_id": cid, "solver_handling": solver, "verifier_check": verifier}
        for cid, solver, verifier in clause_specs
    ]
    raw["verifier_strategy"]["clause_checks"] = checks
    return raw


def _activation(routes):
    activation = VerifierActivation("homolog", max_model_turns=4)
    activation.begin_model_turn()
    ids = activation.request_inspections(tuple({"route": route} for route in routes))
    return activation, ids


def test_asymmetric_proto_contract_rejects_same_method_self_confirmation(config_factory):
    contract = TaskContract.create(
        "Set request field must be value; response field may be val; live round trip must work.",
        (
            TaskClause("c_request_field", "SetValRequest uses exact field value.", ("value",)),
            TaskClause("c_roundtrip", "Live SetVal then GetVal returns stored integer."),
        ),
    )
    world = WorldState(contract, files={"/app/kv.proto": "message SetValRequest { int32 val = 2; }"})
    raw = _config_for(
        config_factory,
        [
            ("c_request_field", "follow clause exactly", "inspect public descriptor"),
            ("c_roundtrip", "run service", "independent client round trip"),
        ],
        [
            {"clause_id": "c_request_field", "inspection_route": "descriptor", "fallback_route": "read_proto", "falsification_check": "public field differs", "required_evidence_class": "exact_contract"},
            {"clause_id": "c_roundtrip", "inspection_route": "independent_client", "fallback_route": "rpc_probe", "falsification_check": "round trip differs", "required_evidence_class": "independent_semantic"},
        ],
        selectors=[{"kind": "task_contract", "representation": "full", "required": True}],
    )
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    activation, ids = _activation(["generated_stub", "solver_client"])
    outcome = VerificationOutcome(
        TaskJudgement.COMPLETED,
        VerificationStatus.CONCLUSIVE,
        OutcomeOwner.NONE,
        "internally consistent",
        evidence=(
            EvidenceRecord(ids[0], ("c_request_field",), "same_method", "generated stub accepts val", "call value"),
            EvidenceRecord(ids[1], ("c_roundtrip",), "behavioral", "solver client works", "use public field"),
        ),
    )
    with pytest.raises(ValueError, match="evidence too weak"):
        runtime.apply_verifier_outcome(outcome, activation=activation)


def test_video_proxy_evidence_cannot_satisfy_event_semantics(config_factory):
    contract = TaskContract.create("Report real takeoff and landing frames.", (TaskClause("c_frames", "Frames visually correspond to the two events."),))
    world = WorldState(contract)
    raw = _config_for(
        config_factory,
        [("c_frames", "derive event boundaries", "inspect named frames visually")],
        [{"clause_id": "c_frames", "inspection_route": "perceive_frames", "falsification_check": "feet still grounded or not yet landed", "required_evidence_class": "independent_semantic"}],
        selectors=[{"kind": "task_contract", "representation": "full", "required": True}],
    )
    runtime = HarnessRuntime(contract=contract, envmap={}, raw_config=raw, world=world)
    activation, ids = _activation(["bounding_box_metadata"])
    outcome = VerificationOutcome(TaskJudgement.COMPLETED, VerificationStatus.CONCLUSIVE, OutcomeOwner.NONE, "proxy", evidence=(EvidenceRecord(ids[0], ("c_frames",), "metadata_proxy", "box bottom changed", "inspect feet"),))
    with pytest.raises(ValueError, match="evidence too weak"):
        runtime.apply_verifier_outcome(outcome, activation=activation)


def test_narrow_sanitizer_fixture_cannot_prove_broad_contract(config_factory):
    contract = TaskContract.create("Remove all executable JavaScript while preserving clean HTML.", (TaskClause("c_sanitize", "Broad adversarial inputs are blocked and clean bytes preserved."),))
    world = WorldState(contract)
    raw = _config_for(
        config_factory,
        [("c_sanitize", "sanitize", "run adversarial and clean matrix")],
        [{"clause_id": "c_sanitize", "inspection_route": "adversarial_matrix", "falsification_check": "one bypass or clean mutation", "required_evidence_class": "independent_semantic"}],
        selectors=[{"kind": "task_contract", "representation": "full", "required": True}],
    )
    runtime = HarnessRuntime(contract=contract, envmap={}, raw_config=raw, world=world)
    activation, ids = _activation(["single_fixture"])
    outcome = VerificationOutcome(TaskJudgement.COMPLETED, VerificationStatus.CONCLUSIVE, OutcomeOwner.NONE, "one fixture passed", evidence=(EvidenceRecord(ids[0], ("c_sanitize",), "solver_authored_test", "script/onclick removed", "try encoded handlers"),))
    with pytest.raises(ValueError, match="evidence too weak"):
        runtime.apply_verifier_outcome(outcome, activation=activation)


def test_dna_tm_evidence_without_full_transformation_cannot_complete(config_factory):
    contract = TaskContract.create(
        "Primers must satisfy exact Tm and transform the full input plasmid into the full target plasmid.",
        (
            TaskClause("c_tm", "Exact required Tm method is satisfied."),
            TaskClause("c_transform", "Simulated full product equals full target sequence."),
        ),
    )
    world = WorldState(contract)
    raw = _config_for(
        config_factory,
        [
            ("c_tm", "calculate exact Tm", "rerun exact Tm"),
            ("c_transform", "simulate full transform", "compare full product"),
        ],
        [
            {"clause_id": "c_tm", "inspection_route": "oligotm", "falsification_check": "Tm outside bounds", "required_evidence_class": "exact_contract"},
            {"clause_id": "c_transform", "inspection_route": "full_simulation", "falsification_check": "product differs", "required_evidence_class": "independent_semantic"},
        ],
        selectors=[{"kind": "task_contract", "representation": "full", "required": True}],
    )
    runtime = HarnessRuntime(contract=contract, envmap={}, raw_config=raw, world=world)
    activation, ids = _activation(["oligotm"])
    outcome = VerificationOutcome(TaskJudgement.COMPLETED, VerificationStatus.CONCLUSIVE, OutcomeOwner.NONE, "Tm correct", evidence=(EvidenceRecord(ids[0], ("c_tm",), "exact_contract", "Tm exact", "change sequence"),))
    with pytest.raises(ValueError, match="lacks clause evidence:c_transform"):
        runtime.apply_verifier_outcome(outcome, activation=activation)


def test_nested_repository_path_is_materialised_exactly(config_factory):
    contract = TaskContract.create("Recover nested repository content.", (TaskClause("c_nested", "Nested file has expected content.", ("/app/repo/site/about.md",)),))
    world = WorldState(contract, files={"/app/repo/site/about.md": "Stanford"})
    raw = _config_for(
        config_factory,
        [("c_nested", "read nested path", "read nested path")],
        [{"clause_id": "c_nested", "inspection_route": "read_nested", "falsification_check": "path missing", "required_evidence_class": "exact_contract"}],
        selectors=[{"kind": "file", "target": "/app/repo/site/about.md", "representation": "full", "required": True}],
    )
    runtime = HarnessRuntime(contract=contract, envmap={"repo_root": "/app/repo"}, raw_config=raw, world=world)
    assert runtime._last_selections[0].inline_value == "Stanford"


def test_stale_log_head_does_not_hide_live_tail(config_factory):
    contract = TaskContract.create("Judge current service state from latest log evidence.", (TaskClause("c_log", "Latest log shows ready."),))
    log = "OLD FAILURE\n" + "noise\n" * 1000 + "LATEST READY\n"
    world = WorldState(contract, files={"/var/log/service.log": log})
    raw = _config_for(
        config_factory,
        [("c_log", "inspect latest log", "tail anchored read")],
        [{"clause_id": "c_log", "inspection_route": "tail_log", "falsification_check": "latest state not ready", "required_evidence_class": "behavioral"}],
        selectors=[{"kind": "file", "target": "/var/log/service.log", "representation": "head_tail", "max_chars": 200, "required": True}],
    )
    runtime = HarnessRuntime(contract=contract, envmap={}, raw_config=raw, world=world)
    rendered = runtime._last_selections[0].inline_value
    assert "OLD FAILURE" in rendered["excerpt"]
    assert "LATEST READY" in rendered["excerpt"]
    assert rendered["retrieval_handle"]
