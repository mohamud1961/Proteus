"""Structured worker handoff records for the explicit Aether-2 subagent surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
import json


HandoffStatus = Literal["blocked", "complete", "invalid_due_to_environment", "partial"]


@dataclass(frozen=True)
class ValidationRecord:
    command: str
    result: str
    evidence_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "result": self.result,
            "evidence_path": self.evidence_path,
        }


@dataclass(frozen=True)
class ReviewRecord:
    finding: str
    disposition: Literal["accepted", "noted", "rejected"]
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding,
            "disposition": self.disposition,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class WorkerHandoff:
    status: HandoffStatus
    summary: str
    completed_scope: tuple[str, ...]
    requirement_disposition: tuple[str, ...]
    files_changed: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    validation: tuple[ValidationRecord, ...]
    unresolved_risks: tuple[str, ...]
    external_state: tuple[str, ...]
    review: tuple[ReviewRecord, ...] = ()
    blockers: tuple[str, ...] = ()
    recommended_next_action: str | None = None
    ownership_respected: bool = True
    raw_ledger_update_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "completed_scope": list(self.completed_scope),
            "requirement_disposition": list(self.requirement_disposition),
            "files_changed": list(self.files_changed),
            "evidence_paths": list(self.evidence_paths),
            "validation": [item.as_dict() for item in self.validation],
            "unresolved_risks": list(self.unresolved_risks),
            "external_state": list(self.external_state),
            "review": [item.as_dict() for item in self.review],
            "blockers": list(self.blockers),
            "recommended_next_action": self.recommended_next_action,
            "ownership_respected": self.ownership_respected,
            "raw_ledger_update_path": self.raw_ledger_update_path,
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)),
        }

    def parent_visible_items(self) -> tuple[str, ...]:
        return tuple(self.unresolved_risks) + tuple(self.blockers)


__all__ = ["HandoffStatus", "ReviewRecord", "ValidationRecord", "WorkerHandoff"]
