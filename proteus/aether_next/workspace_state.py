"""Deterministic workspace identity, immutable snapshots, and action deltas."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Iterable

_DEFAULT_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"})


@dataclass(frozen=True)
class PathState:
    path: str
    kind: str
    size: int
    sha256: str
    mode: int
    uid: int
    gid: int
    symlink_target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "symlink_target": self.symlink_target,
        }


@dataclass(frozen=True)
class WorkspaceSnapshot:
    root: str
    entries: tuple[PathState, ...]
    truncated: bool = False

    def by_path(self) -> dict[str, PathState]:
        return {entry.path: entry for entry in self.entries}

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        for entry in self.entries:
            digest.update(repr(entry).encode("utf-8"))
            digest.update(b"\n")
        digest.update(f"truncated={self.truncated}".encode("ascii"))
        return digest.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "digest": self.digest,
            "entry_count": len(self.entries),
            "truncated": self.truncated,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_workspace_state(
    root: str | Path,
    *,
    max_entries: int = 5000,
    skip_dirs: Iterable[str] = _DEFAULT_SKIP_DIRS,
) -> WorkspaceSnapshot:
    root_path = Path(root).resolve()
    skip = set(skip_dirs)
    entries: list[PathState] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in skip)
        for name in sorted(filenames):
            if len(entries) >= max_entries:
                truncated = True
                break
            full = Path(dirpath) / name
            try:
                info = full.lstat()
                rel = full.relative_to(root_path).as_posix()
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(full)
                    entries.append(PathState(
                        path=rel,
                        kind="symlink",
                        size=info.st_size,
                        sha256=hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest(),
                        mode=stat.S_IMODE(info.st_mode),
                        uid=info.st_uid,
                        gid=info.st_gid,
                        symlink_target=target,
                    ))
                elif stat.S_ISREG(info.st_mode):
                    entries.append(PathState(
                        path=rel,
                        kind="file",
                        size=info.st_size,
                        sha256=_hash_file(full),
                        mode=stat.S_IMODE(info.st_mode),
                        uid=info.st_uid,
                        gid=info.st_gid,
                    ))
                else:
                    entries.append(PathState(
                        path=rel,
                        kind="other",
                        size=info.st_size,
                        sha256="",
                        mode=stat.S_IMODE(info.st_mode),
                        uid=info.st_uid,
                        gid=info.st_gid,
                    ))
            except OSError:
                continue
        if truncated:
            break
    entries.sort(key=lambda entry: entry.path)
    return WorkspaceSnapshot(str(root_path), tuple(entries), truncated=truncated)


def diff_workspace_states(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> dict[str, Any]:
    before_map = before.by_path()
    after_map = after.by_path()
    created = sorted(set(after_map) - set(before_map))
    removed = sorted(set(before_map) - set(after_map))
    content_changed: list[str] = []
    metadata_changed: list[str] = []
    for path in sorted(set(before_map) & set(after_map)):
        old = before_map[path]
        new = after_map[path]
        if (old.kind, old.size, old.sha256, old.symlink_target) != (
            new.kind, new.size, new.sha256, new.symlink_target
        ):
            content_changed.append(path)
        elif (old.mode, old.uid, old.gid) != (new.mode, new.uid, new.gid):
            metadata_changed.append(path)
    return {
        "before_digest": before.digest,
        "after_digest": after.digest,
        "created_paths": created,
        "removed_paths": removed,
        "content_changed_paths": content_changed,
        "metadata_changed_paths": metadata_changed,
        "before_truncated": before.truncated,
        "after_truncated": after.truncated,
        "mutation_actor_status": "mutation_actor_unknown",
        "mutation_actor_detail": (
            "change is proven within this action interval; no subprocess actor is asserted"
        ),
    }


def create_immutable_workspace_snapshot(
    source: str | Path,
    destination: str | Path,
) -> WorkspaceSnapshot:
    """Copy pristine state outside the mutable workspace and make the copy read-only."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if destination_path.exists():
        raise FileExistsError(str(destination_path))
    shutil.copytree(source_path, destination_path, symlinks=True)
    for dirpath, dirnames, filenames in os.walk(destination_path, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            try:
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
            except OSError:
                pass
        for name in dirnames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            try:
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
            except OSError:
                pass
    try:
        destination_path.chmod(stat.S_IMODE(destination_path.stat().st_mode) & ~0o222)
    except OSError:
        pass
    return capture_workspace_state(destination_path)
