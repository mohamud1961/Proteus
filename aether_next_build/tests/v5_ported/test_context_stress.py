from aether_next import HarnessRuntime


def test_many_turns_compact_without_changing_stable_prefix_or_losing_state(contract, world, config_factory):
    raw = config_factory()
    raw["context_policy"]["max_events_before_compaction"] = 4
    raw["context_policy"]["max_dynamic_bytes"] = 2048
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    stable = runtime.context.prefix.sha256
    for step in range(1, 31):
        runtime.record_action_result(
            action_id=f"a{step}",
            action_kind="run_command",
            result={"stdout": "x" * 300, "step": step},
            state_delta={"runtime_facts": {"last_step": step}},
            step=step,
        )
        assert runtime.context.prefix.sha256 == stable
    request, manifest = runtime.request()
    assert manifest.epoch_id > 1
    assert runtime.context.epoch.checkpoint["dynamic_world_state"]["runtime_facts"]["last_step"] == 30
    assert runtime.context.epoch.checkpoint["latest_result"]["action_id"] == "a30"
    assert len(request) < 20000


def test_repeated_context_refresh_does_not_create_receipt_bloat(contract, world, config_factory):
    raw = config_factory(selectors=[
        {"kind": "file", "target": "/app/large.log", "representation": "head_tail", "max_chars": 100, "required": True},
    ])
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    for step in range(20):
        runtime.record_action_result(action_id=f"r{step}", action_kind="run_command", result={"step": step})
    assert len(world.receipts.query(kind="context_retrieval_payload")) == 1


def test_large_action_result_is_not_replayed_inline_but_remains_exactly_retrievable(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    payload = {"stdout": "Z" * 50000, "stderr": "", "exit_code": 0}
    receipt_id = runtime.record_action_result(action_id="large", action_kind="run_command", result=payload)
    request, _ = runtime.request()
    text = request.decode()
    assert "Z" * 5000 not in text
    assert receipt_id in text
    assert world.receipts.get(receipt_id).payload["result"]["stdout"] == "Z" * 50000
    assert world.latest_result["result"]["type"] == "large_action_result"


def test_dynamic_checkpoint_omits_large_process_logs(contract, world, config_factory):
    world.services["noisy"] = {"state": "running", "pid": 1, "raw_log": "X" * 100000}
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    snapshot = runtime.context.epoch.checkpoint["dynamic_world_state"]["services"]["noisy"]
    assert "raw_log" not in snapshot
    assert snapshot["omitted_keys"] == ["raw_log"]
    request, _ = runtime.request()
    assert "X" * 1000 not in request.decode()
