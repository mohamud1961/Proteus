from __future__ import annotations

from pathlib import Path

from proteus.models import TaskSpec, Workbench


class Architect:
    """Turns explicit task truth into a bounded workbench contract."""

    def synthesize(self, task: TaskSpec, workspace_root: Path) -> Workbench:
        if not task.deliverables:
            raise ValueError("task must declare at least one deliverable")
        return Workbench(
            task_id=task.task_id,
            task_fingerprint=task.fingerprint(),
            mode=task.mode,
            workspace_root=str(workspace_root),
            allowed_actions=("write_file", "run_command", "read_file"),
            deliverables=tuple(item.path for item in task.deliverables),
            evidence_requirements=("action_receipts", "file_sha256", "verifier_checks", "command_output"),
        )
