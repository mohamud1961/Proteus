from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any, Literal


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


@dataclass(frozen=True)
class Deliverable:
    path: str
    expected_text: str
    description: str = ""


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    prompt: str
    deliverables: tuple[Deliverable, ...]
    verification_command: tuple[str, ...] = ()
    verification_args: tuple[str, ...] = ()
    mode: str = "direct_build"

    def fingerprint(self) -> str:
        return sha256(stable_json(asdict(self)).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Workbench:
    task_id: str
    task_fingerprint: str
    mode: str
    workspace_root: str
    allowed_actions: tuple[str, ...]
    deliverables: tuple[str, ...]
    evidence_requirements: tuple[str, ...]
    compiler_version: str = "proteus-compiler-v1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


ActionKind = Literal["write_file", "run_command", "read_file"]


@dataclass(frozen=True)
class SolverAction:
    action_id: str
    kind: ActionKind
    path: str = ""
    content: str = ""
    command: tuple[str, ...] = ()
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    kind: str
    success: bool
    detail: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    path: str = ""
    sha256: str = ""
    changed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    passed: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    checks: tuple[CheckResult, ...]
    verifier: str = "proteus.independent_verifier.v1"

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "verifier": self.verifier, "checks": [c.as_dict() for c in self.checks]}


@dataclass(frozen=True)
class RunResult:
    task_id: str
    mode: str
    workbench: Workbench
    actions: tuple[ActionReceipt, ...]
    verification: VerificationResult
    evidence_path: str
    workspace_root: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "mode": self.mode,
            "workbench": self.workbench.as_dict(),
            "actions": [a.as_dict() for a in self.actions],
            "verification": self.verification.as_dict(),
            "evidence_path": self.evidence_path,
            "workspace_root": self.workspace_root,
        }
