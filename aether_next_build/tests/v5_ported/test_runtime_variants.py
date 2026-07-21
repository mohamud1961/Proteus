from copy import deepcopy

import pytest

from aether_next import (
    FIXED_KERNEL_TOOLS,
    HarnessRuntime,
    OutcomeOwner,
    RouteAction,
    TaskJudgement,
    VerificationOutcome,
    VerificationStatus,
)


def test_three_materially_different_configs_realise_different_workbenches(contract, world, config_factory):
    interactive = HarnessRuntime(
        contract=contract,
        envmap={"workspace": "/app"},
        raw_config=config_factory(),
        world=world,
    )
    service = HarnessRuntime(
        contract=contract,
        envmap={"workspace": "/app"},
        raw_config=config_factory(mode="service", selectors=[
            {"kind": "task_contract", "representation": "full", "required": True},
            {"kind": "service_state", "target": "web", "representation": "structured_summary", "required": True},
        ]),
        world=world,
    )
    batch = HarnessRuntime(
        contract=contract,
        envmap={"workspace": "/app"},
        raw_config=config_factory(mode="batch_job", selectors=[
            {"kind": "task_contract", "representation": "full", "required": True},
            {"kind": "job_state", "target": "trainer", "representation": "structured_summary", "required": True},
        ]),
        world=world,
    )
    assert {interactive.compiled.process_mode.value, service.compiled.process_mode.value, batch.compiled.process_mode.value} == {
        "interactive", "service", "batch_job"
    }
    assert len({interactive.compiled.stable_config_sha256, service.compiled.stable_config_sha256, batch.compiled.stable_config_sha256}) == 3
    assert interactive.fixed_tools == service.fixed_tools == batch.fixed_tools == FIXED_KERNEL_TOOLS
    assert "service_state" in service.context.epoch.render()
    assert "job_state" in batch.context.epoch.render()


def test_config_realisation_receipt_is_written_once_per_version(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    receipts = runtime.receipts.query(kind="config_realisation")
    assert len(receipts) == 1
    assert receipts[0].payload["config_version"] == 1
    assert all(item["status"] in {"compiled", "kernel_owned"} for item in receipts[0].payload["dispositions"])


def test_action_result_is_visible_on_immediately_next_turn(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    runtime.request()
    receipt_id = runtime.record_action_result(
        action_id="a-write",
        action_kind="write_file",
        result={"path": "/app/out.txt", "bytes": 5},
        state_delta={"files": {"/app/out.txt": {"status": "modified", "sha256": "abc", "step": 4}}},
    )
    request, _ = runtime.request()
    text = request.decode()
    assert receipt_id in text
    assert '"action_kind":"write_file"' in text
    assert '"sha256":"abc"' in text


def test_harness_config_blocker_reconfigures_to_batch_mode(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    blocker = VerificationOutcome(TaskJudgement.NOT_JUDGED, VerificationStatus.FAILED, OutcomeOwner.HARNESS_CONFIG, "interactive mode unsuitable for long training")
    assert runtime.apply_verifier_outcome(blocker) is RouteAction.RECONFIGURE
    new_config = config_factory(mode="batch_job", selectors=[
        {"kind": "task_contract", "representation": "full", "required": True},
        {"kind": "job_state", "target": "trainer", "representation": "structured_summary", "required": True},
    ])
    runtime.reconfigure(new_config)
    assert runtime.compiled.config_version == 2
    assert runtime.compiled.process_mode.value == "batch_job"
    receipt = runtime.receipts.query(kind="workbench_reconfigured")[0]
    assert receipt.payload["previous_stable_prefix_sha256"] != receipt.payload["new_stable_prefix_sha256"]


def test_second_reconfiguration_is_refused_by_default_budget(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    new_config = config_factory(mode="service", selectors=[
        {"kind": "task_contract", "representation": "full", "required": True},
        {"kind": "service_state", "target": "web", "representation": "full", "required": True},
    ])
    runtime.reconfigure(new_config, force=True)
    try:
        runtime.reconfigure(config_factory(), force=True)
    except Exception as exc:
        assert "budget exhausted" in str(exc)
    else:
        raise AssertionError("expected reconfiguration budget failure")


def test_same_raw_config_and_world_produce_same_initial_request(contract, world, config_factory):
    raw = config_factory()
    one = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=deepcopy(raw), world=world)
    two = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=deepcopy(raw), world=world)
    one_request, _ = one.request()
    two_request, _ = two.request()
    assert one_request == two_request


def test_reconfiguration_emits_one_full_realisation_receipt_per_version(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    runtime.reconfigure(config_factory(mode="service", selectors=[
        {"kind": "task_contract", "representation": "full", "required": True},
        {"kind": "service_state", "target": "web", "representation": "full", "required": True},
    ]), force=True)
    receipts = runtime.receipts.query(kind="config_realisation")
    assert [receipt.payload["config_version"] for receipt in receipts] == [1, 2]
    assert all("selector_realisation" in receipt.payload for receipt in receipts)


def test_reconfiguration_requires_verified_harness_owned_blocker(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    with pytest.raises(Exception, match="requires a verified"):
        runtime.reconfigure(config_factory(mode="batch_job"))
