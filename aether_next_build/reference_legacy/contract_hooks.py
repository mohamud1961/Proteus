"""ContractArchitect: model hook for TaskContract extraction.

Thin wrapper that calls a model with the contract-architect system prompt,
parses the result, and returns (contract, errors) honestly — no fallback
contract, so replay experiments see failures as-is.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from aether_next.model_hooks import ModelCallable
from .task_contract import (
    CONTRACT_ARCHITECT_SYSTEM_PROMPT,
    TaskContract,
    parse_task_contract,
)


class ContractArchitect:
    """Extract a TaskContract from a task via a single model call."""

    def __init__(self, model: ModelCallable) -> None:
        self._model = model

    def extract(
        self,
        request: Mapping[str, Any],
        *,
        workspace_root: str = "/app",
    ) -> tuple[TaskContract | None, list[str]]:
        """Call the model and parse the result.

        Returns ``(contract, parse_errors)``.  On any exception the contract
        is ``None`` and the error is recorded — no synthetic fallback.
        """
        messages = [
            {"role": "system", "content": CONTRACT_ARCHITECT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(request, default=str)},
        ]
        try:
            raw = self._model(messages, max_output_tokens=8000)
            contract = parse_task_contract(raw, workspace_root=workspace_root)
            return contract, []
        except Exception as exc:
            return None, [str(exc)]
