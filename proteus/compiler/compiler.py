from __future__ import annotations

from pathlib import Path

from proteus.architect import Architect
from proteus.models import TaskSpec, Workbench


class Compiler:
    """Compiles task truth into the runtime's controlled execution contract."""

    def __init__(self, architect: Architect | None = None) -> None:
        self.architect = architect or Architect()

    def compile(self, task: TaskSpec, workspace_root: Path) -> Workbench:
        root = workspace_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return self.architect.synthesize(task, root)
