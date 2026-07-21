from pathlib import Path
import sys

import pytest

from aether_next import HarnessRuntime, ProcessRegistry, ProcessState, free_tcp_port


def test_service_config_controls_real_service_lifecycle(contract, world, config_factory, tmp_path: Path):
    registry = ProcessRegistry(tmp_path / "logs")
    port = free_tcp_port()
    (tmp_path / "index.html").write_text("ok")
    runtime = HarnessRuntime(
        contract=contract,
        envmap={"workspace": "/app"},
        raw_config=config_factory(mode="service", selectors=[
            {"kind": "task_contract", "representation": "full", "required": True},
            {"kind": "service_state", "target": "web", "representation": "full", "required": True},
        ]),
        world=world,
        process_registry=registry,
    )
    try:
        runtime.start_managed_process(
            process_id="web",
            semantic_key="service:web",
            command=(sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"),
            cwd=str(tmp_path),
        )
        assert runtime.wait_for_managed_process("web", timeout_s=3, port=port) is True
        assert runtime.receipts.query(kind="service_started")
        assert runtime.receipts.query(kind="service_readiness")[0].payload["ready"] is True
    finally:
        registry.cleanup()


def test_batch_config_controls_real_batch_lifecycle(contract, world, config_factory, tmp_path: Path):
    registry = ProcessRegistry(tmp_path / "logs")
    runtime = HarnessRuntime(
        contract=contract,
        envmap={"workspace": "/app"},
        raw_config=config_factory(mode="batch_job", selectors=[
            {"kind": "task_contract", "representation": "full", "required": True},
            {"kind": "job_state", "target": "trainer", "representation": "full", "required": True},
        ]),
        world=world,
        process_registry=registry,
    )
    try:
        runtime.start_managed_process(
            process_id="train",
            semantic_key="train:model",
            command=(sys.executable, "-c", "print('model-ready')"),
        )
        final = runtime.wait_for_managed_process("train", timeout_s=2)
        assert final.state is ProcessState.SUCCEEDED
        terminal = runtime.receipts.query(kind="batch_job_terminal_state")[0]
        assert "model-ready" in terminal.payload["stdout"]
    finally:
        registry.cleanup()


def test_interactive_config_refuses_durable_background_work(contract, world, config_factory, tmp_path: Path):
    registry = ProcessRegistry(tmp_path / "logs")
    runtime = HarnessRuntime(
        contract=contract,
        envmap={"workspace": "/app"},
        raw_config=config_factory(),
        world=world,
        process_registry=registry,
    )
    with pytest.raises(RuntimeError, match="interactive workbench"):
        runtime.start_managed_process(
            process_id="bad",
            semantic_key="bad",
            command=(sys.executable, "-c", "print('x')"),
        )
