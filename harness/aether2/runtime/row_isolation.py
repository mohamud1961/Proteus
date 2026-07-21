"""Generic row-isolation hygiene: detect and clean stale same-run leftovers between board rows.

This module provides a data-driven, non-task-specific mechanism to identify
stale processes/listeners left by a prior row in the same board run.  It:

- Detects TCP listeners on ports that were NOT present at board-start baseline.
- Identifies the owner PID of each stale listener.
- Checks whether the owner is a protected (viable-locked) candidate and skips it.
- Kills only safe stale same-run leftovers (owner started after board baseline).
- Writes an auditable cleanup receipt for every action taken (or skipped).
- Surfaces a clear blocker when a foreign/unknown process holds a needed resource.

NOT task/benchmark-specific: operates on generic TCP-listener state.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class ListenerEntry:
    """A TCP listener detected on the host."""

    port: int
    pid: int | None
    process_name: str
    raw_line: str


@dataclass
class CleanupAction:
    """Record of a cleanup decision for one stale listener."""

    port: int
    pid: int | None
    process_name: str
    action: str  # "killed", "skipped_protected", "skipped_foreign", "skipped_baseline"
    reason: str
    success: bool = True


@dataclass
class CleanupReceipt:
    """Auditable receipt for a row-isolation cleanup pass."""

    run_id: str
    row_label: str
    baseline_ports: list[int]
    detected_listeners: list[dict[str, Any]]
    actions: list[dict[str, Any]] = field(default_factory=list)
    blocker: str | None = None


def snapshot_listeners() -> list[ListenerEntry]:
    """Snapshot current TCP listeners via lsof (macOS) or ss (Linux)."""
    entries: list[ListenerEntry] = []
    try:
        result = subprocess.run(
            ["lsof", "-iTCP", "-sTCP:LISTEN", "-nP", "-F", "pcn"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        entries = _parse_lsof_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if not entries:
        try:
            result = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            entries = _parse_ss_output(result.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return entries


def _parse_lsof_output(text: str) -> list[ListenerEntry]:
    """Parse lsof -F pcn output into ListenerEntry list."""
    entries: list[ListenerEntry] = []
    current_pid: int | None = None
    current_name: str = ""
    for line in (text or "").splitlines():
        if line.startswith("p"):
            current_pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("c"):
            current_name = line[1:]
        elif line.startswith("n"):
            port_match = re.search(r":(\d+)$", line[1:])
            if port_match:
                entries.append(ListenerEntry(
                    port=int(port_match.group(1)),
                    pid=current_pid,
                    process_name=current_name,
                    raw_line=line,
                ))
    return entries


def _parse_ss_output(text: str) -> list[ListenerEntry]:
    """Parse ss -tlnp output into ListenerEntry list."""
    entries: list[ListenerEntry] = []
    for line in (text or "").splitlines():
        if "LISTEN" not in line:
            continue
        port_match = re.search(r":(\d+)\s", line)
        pid_match = re.search(r"pid=(\d+)", line)
        name_match = re.search(r"users:\(\(\"([^\"]+)\"", line)
        if port_match:
            entries.append(ListenerEntry(
                port=int(port_match.group(1)),
                pid=int(pid_match.group(1)) if pid_match else None,
                process_name=name_match.group(1) if name_match else "",
                raw_line=line.strip(),
            ))
    return entries


def clean_stale_listeners(
    *,
    run_id: str,
    row_label: str,
    baseline_ports: Sequence[int],
    protected_pids: Sequence[int] | None = None,
    receipt_dir: Path | None = None,
    dry_run: bool = False,
    _listener_override: list[ListenerEntry] | None = None,
) -> CleanupReceipt:
    """Detect and clean stale listeners not in the baseline snapshot.

    Args:
        run_id: Board run identifier.
        row_label: Human-readable label for the current row.
        baseline_ports: Ports that were listening at board start (never cleaned).
        protected_pids: PIDs of viable-locked candidates (never killed).
        receipt_dir: Where to write the cleanup receipt JSON.
        dry_run: If True, detect but do not kill.
        _listener_override: For testing; supply listeners instead of probing live.

    Returns:
        CleanupReceipt with all actions taken.
    """
    protected = set(protected_pids or [])
    baseline_set = set(baseline_ports)

    current = _listener_override if _listener_override is not None else snapshot_listeners()

    receipt = CleanupReceipt(
        run_id=run_id,
        row_label=row_label,
        baseline_ports=sorted(baseline_set),
        detected_listeners=[
            {"port": e.port, "pid": e.pid, "process_name": e.process_name}
            for e in current
        ],
    )

    stale = [e for e in current if e.port not in baseline_set]

    for entry in stale:
        if entry.pid is None:
            action = CleanupAction(
                port=entry.port,
                pid=None,
                process_name=entry.process_name,
                action="skipped_foreign",
                reason="Cannot identify owner PID; surfacing as blocker",
            )
            receipt.actions.append(_action_dict(action))
            receipt.blocker = (
                f"Port {entry.port} held by unknown process (no PID); "
                f"cannot safely clean. Manual intervention required."
            )
            continue

        if entry.pid in protected:
            action = CleanupAction(
                port=entry.port,
                pid=entry.pid,
                process_name=entry.process_name,
                action="skipped_protected",
                reason=f"PID {entry.pid} is a viable-locked candidate; preserved",
            )
            receipt.actions.append(_action_dict(action))
            continue

        if dry_run:
            action = CleanupAction(
                port=entry.port,
                pid=entry.pid,
                process_name=entry.process_name,
                action="would_kill",
                reason=f"Stale listener on port {entry.port} (PID {entry.pid}); dry-run",
            )
            receipt.actions.append(_action_dict(action))
            continue

        killed = _safe_kill(entry.pid)
        action = CleanupAction(
            port=entry.port,
            pid=entry.pid,
            process_name=entry.process_name,
            action="killed",
            reason=f"Stale listener on port {entry.port} cleaned",
            success=killed,
        )
        receipt.actions.append(_action_dict(action))

    if receipt_dir is not None:
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_dir / f"row_isolation_{row_label}.json"
        receipt_path.write_text(json.dumps(_receipt_dict(receipt), indent=2) + "\n")

    return receipt


def _safe_kill(pid: int) -> bool:
    """Send SIGTERM to a PID. Returns True if the signal was sent."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _action_dict(action: CleanupAction) -> dict[str, Any]:
    return {
        "port": action.port,
        "pid": action.pid,
        "process_name": action.process_name,
        "action": action.action,
        "reason": action.reason,
        "success": action.success,
    }


def _receipt_dict(receipt: CleanupReceipt) -> dict[str, Any]:
    return {
        "event_type": "row_isolation_cleanup",
        "run_id": receipt.run_id,
        "row_label": receipt.row_label,
        "baseline_ports": receipt.baseline_ports,
        "detected_listeners": receipt.detected_listeners,
        "actions": receipt.actions,
        "blocker": receipt.blocker,
    }


__all__ = [
    "CleanupAction",
    "CleanupReceipt",
    "ListenerEntry",
    "clean_stale_listeners",
    "snapshot_listeners",
]
