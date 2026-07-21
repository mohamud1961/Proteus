"""Prompt text for the vNext Runtime Workbench Architect."""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .model_hooks import ModelCallable
from .workbench_config import HarnessConfigIR, parse_workbench_architect_output


from .workbench_prompt import WORKBENCH_ARCHITECT_SYSTEM_PROMPT



class WorkbenchArchitect:
    """Ask a model for HarnessConfigIR without synthesizing fallback config."""

    def __init__(self, model: ModelCallable, *, max_output_tokens: int = 24000) -> None:
        self._model = model
        self._max_output_tokens = max(1000, int(max_output_tokens))
        self.last_raw_output = ""
        self.last_errors: list[str] = []
        self.last_warning_codes: list[str] = []
        self.last_warnings: list[str] = []
        self.last_rejected_config_items: list[dict[str, Any]] = []
        self.last_repaired_output: str | None = None

    @staticmethod
    def _is_output_budget_error(exc: Exception) -> bool:
        text = str(exc)
        return bool(re.search(r"max_output_tokens", text, re.IGNORECASE))

    def _call_model(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int,
        allow_budget_retry: bool = True,
    ) -> str:
        try:
            return self._model(messages, max_output_tokens=max_output_tokens)
        except Exception as exc:
            if allow_budget_retry and self._is_output_budget_error(exc):
                expanded = min(max(max_output_tokens + 12000, int(max_output_tokens * 1.5)), 48000)
                return self._model(messages, max_output_tokens=expanded)
            raise

    def configure(self, request: Mapping[str, Any]) -> tuple[HarnessConfigIR | None, list[str]]:
        messages = [
            {"role": "system", "content": WORKBENCH_ARCHITECT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(request, default=str)},
        ]
        try:
            raw = self._call_model(messages, max_output_tokens=self._max_output_tokens)
            self.last_raw_output = raw
            repaired = parse_workbench_architect_output(raw)
            self.last_errors = list(repaired.errors)
            self.last_warning_codes = list(repaired.warning_codes)
            self.last_warnings = list(repaired.warnings)
            self.last_rejected_config_items = [dict(item) for item in repaired.rejected_config_items]
            self.last_repaired_output = repaired.repaired_json
            if repaired.config is not None:
                return repaired.config, list(repaired.errors)
            repair_messages = [
                {"role": "system", "content": WORKBENCH_ARCHITECT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps({
                        "request": request,
                        "previous_output": raw[:12000],
                        "parse_errors": list(repaired.errors),
                        "repair_instruction": (
                            "Repair the previous output. Return one complete, balanced, strict JSON object only. "
                            "Keep array items concise but preserve solver/verifier/config quality. "
                            "Do not include markdown, comments, trailing commas, or extra prose."
                        ),
                        "strict_json_rules": [
                            "Return exactly one JSON object.",
                            "Use double-quoted keys and strings.",
                            "Do not emit markdown, code fences, or comments.",
                            "Do not emit trailing commas or ellipses.",
                            "Do not invent keys outside the required schema.",
                        ],
                    }, default=str),
                },
            ]
            raw_retry = self._call_model(repair_messages, max_output_tokens=self._max_output_tokens)
            self.last_raw_output = raw + "\n\n---RETRY---\n\n" + raw_retry
            retry = parse_workbench_architect_output(raw_retry)
            self.last_errors = list(retry.errors)
            self.last_warning_codes = list(retry.warning_codes)
            self.last_warnings = list(retry.warnings)
            self.last_rejected_config_items = [dict(item) for item in retry.rejected_config_items]
            self.last_repaired_output = retry.repaired_json
            return retry.config, list(retry.errors)
        except Exception as exc:
            self.last_errors = [str(exc)]
            self.last_warning_codes = []
            self.last_warnings = []
            self.last_rejected_config_items = []
            self.last_repaired_output = None
            return None, [str(exc)]
