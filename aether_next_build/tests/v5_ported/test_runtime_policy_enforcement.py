import sys
import time
from pathlib import Path

import pytest

from aether_next import HarnessRuntime, ProcessRegistry


def test_resource_step_budget_is_enforced(contract, world, config_factory):
    raw = config_factory()
    raw["resource_policy"]["max_steps"] = 2
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    assert runtime.begin_solver_step() == 1
    assert runtime.begin_solver_step() == 2
    with pytest.raises(RuntimeError, match="step budget"):
        runtime.begin_solver_step()


def test_effective_timeouts_are_bounded_by_remaining_total(contract, world, config_factory):
    raw = config_factory()
    raw["resource_policy"].update({"total_timeout_s": 0.08, "command_timeout_s": 0.07, "verifier_timeout_s": 0.07})
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    time.sleep(0.03)
    assert 0 < runtime.effective_command_timeout_s() <= runtime.remaining_time_s + 0.01
    assert 0 < runtime.effective_verifier_timeout_s() <= runtime.remaining_time_s + 0.01
    time.sleep(0.07)
    with pytest.raises(TimeoutError):
        runtime.begin_solver_step()


def test_repeat_policy_requires_memory_query_before_same_state_repeat(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    runtime.guard_action(action_kind="run_command", target="/app", content_hash="one")
    with pytest.raises(RuntimeError, match="memory query required"):
        runtime.guard_action(action_kind="run_command", target="/app", content_hash="one")
    runtime.note_memory_query()
    runtime.guard_action(action_kind="run_command", target="/app", content_hash="one")


def test_changed_content_is_not_treated_as_same_action(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    first = runtime.guard_action(action_kind="write_file", target="/app/out.txt", content_hash="alpha")
    second = runtime.guard_action(action_kind="write_file", target="/app/out.txt", content_hash="beta")
    assert first != second


def test_require_state_change_repeat_mode_blocks_even_after_memory_query(contract, world, config_factory):
    raw = config_factory()
    raw["memory_policy"]["repeat_mode"] = "require_state_change"
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world)
    runtime.guard_action(action_kind="run_command", target="/app", content_hash="same")
    runtime.note_memory_query()
    with pytest.raises(RuntimeError, match="intervening state change"):
        runtime.guard_action(action_kind="run_command", target="/app", content_hash="same")
    runtime.record_action_result(action_id="mutate", action_kind="write_file", result={"ok": True}, state_delta={"files": {"/app/new": "x"}})
    runtime.guard_action(action_kind="run_command", target="/app", content_hash="same")


def test_overwrite_requires_memory_query(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    with pytest.raises(RuntimeError, match="before overwriting"):
        runtime.guard_action(action_kind="write_file", target="/app/out.txt", content_hash="new", overwrites_existing=True)
    runtime.note_memory_query()
    runtime.guard_action(action_kind="write_file", target="/app/out.txt", content_hash="new", overwrites_existing=True)


def test_configured_overlap_policy_changes_process_behavior(contract, world, config_factory, tmp_path: Path):
    raw = config_factory(mode="batch_job")
    raw["process_policy"]["allow_equivalent_overlap"] = True
    registry = ProcessRegistry(tmp_path / "logs")
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world, process_registry=registry)
    try:
        runtime.start_managed_process(process_id="one", semantic_key="train:x", command=(sys.executable, "-c", "import time; time.sleep(.4)"))
        runtime.start_managed_process(process_id="two", semantic_key="train:x", command=(sys.executable, "-c", "import time; time.sleep(.4)"))
        assert registry.is_running("one") and registry.is_running("two")
    finally:
        registry.cleanup()


def test_heartbeat_interval_is_materially_enforced(contract, world, config_factory, tmp_path: Path):
    raw = config_factory(mode="batch_job")
    raw["process_policy"]["heartbeat_interval_s"] = 10.0
    registry = ProcessRegistry(tmp_path / "logs")
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world, process_registry=registry)
    try:
        runtime.start_managed_process(process_id="job", semantic_key="job:x", command=(sys.executable, "-c", "import time; time.sleep(.3)"))
        assert runtime.heartbeat_managed_process("job", now=100.0) is True
        assert runtime.heartbeat_managed_process("job", now=105.0) is False
        assert runtime.heartbeat_managed_process("job", now=111.0) is True
    finally:
        registry.cleanup()


def test_heartbeat_time_cannot_move_backwards(contract, world, config_factory, tmp_path: Path):
    raw = config_factory(mode="batch_job")
    registry = ProcessRegistry(tmp_path / "logs")
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world, process_registry=registry)
    try:
        runtime.start_managed_process(process_id="clock", semantic_key="clock", command=(sys.executable, "-c", "import time; time.sleep(.3)"))
        runtime.heartbeat_managed_process("clock", now=20.0)
        with pytest.raises(ValueError, match="cannot move backwards"):
            runtime.heartbeat_managed_process("clock", now=19.0)
    finally:
        registry.cleanup()
