from __future__ import annotations

import pytest

from aether_next import HarnessRuntime, StableEnvMap, WorldStateDeltaError


def test_stable_envmap_copies_input_and_cannot_be_mutated_through_original(contract, world, config_factory):
    raw_env = {"workspace": "/app", "python": {"version": "3.13"}}
    runtime = HarnessRuntime(contract=contract, envmap=raw_env, raw_config=config_factory(), world=world)
    original_hash = runtime.envmap.sha256
    raw_env["python"]["version"] = "mutated"
    assert runtime.envmap.facts["python"]["version"] == "3.13"
    assert runtime.envmap.sha256 == original_hash


def test_dynamic_progress_does_not_mutate_envmap_or_stable_prefix(contract, world, config_factory):
    runtime = HarnessRuntime(
        contract=contract,
        envmap={"workspace": "/app", "python": {"version": "3.13"}},
        raw_config=config_factory(),
        world=world,
    )
    first_request, first_manifest = runtime.request()
    env_hash = runtime.envmap.sha256
    prefix_hash = runtime.context.prefix.sha256

    runtime.record_action_result(
        action_id="install-grpc",
        action_kind="run_command",
        result={"exit_code": 0},
        state_delta={
            "installed_packages": {"grpcio": "1.73.0"},
            "services": {"server-1": {"state": "listening", "port": 5328}},
            "files": {"/app/server.py": {"status": "modified", "step": 4, "sha256": "abc"}},
        },
        step=4,
    )
    second_request, second_manifest = runtime.request()

    assert runtime.envmap.sha256 == env_hash
    assert runtime.context.prefix.sha256 == prefix_hash
    assert second_request.startswith(first_request)
    assert second_manifest.envmap_version == first_manifest.envmap_version == 1
    text = second_request.decode()
    assert "grpcio 1.73.0 has now been installed." in text
    assert "Process server-1 is listening on 5328." in text
    assert "server.py was modified at step 4." in text
    assert world.installed_packages["grpcio"] == "1.73.0"
    assert world.services["server-1"]["port"] == 5328


def test_dynamic_state_is_preserved_in_compaction_checkpoint(contract, world, config_factory):
    raw = config_factory()
    raw["context_policy"]["max_events_before_compaction"] = 2
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    runtime.record_action_result(
        action_id="install-grpc",
        action_kind="run_command",
        result={"exit_code": 0},
        state_delta={"installed_packages": {"grpcio": "1.73.0"}},
    )
    runtime.record_action_result(
        action_id="touch-server",
        action_kind="write_file",
        result={"bytes": 10},
        state_delta={"files": {"/app/server.py": {"status": "modified", "step": 2}}},
    )
    assert runtime.context.epoch.epoch_id == 2
    snapshot = runtime.context.epoch.checkpoint["dynamic_world_state"]
    assert snapshot["installed_packages"]["grpcio"] == "1.73.0"
    assert snapshot["state_version"] == 2


def test_unknown_dynamic_state_key_fails_closed(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    with pytest.raises(WorldStateDeltaError, match="unsupported dynamic-state keys"):
        runtime.record_action_result(
            action_id="bad",
            action_kind="run_command",
            result={"exit_code": 0},
            state_delta={"silent_mystery_state": {"x": 1}},
        )


def test_envmap_revision_requires_evidence_and_reason(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    with pytest.raises(ValueError, match="reason"):
        runtime.revise_envmap(changes={"binaries": {"john": "/app/john"}}, reason="", evidence_receipt_ids=["receipt:missing"])
    with pytest.raises(KeyError, match="unknown receipt"):
        runtime.revise_envmap(
            changes={"binaries": {"john": "/app/john"}},
            reason="discovered durable binary",
            evidence_receipt_ids=["receipt:missing"],
        )


def test_explicit_envmap_revision_changes_prefix_and_starts_new_epoch(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    runtime.record_action_result(
        action_id="discover-john",
        action_kind="run_command",
        result={"stdout": "/app/john/run/john"},
        state_delta={"runtime_facts": {"john_discovered": True}},
    )
    evidence = runtime.receipts.query(kind="action_result")[0].receipt_id
    old_env_hash = runtime.envmap.sha256
    old_prefix_hash = runtime.context.prefix.sha256

    runtime.revise_envmap(
        changes={"binaries": {"john": "/app/john/run/john"}},
        reason="A durable binary path was directly observed and future strategy depends on it.",
        evidence_receipt_ids=[evidence],
    )

    assert runtime.envmap.version == 2
    assert runtime.envmap.sha256 != old_env_hash
    assert runtime.context.prefix.sha256 != old_prefix_hash
    assert runtime.context.epoch.epoch_id == 1
    assert runtime.context.epoch.events == []
    assert runtime.context.prefix.envmap_version == 2
    receipt = runtime.receipts.query(kind="stable_envmap_revised")[0]
    assert receipt.payload["previous_version"] == 1
    assert receipt.payload["new_version"] == 2
    assert receipt.payload["evidence_receipt_ids"] == [evidence]


def test_stable_envmap_deep_revision_preserves_unrelated_facts():
    envmap = StableEnvMap.create({"workspace": "/app", "binaries": {"python": "/usr/bin/python3"}})
    revised = envmap.revise(
        changes={"binaries": {"john": "/app/john/run/john"}},
        reason="observed",
        evidence_receipt_ids=["receipt:000001"],
    )
    assert revised.facts == {
        "workspace": "/app",
        "binaries": {"python": "/usr/bin/python3", "john": "/app/john/run/john"},
    }
    assert envmap.facts == {"workspace": "/app", "binaries": {"python": "/usr/bin/python3"}}
