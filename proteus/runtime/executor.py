from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

from proteus.models import ActionReceipt, SolverAction


class WorkspaceExecutor:
    """Fail-closed executor that keeps every path inside the compiled workspace."""

    def __init__(self, root: Path, evidence) -> None:
        self.root = root.resolve()
        self.evidence = evidence

    def _safe_path(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"path escapes workspace: {relative}")
        return candidate

    def execute(self, action: SolverAction) -> ActionReceipt:
        try:
            if action.kind == "write_file":
                path = self._safe_path(action.path)
                before = path.read_bytes() if path.exists() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(action.content, encoding="utf-8")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                receipt = ActionReceipt(action.action_id, action.kind, True, f"wrote {action.path}", path=action.path, sha256=digest, changed=before != path.read_bytes())
            elif action.kind == "read_file":
                path = self._safe_path(action.path)
                content = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(content.encode()).hexdigest()
                receipt = ActionReceipt(action.action_id, action.kind, True, f"read {action.path}", stdout=content, path=action.path, sha256=digest)
            elif action.kind == "run_command":
                completed = subprocess.run(action.command, cwd=self.root, capture_output=True, text=True, timeout=15, check=False)
                receipt = ActionReceipt(action.action_id, action.kind, completed.returncode == 0, f"exit={completed.returncode}", exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
            else:
                raise ValueError(f"unsupported action kind: {action.kind}")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            receipt = ActionReceipt(action.action_id, action.kind, False, str(exc), path=action.path)
        self.evidence.record("action_receipt", receipt.as_dict())
        return receipt
