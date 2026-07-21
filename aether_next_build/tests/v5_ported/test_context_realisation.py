import pytest

from aether_next import HarnessRuntime


def _runtime(contract, world, config_factory, selectors):
    return HarnessRuntime(
        contract=contract,
        envmap={"workspace": "/app"},
        raw_config=config_factory(selectors=selectors),
        world=world,
    )


def test_full_file_selector_materialises_exact_content(contract, world, config_factory):
    runtime = _runtime(contract, world, config_factory, [
        {"kind": "file", "target": "/app/out.txt", "representation": "full", "required": True}
    ])
    selected = runtime._last_selections
    assert selected[0].inline_value == "alpha"
    assert selected[0].retrieval_handle is None
    assert selected[0].truncated is False


def test_head_tail_selector_has_lossless_handle(contract, world, config_factory):
    runtime = _runtime(contract, world, config_factory, [
        {"kind": "file", "target": "/app/large.log", "representation": "head_tail", "max_chars": 120, "required": True}
    ])
    selected = runtime._last_selections[0]
    assert selected.truncated is True
    assert selected.retrieval_handle is not None
    stored = world.receipts.get(selected.retrieval_handle).payload["value"]
    assert stored == world.files["/app/large.log"]


def test_targeted_excerpt_centres_matching_evidence(contract, world, config_factory):
    runtime = _runtime(contract, world, config_factory, [
        {"kind": "file", "target": "/app/large.log", "representation": "targeted_excerpt", "pattern": "TARGET failure", "max_chars": 160, "required": True}
    ])
    selected = runtime._last_selections[0]
    assert "TARGET failure" in str(selected.inline_value)
    assert selected.retrieval_handle is not None


def test_handle_only_selector_never_destroys_artifact(contract, world, config_factory):
    runtime = _runtime(contract, world, config_factory, [
        {"kind": "artifact", "target": "/app/frame.png", "representation": "handle_only", "required": True}
    ])
    selected = runtime._last_selections[0]
    assert selected.inline_value["available"] is True
    assert world.receipts.get(selected.retrieval_handle).payload["value"]["width"] == 640


def test_missing_required_selector_fails_before_solver_context(contract, world, config_factory):
    raw = config_factory(selectors=[
        {"kind": "file", "target": "/app/missing.txt", "representation": "full", "required": True}
    ])
    with pytest.raises(ValueError, match="before Solver start"):
        HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)


def test_missing_optional_selector_is_explicit_none(contract, world, config_factory):
    runtime = _runtime(contract, world, config_factory, [
        {"kind": "file", "target": "/app/missing.txt", "representation": "full", "required": False}
    ])
    selected = runtime._last_selections[0]
    assert selected.inline_value is None
    assert selected.raw_chars == 0


def test_every_supported_selector_kind_materialises(contract, world, config_factory):
    pinned = world.receipts.append("note", {"message": "remember me"})
    selectors = [
        {"kind": "task_contract", "representation": "structured_summary", "required": True},
        {"kind": "env_fact", "target": "python", "representation": "full", "required": True},
        {"kind": "file", "target": "/app/out.txt", "representation": "full", "required": True},
        {"kind": "receipt", "target": pinned.receipt_id, "representation": "full", "required": True},
        {"kind": "artifact", "target": "/app/frame.png", "representation": "structured_summary", "required": True},
        {"kind": "service_state", "target": "web", "representation": "full", "required": True},
        {"kind": "job_state", "target": "trainer", "representation": "full", "required": True},
        {"kind": "active_findings", "representation": "full", "required": True},
        {"kind": "latest_result", "representation": "full", "required": True},
        {"kind": "named_section", "target": "plan", "representation": "full", "required": True},
    ]
    runtime = _runtime(contract, world, config_factory, selectors)
    selected = runtime._last_selections
    assert [item.kind for item in selected] == [item["kind"] for item in selectors]
    assert all(item.inline_value is not None for item in selected)
