from __future__ import annotations

import os
from pathlib import Path

import pytest

from aether_next.workspace_state import (
    capture_workspace_state,
    create_immutable_workspace_snapshot,
    diff_workspace_states,
)


def test_workspace_delta_records_create_change_remove_and_metadata(tmp_path: Path) -> None:
    (tmp_path / "removed.txt").write_text("remove me", encoding="utf-8")
    changed = tmp_path / "changed.txt"
    changed.write_text("before", encoding="utf-8")
    metadata = tmp_path / "mode.txt"
    metadata.write_text("same", encoding="utf-8")
    metadata.chmod(0o644)
    before = capture_workspace_state(tmp_path)

    (tmp_path / "removed.txt").unlink()
    changed.write_text("after", encoding="utf-8")
    metadata.chmod(0o600)
    (tmp_path / "created.txt").write_text("new", encoding="utf-8")
    after = capture_workspace_state(tmp_path)

    delta = diff_workspace_states(before, after)
    assert delta["created_paths"] == ["created.txt"]
    assert delta["removed_paths"] == ["removed.txt"]
    assert delta["content_changed_paths"] == ["changed.txt"]
    assert delta["metadata_changed_paths"] == ["mode.txt"]
    assert delta["mutation_actor_status"] == "mutation_actor_unknown"


def test_initial_snapshot_preserves_file_after_live_workspace_mutation(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    critical = live / "critical.bin"
    critical.write_bytes(b"original")
    snapshot_dir = tmp_path / "immutable"

    snapshot = create_immutable_workspace_snapshot(live, snapshot_dir)
    critical.unlink()

    assert not critical.exists()
    preserved = snapshot_dir / "critical.bin"
    assert preserved.read_bytes() == b"original"
    assert preserved.stat().st_mode & 0o222 == 0
    assert snapshot.by_path()["critical.bin"].sha256


def test_snapshot_records_hash_mode_owner_and_symlink(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("hello", encoding="utf-8")
    link_path = tmp_path / "link.txt"
    try:
        link_path.symlink_to("file.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    snapshot = capture_workspace_state(tmp_path)
    entries = snapshot.by_path()
    assert entries["file.txt"].kind == "file"
    assert len(entries["file.txt"].sha256) == 64
    assert entries["file.txt"].mode == os.stat(file_path).st_mode & 0o7777
    assert entries["link.txt"].kind == "symlink"
    assert entries["link.txt"].symlink_target == "file.txt"
