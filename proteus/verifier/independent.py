from __future__ import annotations

from pathlib import Path
import subprocess

from proteus.models import CheckResult, TaskSpec, VerificationResult


class IndependentVerifier:
    """Checks durable workspace state, not solver claims or receipt success flags."""

    def verify(self, task: TaskSpec, root: Path) -> VerificationResult:
        root = root.resolve()
        checks: list[CheckResult] = []
        for deliverable in task.deliverables:
            path = (root / deliverable.path).resolve()
            safe = path == root or root in path.parents
            exists = safe and path.is_file()
            actual = path.read_text(encoding="utf-8") if exists else ""
            matches = exists and actual == deliverable.expected_text
            checks.append(CheckResult(f"deliverable:{deliverable.path}", matches, "content matches contract" if matches else "missing or content differs", {"path": deliverable.path, "exists": exists}))
        if task.verification_command:
            try:
                completed = subprocess.run(task.verification_command + task.verification_args, cwd=root, capture_output=True, text=True, timeout=15, check=False)
                checks.append(CheckResult("independent_command", completed.returncode == 0, f"exit={completed.returncode}", {"stdout": completed.stdout, "stderr": completed.stderr}))
            except (OSError, subprocess.SubprocessError) as exc:
                checks.append(CheckResult("independent_command", False, str(exc), {"error": type(exc).__name__}))
        return VerificationResult(all(check.passed for check in checks), tuple(checks))
