from copy import deepcopy

import pytest

from aether_next import HarnessRuntime, WorldStateDeltaError


def test_malformed_late_delta_rolls_back_all_prior_sections(world):
    before = deepcopy(world.dynamic_snapshot())
    version = world.state_version
    with pytest.raises(WorldStateDeltaError, match="service bad must be a mapping"):
        world.apply_delta(
            {
                "installed_packages": {"grpcio": "1.73.0"},
                "files": {"/app/server.py": {"status": "modified", "step": 4}},
                "services": {"bad": "not-a-mapping"},
            }
        )
    assert world.dynamic_snapshot() == before
    assert world.state_version == version


def test_noop_delta_does_not_advance_state_version(world):
    version = world.state_version
    assert world.apply_delta({"files": {}}) == ()
    assert world.state_version == version


def test_unchanged_truncated_selector_reuses_same_lossless_handle(contract, world, config_factory):
    raw = config_factory(selectors=[
        {"kind": "file", "target": "/app/large.log", "representation": "head_tail", "max_chars": 200, "required": True},
    ])
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    first = runtime.config_realisation_payload()["selector_realisation"][0]["retrieval_handle"]
    runtime.record_action_result(action_id="a1", action_kind="run_command", result={"stdout": "unrelated"})
    second = runtime._last_selections[0].retrieval_handle
    assert first == second
    assert len(world.receipts.query(kind="context_retrieval_payload")) == 1


def test_changed_truncated_selector_gets_new_lossless_handle(contract, world, config_factory):
    raw = config_factory(selectors=[
        {"kind": "file", "target": "/app/large.log", "representation": "head_tail", "max_chars": 200, "required": True},
    ])
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    first = runtime._last_selections[0].retrieval_handle
    world.files["/app/large.log"] += "NEW MATERIAL\n"
    runtime.record_action_result(action_id="a2", action_kind="read_file", result={"ok": True})
    second = runtime._last_selections[0].retrieval_handle
    assert first != second


def test_envmap_revision_must_materially_change_facts(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    evidence = runtime.record_action_result(action_id="inspect", action_kind="read_file", result={"workspace": "/app"})
    with pytest.raises(ValueError, match="materially change"):
        runtime.revise_envmap(changes={"workspace": "/app"}, reason="same", evidence_receipt_ids=[evidence])
