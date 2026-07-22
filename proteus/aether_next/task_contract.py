"""Immutable task truth for the canonical Aether-Next runtime.

The Architect may propose strategy, but these clauses are the immutable task
authority consumed by downstream state/verifier code.  The module deliberately
contains no benchmark- or task-specific knowledge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class TaskClause:
    clause_id: str
    text: str
    exact_atoms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.clause_id, str) or not self.clause_id.strip():
            raise ValueError("clause_id must be non-empty")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("clause text must be non-empty")
        atoms = tuple(self.exact_atoms)
        if any(not isinstance(atom, str) or not atom.strip() for atom in atoms):
            raise ValueError("exact task atoms must be non-empty strings")
        if len(atoms) != len(set(atoms)):
            raise ValueError("exact task atoms must be unique within a clause")
        object.__setattr__(self, "exact_atoms", atoms)


@dataclass(frozen=True)
class TaskContract:
    raw_task_prompt: str
    clauses: tuple[TaskClause, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.raw_task_prompt, str) or not self.raw_task_prompt.strip():
            raise ValueError("raw_task_prompt must be non-empty")
        clauses = tuple(self.clauses)
        if not clauses:
            raise ValueError("at least one task clause is required")
        if any(not isinstance(clause, TaskClause) for clause in clauses):
            raise TypeError("clauses must contain TaskClause values")
        ids = [clause.clause_id for clause in clauses]
        if len(ids) != len(set(ids)):
            raise ValueError("task clause IDs must be unique")
        object.__setattr__(self, "clauses", clauses)

    @property
    def clause_ids(self) -> frozenset[str]:
        return frozenset(clause.clause_id for clause in self.clauses)

    @classmethod
    def create(cls, raw_task_prompt: str, clauses: Iterable[TaskClause]) -> "TaskContract":
        return cls(raw_task_prompt=raw_task_prompt, clauses=tuple(clauses))

    def as_payload(self) -> dict[str, object]:
        return {
            "raw_task_prompt": self.raw_task_prompt,
            "clauses": [
                {"clause_id": c.clause_id, "text": c.text, "exact_atoms": list(c.exact_atoms)}
                for c in self.clauses
            ],
        }
