"""Task metadata describing one workspace-backed Aether-2 run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    instruction: str
    task_dir: Path
    workspace_root: Path
    artifacts_dir: Path


__all__ = ["TaskSpec"]
