from concurrent.futures import ThreadPoolExecutor

import pytest

from aether_next import (
    ReceiptStore,
    StableEnvMap,
    TaskClause,
    TaskContract,
    WorldState,
    WorldStateDeltaError,
)


def _world() -> WorldState:
    contract = TaskContract.create("do the work", [TaskClause("c1", "produce the result", ("result",))])
    return WorldState(task_contract=contract, stable_envmap=StableEnvMap.create({"workspace": "/app"}))


def test_task_truth_is_typed_immutable_and_clause_ids_stable():
    contract = TaskContract.create("prompt", [TaskClause("a", "A", ("x",)), TaskClause("b", "B")])
    assert contract.clause_ids == frozenset({"a", "b"})
    with pytest.raises((AttributeError, TypeError)):
        contract.clauses[0].text = "changed"
    with pytest.raises(ValueError, match="unique"):
        TaskContract.create("prompt", [TaskClause("a", "A"), TaskClause("a", "B")])


def test_stable_envmap_copies_input_and_revision_is_immutable():
    original = {"workspace": "/app", "python": {"version": "3.13"}}
    envmap = StableEnvMap.create(original)
    original["python"]["version"] = "changed"
    assert envmap.facts["python"]["version"] == "3.13"
    revised = envmap.revise(
        changes={"binaries": {"tool": "/app/tool"}}, reason="observed", evidence_receipt_ids=["r1"]
    )
    assert revised.version == 2
    assert envmap.version == 1
    assert revised.facts["python"]["version"] == "3.13"


def test_malformed_late_delta_rolls_back_all_prior_sections():
    world = _world()
    before = world.dynamic_snapshot()
    version = world.state_version
    with pytest.raises(WorldStateDeltaError, match="service bad must be a mapping"):
        world.apply_delta({"installed_packages": {"grpcio": "1"}, "files": {"/app/a": {"status": "modified"}}, "services": {"bad": "no"}})
    assert world.dynamic_snapshot() == before
    assert world.state_version == version


def test_noop_delta_does_not_advance_state_version():
    world = _world()
    assert world.apply_delta({"files": {}}) == ()
    assert world.state_version == 0


def test_same_shape_dynamic_replacements_are_not_silent_noops():
    world = _world()
    world.apply_delta({"files": {"/app/out.txt": "alpha"}})
    first_version = world.state_version
    assert world.apply_delta({"files": {"/app/out.txt": "omega"}})
    assert world.state_version == first_version + 1


def test_dynamic_snapshot_preserves_named_sections_and_removal_tombstones():
    world = _world()
    world.apply_delta(
        {
            "services": {"web": {"state": "listening", "port": 8080}},
            "jobs": {"trainer": {"state": "running"}},
            "named_sections": {"plan": {"next": "submit"}},
        }
    )
    world.apply_delta({"removed_services": ["web"], "removed_jobs": ["trainer"]})
    snapshot = world.dynamic_snapshot()
    assert snapshot["named_sections"] == {"plan": {"next": "submit"}}
    assert snapshot["removed_services"] == ["web"]
    assert snapshot["removed_jobs"] == ["trainer"]
    assert "web" not in snapshot["services"]
    assert "trainer" not in snapshot["jobs"]

    world.apply_delta({"artifacts": {"/app/result": {"opaque": {"value": 1}}}})
    first_version = world.state_version
    assert world.apply_delta({"artifacts": {"/app/result": {"opaque": {"value": 2}}}})
    assert world.state_version == first_version + 1


def test_output_handles_are_exact_deduplicated_and_not_inline():
    world = _world()
    content = "header\n" + ("x" * 10000) + "\ntail"
    first = world.store_output(content)
    second = world.store_output(content)
    changed = world.store_output(content + "!")
    assert first == second
    assert first != changed
    assert world.retrieve_output(first) == content
    descriptor = world.output_handles.describe(first)
    assert descriptor["handle"] == first
    assert "content" not in descriptor


def test_output_handles_preserve_bytes_type_and_exact_payload():
    world = _world()
    content = b"prefix\x00\xff\n"
    handle = world.store_output(content)
    assert world.retrieve_output(handle) == content
    assert isinstance(world.retrieve_output(handle), bytes)
    # A same-text str is a distinct typed payload, not an accidental alias.
    assert world.store_output(content.decode("utf-8", errors="surrogateescape")) != handle


def test_stable_envmap_direct_construction_rejects_forged_digest():
    envmap = StableEnvMap.create({"workspace": "/app"})
    with pytest.raises(ValueError, match="digest"):
        StableEnvMap(envmap.version, envmap._facts_json, "0" * 64)


def test_receipts_survive_reopen_and_dedup():
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "receipts.sqlite"
        store = ReceiptStore(path)
        first = store.append_deduplicated("action_result", {"stdout": "alpha", "exit_code": 0})
        second = store.append_deduplicated("action_result", {"stdout": "alpha", "exit_code": 0})
        assert first.receipt_id == second.receipt_id
        store.close()
        reopened = ReceiptStore(path)
        assert reopened.get(first.receipt_id).payload["stdout"] == "alpha"
        assert reopened.get(first.receipt_id).sha256 == first.sha256
        reopened.close()


def test_receipt_appends_are_unique_under_concurrency():
    store = ReceiptStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda i: store.append("event", {"i": i}), range(200)))
    assert len({row.receipt_id for row in rows}) == 200
    assert len(store) == 200


def test_independent_sqlite_stores_reconcile_ordinals_under_concurrency(tmp_path):
    path = tmp_path / "shared-receipts.sqlite"
    first = ReceiptStore(path)
    second = ReceiptStore(path)
    stores = (first, second)

    def append(index):
        return stores[index % 2].append("event", {"i": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(append, range(100)))

    ids = {row.receipt_id for row in rows}
    assert len(ids) == 100
    assert len(first) == len(second) == 100
    assert {row.payload["i"] for row in second.query(kind="event")} == set(range(100))
    first.close()
    second.close()
