from pathlib import Path
import sys
import time
import urllib.request

import pytest

from aether_next import ProcessRegistry, ProcessState, free_tcp_port, wait_for_port


def test_real_batch_job_runs_and_retains_logs(tmp_path: Path):
    registry = ProcessRegistry(tmp_path / "logs")
    try:
        record = registry.start(
            process_id="job1",
            semantic_key="train:model-a",
            command=(sys.executable, "-c", "import time; print('start', flush=True); time.sleep(0.05); print('done', flush=True)"),
        )
        assert record.state is ProcessState.RUNNING
        final = registry.wait("job1", timeout_s=2)
        stdout, stderr = registry.logs("job1")
        assert final.state is ProcessState.SUCCEEDED
        assert "start" in stdout and "done" in stdout
        assert stderr == ""
    finally:
        registry.cleanup()


def test_equivalent_batch_job_overlap_is_blocked(tmp_path: Path):
    registry = ProcessRegistry(tmp_path / "logs")
    try:
        registry.start(process_id="job1", semantic_key="train:model-a", command=(sys.executable, "-c", "import time; time.sleep(3)"))
        with pytest.raises(ValueError, match="equivalent process already active"):
            registry.start(process_id="job2", semantic_key="train:model-a", command=(sys.executable, "-c", "print('x')"))
    finally:
        registry.cleanup()


def test_explicit_replacement_cancels_old_batch_job(tmp_path: Path):
    registry = ProcessRegistry(tmp_path / "logs")
    try:
        registry.start(process_id="job1", semantic_key="train:model-a", command=(sys.executable, "-c", "import time; time.sleep(3)"))
        registry.start(process_id="job2", semantic_key="train:model-a", command=(sys.executable, "-c", "print('new')"), replace_active=True)
        assert registry.records["job1"].state is ProcessState.CANCELLED
        assert registry.wait("job2", 2).state is ProcessState.SUCCEEDED
    finally:
        registry.cleanup()


def test_real_service_waits_for_port_and_serves_http(tmp_path: Path):
    registry = ProcessRegistry(tmp_path / "logs")
    port = free_tcp_port()
    (tmp_path / "index.html").write_text("healthy")
    try:
        registry.start(
            process_id="web1",
            semantic_key="service:web",
            command=(sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"),
            cwd=tmp_path,
        )
        assert wait_for_port("127.0.0.1", port, timeout_s=3)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=2) as response:
            assert response.read().decode() == "healthy"
    finally:
        registry.cleanup()
    assert registry.records["web1"].state is ProcessState.CANCELLED


def test_wait_timeout_does_not_misclassify_running_job(tmp_path: Path):
    registry = ProcessRegistry(tmp_path / "logs")
    try:
        registry.start(process_id="job1", semantic_key="long", command=(sys.executable, "-c", "import time; time.sleep(1)"))
        current = registry.wait("job1", timeout_s=0.01)
        assert current.state is ProcessState.RUNNING
    finally:
        registry.cleanup()
