from __future__ import annotations

import pytest

from aether_next.runtime import HarnessRuntime
from aether_next.task_contract import TaskClause, TaskContract
from aether_next.world import StableEnvMap, WorldState, WorldStateDeltaError


def _runtime(**kwargs) -> HarnessRuntime:
    contract = TaskContract.create("Create the output artifact.", [TaskClause("out", "output exists")])
    world = WorldState(contract)
    return HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, world=world, **kwargs)


def test_action_result_is_exactly_receipted_and_dynamic_delta_is_atomic() -> None:
    runtime = _runtime()
    receipt_id = runtime.record_action_result(
        action_id="a1",
        action_kind="write_file",
        result={"stdout": "done"},
        state_delta={"files": {"/app/out.txt": {"status": "created"}}},
    )
    receipt = runtime.receipts.get(receipt_id)
    assert receipt.payload["result"] == {"stdout": "done"}
    assert runtime.world.files["/app/out.txt"]["status"] == "created"
    state_before = runtime.world.dynamic_snapshot()
    with pytest.raises(WorldStateDeltaError):
        runtime.record_action_result(
            action_id="bad",
            action_kind="write_file",
            result={"ok": False},
            state_delta={"files": {"/app/bad": "ok"}, "unknown": True},
        )
    assert runtime.world.dynamic_snapshot() == state_before
    assert not runtime.receipts.query(kind="action_result", text='"action_id":"bad"')


def test_large_result_is_handle_backed_in_context_and_stable_prefix_survives_progress() -> None:
    runtime = _runtime(max_inline_chars=32)
    first, first_manifest = runtime.request()
    runtime.record_action_result(
        action_id="large",
        action_kind="run_command",
        result={"stdout": "x" * 5000},
    )
    second, second_manifest = runtime.request()
    assert first_manifest.stable_prefix_sha256 == second_manifest.stable_prefix_sha256
    assert second_manifest.common_prefix_bytes_with_previous > 0
    assert b'"stdout":"' not in second
    assert b"output_handle" in second
    assert runtime.context.retrieve_output(runtime.world.latest_result["result"]["output_handle"]).startswith('{"stdout":')


def test_envmap_revision_requires_receipt_and_changes_prefix_and_epoch() -> None:
    runtime = _runtime()
    receipt = runtime.record_action_result(action_id="inspect", action_kind="read_file", result={"ok": True})
    before_prefix = runtime.context.prefix.sha256
    before_epoch = runtime.context.epoch.epoch_id
    runtime.revise_envmap(changes={"python": "3.13"}, reason="inspection evidence", evidence_receipt_ids=[receipt])
    assert runtime.envmap.version == 2
    assert runtime.context.prefix.sha256 != before_prefix
    assert runtime.context.epoch.epoch_id == 1
    with pytest.raises(ValueError):
        runtime.revise_envmap(changes={"x": 1}, reason="", evidence_receipt_ids=[receipt])
    with pytest.raises(KeyError):
        runtime.revise_envmap(changes={"x": 1}, reason="missing evidence", evidence_receipt_ids=["receipt:nope"])


def test_reconfigure_changes_digest_and_version_without_leaking_raw_config() -> None:
    runtime = _runtime(raw_config={"verifier_prompt": "SECRET_TRAP"})
    request, _ = runtime.request()
    old = runtime.compiled
    new = runtime.reconfigure({"verifier_prompt": "CHANGED"})
    assert new.config_version == old.config_version + 1
    assert new.stable_config_sha256 != old.stable_config_sha256
    assert b"SECRET_TRAP" not in request
    request2, _ = runtime.request()
    assert b"CHANGED" not in request2


def test_raw_stable_envmap_is_content_addressed() -> None:
    runtime = _runtime()
    assert isinstance(runtime.envmap, StableEnvMap)
    assert runtime.envmap.facts == {"workspace": "/app"}


def test_empty_receipt_store_is_shared_with_world_and_context_handles() -> None:
    runtime = _runtime()
    assert runtime.receipts is runtime.world.output_handles.receipts
    assert runtime.receipts is runtime.context.output_handles.receipts

    handle = runtime.world.store_output("exact payload")
    stored = runtime.receipts.query(kind="output_payload")
    assert len(stored) == 1
    assert stored[0].payload["content"] == "exact payload"
    assert runtime.world.retrieve_output(handle) == "exact payload"


def test_large_result_descriptor_keeps_original_handle_and_one_payload_receipt() -> None:
    runtime = _runtime(max_inline_chars=32)
    runtime.record_action_result(
        action_id="large",
        action_kind="run_command",
        result={"stdout": "x" * 5000},
    )
    descriptor = runtime.world.latest_result["result"]
    event_descriptor = runtime.context.epoch.events[-1]["result"]
    assert event_descriptor["output_handle"] == descriptor["output_handle"]
    assert runtime.context.retrieve_output(descriptor["output_handle"]).startswith('{"stdout":')
    payloads = runtime.receipts.query(kind="output_payload")
    assert len(payloads) == 1
