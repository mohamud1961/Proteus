import json

from aether_next import HarnessRuntime
from aether_next.context_epochs import common_prefix_bytes


def test_stable_prefix_order_is_fixed(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app", "python": "3.13"}, raw_config=config_factory(), world=world)
    text = runtime.context.prefix.text
    markers = [
        "[00_kernel_constitution]", "[01_fixed_tool_schema]", "[02_task_contract]",
        "[03_envmap]", "[04_architect_solver_prompt]", "[05_compiled_workbench]",
        "[06_response_protocol]",
    ]
    offsets = [text.index(marker) for marker in markers]
    assert offsets == sorted(offsets)


def test_append_only_turn_keeps_previous_request_as_exact_prefix(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    first, _ = runtime.request()
    runtime.record_action_result(action_id="a1", action_kind="read_file", result={"content": "alpha"})
    second, manifest = runtime.request()
    assert second.startswith(first)
    assert common_prefix_bytes(first, second) == len(first)
    assert manifest.common_prefix_bytes_with_previous == len(first)


def test_multiple_recent_outputs_remain_append_only_not_sliding_window(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    previous, _ = runtime.request()
    for index in range(1, 4):
        runtime.record_action_result(action_id=f"a{index}", action_kind="run_command", result={"stdout": f"output-{index}"})
        current, manifest = runtime.request()
        assert current.startswith(previous)
        assert manifest.common_prefix_bytes_with_previous == len(previous)
        previous = current
    decoded = previous.decode()
    assert "output-1" in decoded and "output-2" in decoded and "output-3" in decoded


def test_compaction_resets_dynamic_epoch_but_keeps_stable_prefix(contract, world, config_factory):
    raw = config_factory()
    raw["context_policy"]["max_events_before_compaction"] = 2
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    prefix_hash = runtime.context.prefix.sha256
    runtime.request()
    runtime.record_action_result(action_id="a1", action_kind="read_file", result={"content": "alpha"})
    runtime.request()
    runtime.record_action_result(action_id="a2", action_kind="write_file", result={"bytes": 5})
    assert runtime.context.epoch.epoch_id == 2
    assert runtime.context.prefix.sha256 == prefix_hash
    assert runtime.context.epoch.events == []
    assert runtime.receipts.query(kind="context_compaction")


def test_config_change_invalidates_config_dependent_prefix(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    old_hash = runtime.context.prefix.sha256
    new = config_factory(mode="batch_job", selectors=[
        {"kind": "task_contract", "representation": "full", "required": True},
        {"kind": "job_state", "target": "trainer", "representation": "structured_summary", "required": True},
    ])
    runtime.reconfigure(new, force=True)
    assert runtime.compiled.config_version == 2
    assert runtime.context.prefix.sha256 != old_hash
    assert runtime.context.epoch.config_version == 2


def test_provider_cached_tokens_are_recorded_not_inferred(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    _, manifest = runtime.request({"input_tokens": 2000, "input_tokens_details": {"cached_tokens": 1500, "cache_write_tokens": 250}})
    assert manifest.provider_input_tokens == 2000
    assert manifest.provider_cached_tokens == 1500
    assert manifest.provider_cache_share == 0.75
    assert manifest.provider_cache_write_tokens == 250
    assert manifest.provider_cache_write_share == 0.125


def test_no_provider_usage_means_cache_share_unknown(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    _, manifest = runtime.request()
    assert manifest.provider_cache_share is None


def test_stable_prefix_is_identical_across_turns(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    original = runtime.context.prefix.text
    for index in range(5):
        runtime.record_action_result(action_id=f"a{index}", action_kind="read_file", result={"i": index})
        runtime.request()
        assert runtime.context.prefix.text == original


def test_compaction_preserves_latest_result_even_when_not_architect_selected(contract, world, config_factory):
    raw = config_factory(selectors=[
        {"kind": "task_contract", "representation": "full", "required": True},
        {"kind": "file", "target": "/app/out.txt", "representation": "full", "required": True},
    ])
    raw["context_policy"]["max_events_before_compaction"] = 2
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    runtime.record_action_result(action_id="a1", action_kind="read_file", result={"content": "alpha"})
    runtime.record_action_result(action_id="a2", action_kind="write_file", result={"bytes": 5})
    assert runtime.context.epoch.epoch_id == 2
    assert runtime.context.epoch.checkpoint["latest_result"]["action_id"] == "a2"
