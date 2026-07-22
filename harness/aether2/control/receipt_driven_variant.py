"""Receipt-driven variant coordinator.

This layer indexes run-local evidence and renders a compact context view. It is
variant-gated and does not add task-specific logic or new solver tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import json

from harness.aether2.runtime.run_config import ContextPackPolicy
from harness.aether2.traces.receipt_store import QueryableReceiptStore
from harness.aether2.traces.task_local_tools import TaskLocalToolRegistry


class ReceiptDrivenVariant:
    def __init__(
        self,
        *,
        workspace_root: Path,
        task_id: str,
        success_contract: Mapping[str, Any],
        context_pack_policy: ContextPackPolicy,
    ) -> None:
        self.context_pack_policy = context_pack_policy
        self.store = QueryableReceiptStore(root=workspace_root, run_id=task_id)
        self.local_tools = TaskLocalToolRegistry(root=workspace_root)
        self.store.set_success_contract(success_contract)

    def model_context_message(self, *, proof_state: Mapping[str, Any] | None = None) -> dict[str, str]:
        payload = self.store.context_view(
            policy=self.context_pack_policy,
            local_tools=self.local_tools.summary(),
            proof_state=proof_state,
        )
        return {
            "role": "system",
            "content": "[receipt_context]\n" + json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        }

    def record_model_decision(
        self,
        *,
        step: int,
        text: str,
        tool_calls: list[Mapping[str, Any]] | None,
        plan_text: str | None,
    ) -> None:
        self.store.record_model_decision(step=step, text=text, tool_calls=tool_calls, plan_text=plan_text)

    def record_tool_invocations(self, invocations: list[Any]) -> None:
        for record in invocations:
            envelope = record.envelope
            files_changed = [getattr(item, "path", str(item)) for item in getattr(envelope, "files_changed", [])]
            raw_log_path = getattr(envelope, "raw_log_path", None)
            event = self.store.record_tool_result(
                step=int(record.step),
                tool_name=str(record.tool_name),
                arguments=dict(record.arguments),
                exit_code=getattr(envelope, "exit_code", None),
                stdout=(getattr(envelope, "stdout_head", "") or "") + (getattr(envelope, "stdout_tail", "") or ""),
                stderr=(getattr(envelope, "stderr_head", "") or "") + (getattr(envelope, "stderr_tail", "") or ""),
                raw_log_path=None if raw_log_path is None else str(raw_log_path),
                files_changed=files_changed,
            )
            self.local_tools.observe_tool_invocation(
                step=int(record.step),
                tool_name=str(record.tool_name),
                arguments=dict(record.arguments),
                exit_code=getattr(envelope, "exit_code", None),
                evidence_id=event.event_id,
                files_changed=files_changed,
            )

    def record_verification_feedback(self, *, step: int | None, ready: bool, feedback: Mapping[str, Any] | str) -> None:
        self.store.record_verification_feedback(step=step, ready=ready, feedback=feedback)


__all__ = ["ReceiptDrivenVariant"]
