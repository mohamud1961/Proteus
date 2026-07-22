from __future__ import annotations

from aether_next import (
    ContextEpoch,
    ContextManager,
    StableEnvMap,
    TaskClause,
    TaskContract,
    build_stable_prefix,
)


def _context(*, max_events: int = 3) -> ContextManager:
    contract = TaskContract.create("write out.txt", [TaskClause("write", "out.txt exists")])
    envmap = StableEnvMap.create({"workspace": "/app", "python": "3.13"})
    prefix = build_stable_prefix(
        kernel_constitution="fixed trusted kernel",
        fixed_tool_schema={"actions": ["read_file", "write_file"]},
        task_contract=contract,
        envmap=envmap,
        architect_solver_prompt="solve from state",
        compiled_workbench={"mode": "direct_build"},
        response_protocol={"format": "json"},
    )
    return ContextManager(prefix, ContextEpoch(config_version=1, epoch_id=1), max_events=max_events, max_dynamic_bytes=10_000)


def test_stable_prefix_order_and_identity_are_unchanged_by_events() -> None:
    context = _context()
    markers = [
        "[00_kernel_constitution]", "[01_fixed_tool_schema]", "[02_task_contract]",
        "[03_envmap]", "[04_architect_solver_prompt]", "[05_compiled_workbench]",
        "[06_response_protocol]",
    ]
    offsets = [context.prefix.text.index(marker) for marker in markers]
    assert offsets == sorted(offsets)
    stable_text = context.prefix.text
    first, _ = context.current_request()
    context.append_event({"kind": "action_result", "result": {"status": "ok"}})
    second, manifest = context.current_request()
    assert second.startswith(first)
    assert manifest.common_prefix_bytes_with_previous == len(first)
    assert context.prefix.text == stable_text


def test_compaction_preserves_result_findings_handles_and_current_state() -> None:
    context = _context(max_events=2)
    checkpoint = {
        "latest_result": {"action_id": "a2"},
        "active_findings": [{"finding_id": "f1"}],
        "retrieval_handles": ["output:receipt:000001"],
        "current_state": {"files": {"out.txt": {"status": "present"}}},
    }
    context.append_event({"kind": "first"})
    context.append_event({"kind": "second"})
    assert context.compact_if_needed(checkpoint) is True
    assert context.epoch.epoch_id == 2
    assert context.epoch.events == []
    assert context.epoch.checkpoint == checkpoint


def test_large_event_output_is_handle_backed_and_exactly_retrievable() -> None:
    context = _context()
    payload = "Z" * 10_000
    context.append_event({"kind": "run_command", "stdout": payload})
    rendered = context.epoch.render()
    assert "Z" * 1000 not in rendered
    handle = context.epoch.events[0]["stdout"]["output_handle"]
    assert context.retrieve_output(handle) == payload
    # Content-deduplicated handles remain stable across unchanged outputs.
    context.append_event({"kind": "run_command", "stdout": payload})
    assert context.epoch.events[1]["stdout"]["output_handle"] == handle


def test_binary_event_output_is_handle_backed_and_exactly_retrievable() -> None:
    context = _context()
    payload = b"\x00\xffbinary"
    context.append_event({"kind": "run_command", "stdout": payload})
    rendered = context.current_request()[0]
    assert payload not in rendered
    handle = context.epoch.events[0]["stdout"]["output_handle"]
    assert context.retrieve_output(handle) == payload


def test_cache_manifest_records_provider_values_and_unknown_is_not_inferred() -> None:
    context = _context()
    _, observed = context.current_request({"input_tokens": 2000, "input_tokens_details": {"cached_tokens": 1500, "cache_write_tokens": 250}})
    assert observed.provider_input_tokens == 2000
    assert observed.provider_cached_tokens == 1500
    assert observed.provider_cache_share == 0.75
    assert observed.provider_cache_write_tokens == 250
    assert observed.provider_cache_write_share == 0.125
    _, unknown = context.current_request()
    assert unknown.provider_cache_share is None


def test_impossible_provider_write_count_does_not_claim_cache_share() -> None:
    context = _context()
    _, observed = context.current_request(
        {"input_tokens": 10, "input_tokens_details": {"cache_write_tokens": 11}}
    )
    assert observed.provider_cache_write_tokens == 11
    assert observed.provider_cache_write_share is None
