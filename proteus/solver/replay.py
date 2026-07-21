from __future__ import annotations

from collections.abc import Iterable

from proteus.models import SolverAction


class ReplaySolver:
    """Deterministic solver provider used for the judge demo and offline eval_suite.evals."""

    provider_name = "replay"

    def __init__(self, actions: Iterable[SolverAction]) -> None:
        self._actions = tuple(actions)

    def actions(self) -> tuple[SolverAction, ...]:
        return self._actions
