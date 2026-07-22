import os
from pathlib import Path
import signal
import sys
import time

import pytest

from aether_next import HarnessRuntime, ProcessRegistry, free_tcp_port


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@pytest.mark.skipif(os.name != "posix", reason="process-group semantics are POSIX-specific")
def test_cancelling_parent_kills_spawned_child_process_group(tmp_path: Path):
    registry = ProcessRegistry(tmp_path / "logs")
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess,time,pathlib,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
        "time.sleep(30)"
    )
    registry.start(process_id="tree", semantic_key="tree", command=(sys.executable, "-c", code))
    deadline = time.time() + 3
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.02)
    child_pid = int(pid_file.read_text())
    assert _pid_exists(child_pid)
    registry.cancel("tree")
    deadline = time.time() + 3
    while _pid_exists(child_pid) and time.time() < deadline:
        time.sleep(0.02)
    assert not _pid_exists(child_pid)


def test_service_readiness_routes_are_all_executed(contract, world, config_factory, tmp_path: Path):
    raw = config_factory(mode="service")
    raw["process_policy"]["readiness"] = ["wait_for_process_state", "wait_for_port", "probe_http"]
    registry = ProcessRegistry(tmp_path / "logs")
    port = free_tcp_port()
    (tmp_path / "index.html").write_text("healthy")
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world, process_registry=registry)
    try:
        runtime.start_managed_process(
            process_id="web2",
            semantic_key="web2",
            command=(sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"),
            cwd=str(tmp_path),
        )
        assert runtime.wait_for_managed_process("web2", timeout_s=3, port=port) is True
        receipt = world.receipts.query(kind="service_readiness")[-1]
        assert receipt.payload["routes"] == {
            "wait_for_process_state": True,
            "wait_for_port": True,
            "probe_http": True,
        }
    finally:
        registry.cleanup()


def test_failed_service_readiness_is_honestly_not_ready(contract, world, config_factory, tmp_path: Path):
    raw = config_factory(mode="service")
    raw["process_policy"]["readiness"] = ["wait_for_port"]
    registry = ProcessRegistry(tmp_path / "logs")
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world, process_registry=registry)
    try:
        runtime.start_managed_process(process_id="sleep", semantic_key="sleep", command=(sys.executable, "-c", "import time; time.sleep(.3)"))
        assert runtime.wait_for_managed_process("sleep", timeout_s=0.05, port=free_tcp_port()) is False
        assert world.services["sleep"]["state"] == "not_ready"
    finally:
        registry.cleanup()


def test_batch_log_pattern_readiness_can_fail_without_false_success(contract, world, config_factory, tmp_path: Path):
    raw = config_factory(mode="batch_job")
    raw["process_policy"]["readiness"] = ["wait_for_process_state", "wait_for_log_pattern"]
    registry = ProcessRegistry(tmp_path / "logs")
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=raw, world=world, process_registry=registry)
    try:
        runtime.start_managed_process(process_id="jobx", semantic_key="jobx", command=(sys.executable, "-c", "print('different')"))
        final = runtime.wait_for_managed_process("jobx", timeout_s=2, log_pattern="MODEL_READY")
        assert final.state.value == "succeeded"
        receipt = world.receipts.query(kind="batch_job_terminal_state")[-1]
        assert receipt.payload["routes"]["wait_for_process_state"] is True
        assert receipt.payload["routes"]["wait_for_log_pattern"] is False
        assert world.jobs["jobx"]["readiness"]["wait_for_log_pattern"] is False
    finally:
        registry.cleanup()
