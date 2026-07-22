"""Deterministic context rebasing."""

from __future__ import annotations

from typing import Any, Mapping
import json

from harness.aether2.runtime.context import ContextManager
from harness.aether2.traces.delta import compact_evidence_ledger
from harness.aether2.runtime.prompts import HANDOFF_TEMPLATE


def should_rebase(window_used_frac: float, model_requested: bool) -> bool:
    return model_requested or window_used_frac >= 0.60


def build_fact_ledger(
    delta_state: Any,
    *,
    orientation: Mapping[str, Any] | None = None,
    completion_contract: Mapping[str, Any] | None = None,
    tail_payload: Mapping[str, Any] | None = None,
    digest_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    files = getattr(delta_state, "files", {}) or {}
    artifact_registry = getattr(delta_state, "artifact_registry", {}) or {}
    workspace_root = getattr(delta_state, "workspace_root", None)
    written_files = [
        {"path": path, "sha256": digest}
        for path, digest in sorted(files.items())
    ]
    artifact_paths = sorted(str(path) for path in artifact_registry)
    fact_ledger: dict[str, Any] = {
        "written_files": written_files,
        "artifacts": artifact_paths,
        "jobs": _sorted_registry(delta_state, "job_registry"),
        "sessions": _sorted_registry(delta_state, "session_registry"),
        "services": _sorted_registry(delta_state, "service_registry"),
        "processes": _sorted_registry(delta_state, "process_registry"),
        "installed_packages": _stable_sequence(getattr(delta_state, "installed_packages", []) or []),
        "nonzero_exits": _stable_sequence(getattr(delta_state, "nonzero_exits", []) or []),
        "evidence_ledger": compact_evidence_ledger(getattr(delta_state, "evidence_ledger", {}) or {}),
    }
    if isinstance(workspace_root, str) and workspace_root.strip():
        fact_ledger["workspace_root"] = workspace_root.strip()
    if orientation:
        fact_ledger["orientation"] = _stable_value(dict(orientation))
    if completion_contract:
        fact_ledger["completion_contract"] = _stable_value(dict(completion_contract))
    if tail_payload:
        fact_ledger["tail_payload"] = _stable_value(dict(tail_payload))
    if digest_snapshot:
        fact_ledger["digest_snapshot"] = _stable_value(dict(digest_snapshot))
    return fact_ledger


def build_receipt_continuity_snapshot(
    receipt_store: Any,
    context_pack_policy: Any,
    local_tools: Any | None = None,
    proof_state: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Extract durable facts from the receipt store for compaction preservation.

    Returns a dict suitable for injection as a [receipt_continuity] prefix
    message, or None when the receipt store is unavailable.
    """
    if receipt_store is None:
        return None
    view = receipt_store.context_view(
        policy=context_pack_policy,
        local_tools=local_tools,
        proof_state=proof_state,
    )
    if not view:
        return None
    return view


def rebase(
    context: ContextManager,
    model_client: Any,
    *,
    record_exchange: Any | None = None,
    receipt_continuity_snapshot: dict[str, Any] | None = None,
) -> ContextManager:
    if context.prefix is None:
        raise ValueError("context prefix must be built before rebase")
    fact_ledger = build_fact_ledger(
        context.delta_state,
        orientation=context.orientation,
        completion_contract=context.current_completion_contract(),
        tail_payload=context.current_tail_payload(),
        digest_snapshot=context.digest_snapshot(),
    )
    handoff_messages = [
        {"role": "system", "content": HANDOFF_TEMPLATE},
        {"role": "user", "content": context.task_instruction},
    ]
    prompt_messages = [
        *handoff_messages,
        {
            "role": "user",
            "content": json.dumps(
                {
                    "fact_ledger": fact_ledger,
                    "last_turns": context.transcript[-10:],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        },
    ]
    response = _call_model(model_client, prompt_messages)
    if record_exchange is not None:
        record_exchange(
            prompt_messages,
            response,
            [],
            call_role="compaction",
            ledger_state=fact_ledger,
            tail_state=context.current_tail_payload(),
        )
    handoff_text = _extract_text(response)
    rebased = ContextManager(
        delta_state=context.delta_state,
        compaction_generation=context.compaction_generation + 1,
    )
    # Receipt-continuity preservation: when the receipt-driven variant is
    # active, durable facts from the receipt store are re-injected after
    # rebase so the model retains coherent state (plan, contract, candidates,
    # tools, failures/feedback).  Baseline (snapshot is None) is unchanged.
    receipt_continuity_messages: list[dict[str, Any]] = []
    if receipt_continuity_snapshot is not None:
        receipt_continuity_messages = [
            {
                "role": "system",
                "content": "[receipt_continuity]\n"
                + json.dumps(
                    receipt_continuity_snapshot,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
            },
        ]
    rebased.build_prefix(
        system_prompt=context.system_prompt,
        task_instruction=context.task_instruction,
        orientation=context.orientation,
        tool_schemas=context.tool_schemas,
        frozen_success_contract=context.current_frozen_success_contract(),
        extra_prefix_messages=[
            {
                "role": "system",
                "content": "[deterministic_fact_ledger]\n"
                + json.dumps(fact_ledger, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            },
            {"role": "assistant", "content": handoff_text},
            *context.transcript[-10:],
            # Durable receipt-driven facts survive compaction here.
            # Future: add proof_state to receipt_continuity_snapshot when built.
            *receipt_continuity_messages,
        ],
    )
    return rebased


def _call_model(model_client: Any, messages: list[dict[str, Any]]) -> Any:
    if hasattr(model_client, "call"):
        return model_client.call(messages, [], cache_prefix_len=0)
    raise TypeError("model_client must define call(messages, tools, *, cache_prefix_len)")


def _extract_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        for key in ("output_text", "text", "content", "summary"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value
        messages = response.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                    return str(message["content"])
    if hasattr(response, "output_text") and isinstance(response.output_text, str):
        return response.output_text
    if hasattr(response, "text") and isinstance(response.text, str):
        return response.text
    return json.dumps(response, sort_keys=True, default=str, ensure_ascii=True)


def _sorted_registry(delta_state: Any, name: str) -> dict[str, Any]:
    registry = getattr(delta_state, name, {}) or {}
    return {str(key): _stable_value(registry[key]) for key in sorted(registry)}


def _stable_sequence(values: Any) -> list[Any]:
    normalized = [_stable_value(value) for value in values if value is not None]
    return sorted(normalized, key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _stable_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    if isinstance(value, tuple):
        return [_stable_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_stable_value(item) for item in value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _stable_value(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _stable_value(vars(value))
    return str(value)
