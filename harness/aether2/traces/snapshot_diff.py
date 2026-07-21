"""Workspace snapshot capture and diff helpers.

Responsible for:
- Scanning the workspace filesystem into a ``StateSnapshot``
- Loading side-car registries (service, process, job, session)
- Comparing two snapshots into a ``DeltaReport``

Public names in this module are re-exported verbatim by
``harness.aether2.traces.delta`` so all existing import sites
continue to work unchanged.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace as dataclass_replace
from pathlib import Path
from typing import Any

from harness.aether2.traces._text_utils import _clean_text
from harness.aether2.traces.kernel_artifacts import _sha256_file, build_artifact_record

__all__ = [
    "FileDelta",
    "StateSnapshot",
    "DeltaReport",
    "snapshot",
    "diff",
    "with_evidence_ledger",
]

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_IGNORED_DIR_NAMES = {
    ".git",
    ".aether2",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}

_REGISTRY_FILENAMES = {
    "service_registry": ("service_registry.json",),
    "process_registry": ("process_registry.json",),
    "job_registry": ("job_registry.json", "jobs.json"),
    "session_registry": ("session_registry.json", "sessions.json"),
}

_EVIDENCE_LEDGER_VERSION = 1


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileDelta:
    path: str
    hash_before: str | None
    hash_after: str | None
    change_type: str


@dataclass(frozen=True)
class StateSnapshot:
    workspace_root: str
    captured_at: float
    files: dict[str, str]
    artifact_registry: dict[str, dict[str, Any]]
    service_registry: dict[str, dict[str, Any]]
    process_registry: dict[str, dict[str, Any]]
    job_registry: dict[str, dict[str, Any]]
    session_registry: dict[str, dict[str, Any]]
    # Cumulative, run-level facts that are NOT derivable from a single filesystem
    # snapshot: package-manager command successes and nonzero-exit commands seen
    # so far. Populated externally (loop.py / ExecutionContext) via
    # dataclasses.replace(); defaulted to empty here so plain snapshot()/diff()
    # callers are unaffected.
    installed_packages: tuple[str, ...] = ()
    nonzero_exits: tuple[dict[str, Any], ...] = ()
    evidence_ledger: dict[str, Any] = field(
        default_factory=lambda: {
            "version": _EVIDENCE_LEDGER_VERSION,
            "requirements": [],
            "blockers": [],
            "terminal_claims": [],
            "repeated_failure_families": [],
        }
    )


@dataclass(frozen=True)
class DeltaReport:
    workspace_root: str
    captured_at: float
    files_changed: list[FileDelta]
    artifact_registry_changed: bool
    service_registry_changed: bool
    process_registry_changed: bool
    job_registry_changed: bool
    session_registry_changed: bool
    added_paths: tuple[str, ...]
    modified_paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.files_changed and not any(
            (
                self.artifact_registry_changed,
                self.service_registry_changed,
                self.process_registry_changed,
                self.job_registry_changed,
                self.session_registry_changed,
            )
        )


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def snapshot(workspace_root: Path) -> StateSnapshot:
    """Capture a conservative workspace snapshot from visible files and local registries."""

    root = workspace_root.resolve(strict=False)
    files: dict[str, str] = {}
    artifact_registry: dict[str, dict[str, Any]] = {}

    for path in _iter_visible_files(root):
        relative = path.relative_to(root).as_posix()
        files[relative] = _sha256_file(path)
        artifact_registry[relative] = build_artifact_record(
            path=relative,
            workspace_root=root,
            generated=None,
        )

    return StateSnapshot(
        workspace_root=root.as_posix(),
        captured_at=time.time(),
        files=files,
        artifact_registry=artifact_registry,
        service_registry=_load_registry(root, "service_registry"),
        process_registry=_load_registry(root, "process_registry"),
        job_registry=_load_job_registry(root),
        session_registry=_load_session_registry(root),
    )


def diff(prev: StateSnapshot, curr: StateSnapshot) -> DeltaReport:
    """Compare two snapshots and return the file and registry deltas."""

    prev_files = dict(prev.files)
    curr_files = dict(curr.files)
    file_paths = sorted(set(prev_files) | set(curr_files))
    file_deltas: list[FileDelta] = []
    added_paths: list[str] = []
    modified_paths: list[str] = []
    deleted_paths: list[str] = []

    for path in file_paths:
        before = prev_files.get(path)
        after = curr_files.get(path)
        if before == after:
            continue
        if before is None:
            file_deltas.append(FileDelta(path=path, hash_before=None, hash_after=after, change_type="added"))
            added_paths.append(path)
        elif after is None:
            file_deltas.append(FileDelta(path=path, hash_before=before, hash_after=None, change_type="deleted"))
            deleted_paths.append(path)
        else:
            file_deltas.append(FileDelta(path=path, hash_before=before, hash_after=after, change_type="modified"))
            modified_paths.append(path)

    return DeltaReport(
        workspace_root=curr.workspace_root,
        captured_at=curr.captured_at,
        files_changed=file_deltas,
        artifact_registry_changed=prev.artifact_registry != curr.artifact_registry,
        service_registry_changed=prev.service_registry != curr.service_registry,
        process_registry_changed=prev.process_registry != curr.process_registry,
        job_registry_changed=prev.job_registry != curr.job_registry,
        session_registry_changed=prev.session_registry != curr.session_registry,
        added_paths=tuple(added_paths),
        modified_paths=tuple(modified_paths),
        deleted_paths=tuple(deleted_paths),
    )


def with_evidence_ledger(snap: StateSnapshot, ledger: Any) -> StateSnapshot:
    """Return a copy of *snap* with evidence_ledger replaced by *ledger*."""
    from harness.aether2.traces.evidence_ledger import compact_evidence_ledger

    return dataclass_replace(snap, evidence_ledger=compact_evidence_ledger(ledger))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _iter_visible_files(workspace_root: Path):
    for path in sorted(workspace_root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIR_NAMES for part in path.parts):
            continue
        yield path


def _load_registry(workspace_root: Path, registry_name: str) -> dict[str, dict[str, Any]]:
    candidates = [workspace_root / f"{filename}" for filename in _REGISTRY_FILENAMES[registry_name]]
    candidates.extend(
        workspace_root / ".aether2" / "state" / f"{filename}"
        for filename in _REGISTRY_FILENAMES[registry_name]
    )
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            return {
                str(key): dict(value) if isinstance(value, dict) else {}
                for key, value in data.items()
                if isinstance(key, str) and key
            }
    return {}


def _load_job_registry(workspace_root: Path) -> dict[str, dict[str, Any]]:
    jobs_dir = workspace_root / ".aether2" / "state" / "jobs"
    if not jobs_dir.exists():
        return _load_registry(workspace_root, "job_registry")
    jobs: dict[str, dict[str, Any]] = {}
    for meta_path in sorted(jobs_dir.glob("*/meta.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        job_id = str(data.get("job_id") or meta_path.parent.name).strip()
        if not job_id:
            continue
        log_path = Path(str(data.get("log_path") or meta_path.parent / "job.log"))
        exit_code_path = Path(str(data.get("exit_code_path") or meta_path.parent / "exit_code"))
        pid = int(data.get("pid") or 0)
        exit_code = None
        if exit_code_path.exists():
            try:
                exit_code = int(exit_code_path.read_text(encoding="utf-8").strip())
            except ValueError:
                exit_code = None
        jobs[job_id] = {
            "pid": pid,
            "cwd": str(data.get("cwd") or ""),
            "alive": _pid_alive(pid) if pid > 0 and exit_code is None else False,
            "exit_code": exit_code,
            "log_path": str(log_path),
            "log_size": log_path.stat().st_size if log_path.exists() else 0,
            "registry_path": str(meta_path),
        }
    if jobs:
        return jobs
    return _load_registry(workspace_root, "job_registry")


def _load_session_registry(workspace_root: Path) -> dict[str, dict[str, Any]]:
    sessions_dir = workspace_root / ".aether2" / "state" / "sessions"
    if not sessions_dir.exists():
        return _load_registry(workspace_root, "session_registry")
    sessions: dict[str, dict[str, Any]] = {}
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        session_id = str(data.get("session_id") or path.stem).strip()
        if not session_id:
            continue
        sessions[session_id] = {
            "command": str(data.get("command") or ""),
            "registry_path": str(path),
        }
    if sessions:
        return sessions
    return _load_registry(workspace_root, "session_registry")
