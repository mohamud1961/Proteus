"""Tests for harness.aether2.runtime.row_isolation — generic stale-port hygiene guard."""

from __future__ import annotations

import json
from pathlib import Path

from harness.aether2.runtime.row_isolation import (
    CleanupReceipt,
    ListenerEntry,
    clean_stale_listeners,
)


def _listener(port: int, pid: int | None = None, name: str = "test") -> ListenerEntry:
    return ListenerEntry(port=port, pid=pid, process_name=name, raw_line=f":{port}")


def test_stale_listener_is_cleaned(tmp_path: Path) -> None:
    """A stale same-run listener NOT in baseline and NOT protected IS killed."""
    killed_pids: list[int] = []
    orig_kill = __import__("harness.aether2.runtime.row_isolation", fromlist=["_safe_kill"])._safe_kill

    import harness.aether2.runtime.row_isolation as mod

    def fake_kill(pid: int) -> bool:
        killed_pids.append(pid)
        return True

    real_kill = mod._safe_kill
    mod._safe_kill = fake_kill
    try:
        receipt = clean_stale_listeners(
            run_id="test-run",
            row_label="row-2",
            baseline_ports=[80, 443],
            protected_pids=[],
            receipt_dir=tmp_path,
            _listener_override=[
                _listener(80, pid=1),    # baseline — should be skipped
                _listener(6665, pid=99),  # stale — should be killed
            ],
        )
    finally:
        mod._safe_kill = real_kill

    assert 99 in killed_pids, "Stale PID 99 should have been killed"
    assert any(a["action"] == "killed" and a["port"] == 6665 for a in receipt.actions)
    # Baseline port 80 should NOT appear in actions
    assert not any(a["port"] == 80 for a in receipt.actions)


def test_protected_candidate_is_not_killed(tmp_path: Path) -> None:
    """A viable-locked candidate PID must NEVER be killed."""
    import harness.aether2.runtime.row_isolation as mod

    killed_pids: list[int] = []

    def fake_kill(pid: int) -> bool:
        killed_pids.append(pid)
        return True

    real_kill = mod._safe_kill
    mod._safe_kill = fake_kill
    try:
        receipt = clean_stale_listeners(
            run_id="test-run",
            row_label="row-3",
            baseline_ports=[80],
            protected_pids=[42],
            receipt_dir=tmp_path,
            _listener_override=[
                _listener(9999, pid=42, name="protected-svc"),
            ],
        )
    finally:
        mod._safe_kill = real_kill

    assert 42 not in killed_pids, "Protected PID 42 must NOT be killed"
    assert any(a["action"] == "skipped_protected" and a["pid"] == 42 for a in receipt.actions)


def test_cleanup_receipt_is_written(tmp_path: Path) -> None:
    """A cleanup receipt JSON file is written to receipt_dir."""
    import harness.aether2.runtime.row_isolation as mod

    mod._safe_kill = lambda pid: True  # noqa: ARG005
    try:
        receipt = clean_stale_listeners(
            run_id="test-run",
            row_label="row-1",
            baseline_ports=[],
            receipt_dir=tmp_path,
            _listener_override=[_listener(5555, pid=77)],
        )
    finally:
        pass

    receipt_path = tmp_path / "row_isolation_row-1.json"
    assert receipt_path.exists(), "Receipt file must be written"
    data = json.loads(receipt_path.read_text())
    assert data["event_type"] == "row_isolation_cleanup"
    assert data["run_id"] == "test-run"
    assert data["row_label"] == "row-1"
    assert len(data["actions"]) >= 1


def test_baseline_path_unaffected(tmp_path: Path) -> None:
    """When all listeners are in baseline, no actions are taken — baseline unaffected."""
    receipt = clean_stale_listeners(
        run_id="test-run",
        row_label="row-baseline",
        baseline_ports=[80, 443, 8080],
        receipt_dir=tmp_path,
        _listener_override=[
            _listener(80, pid=1),
            _listener(443, pid=2),
            _listener(8080, pid=3),
        ],
    )

    assert len(receipt.actions) == 0, "No actions for baseline-only listeners"
    assert receipt.blocker is None


def test_foreign_unknown_process_surfaces_blocker(tmp_path: Path) -> None:
    """A listener with no identifiable PID surfaces a blocker, not a kill."""
    receipt = clean_stale_listeners(
        run_id="test-run",
        row_label="row-foreign",
        baseline_ports=[80],
        receipt_dir=tmp_path,
        _listener_override=[
            _listener(6665, pid=None, name="unknown"),
        ],
    )

    assert receipt.blocker is not None
    assert "6665" in receipt.blocker
    assert any(a["action"] == "skipped_foreign" for a in receipt.actions)
