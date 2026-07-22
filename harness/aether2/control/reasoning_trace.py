"""Reasoning-trace builders for the Aether-2 control loop.

Responsibilities:
- Build per-step reasoning trace entries from model responses and tool invocations.
- Write the full reasoning trace to disk at finalization.
- Summarize model response usage and cost.
- Collect non-step model calls from receipt files.
- Digest tail-payload hashes for deduplication.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "_build_reasoning_trace_step",
    "_estimate_token_cost",
    "_model_visible_requirement_summary",
    "_response_cost",
    "_response_usage",
    "_tail_payload_digest",
    "_trace_envelope_summary",
    "_trace_non_step_model_calls",
    "_trace_tool_invocation_summary",
    "_write_reasoning_trace",
]


def _trace_envelope_summary(envelope: Any) -> dict[str, Any]:
    """Return a JSON-safe summary dict for an ObservationEnvelope."""

    return {
        "tool": envelope.tool,
        "exit_code": envelope.exit_code,
        "duration_sec": round(envelope.duration_sec, 3),
        "cwd": envelope.cwd,
        "stdout_head": envelope.stdout_head,
        "stdout_tail": envelope.stdout_tail,
        "stderr_head": envelope.stderr_head,
        "stderr_tail": envelope.stderr_tail,
        "truncated": envelope.truncated,
        "raw_log_path": envelope.raw_log_path,
        "files_changed": [item.__dict__ for item in envelope.files_changed],
        "process_delta": envelope.process_delta.__dict__,
        "blind_retry_blocked": envelope.blind_retry_blocked,
        "error": None if envelope.error is None else envelope.error.__dict__,
    }


def _trace_tool_invocation_summary(record: Any) -> dict[str, Any]:
    """Return a JSON-safe summary dict for a ToolInvocationRecord."""

    return {
        "step": record.step,
        "tool_name": record.tool_name,
        "arguments": record.arguments,
        "permission_decision": record.permission_decision,
        "hook_trace": record.hook_trace,
        "observation": _trace_envelope_summary(record.envelope),
    }


def _model_visible_requirement_summary(
    completion_contract: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the model-visible requirement summary dict from contract + ledger."""

    unresolved_requirements = completion_contract.get("unresolved_requirements")
    if not isinstance(unresolved_requirements, list):
        unresolved_requirements = []
    next_required_evidence = completion_contract.get("next_required_evidence")
    if not isinstance(next_required_evidence, list):
        next_required_evidence = []
    weak_evidence = completion_contract.get("weak_evidence")
    if not isinstance(weak_evidence, list):
        weak_evidence = []
    verifier_blockers = completion_contract.get("verifier_blockers")
    if not isinstance(verifier_blockers, list):
        verifier_blockers = []
    return {
        "unresolved_requirements": [str(item) for item in unresolved_requirements],
        "next_required_evidence": [str(item) for item in next_required_evidence],
        "weak_evidence": [str(item) for item in weak_evidence],
        "verifier_blockers": [str(item) for item in verifier_blockers],
        "persistent_blockers": list(ledger.get("blockers", []) or []),
    }


def _build_reasoning_trace_step(
    *,
    step: int | None,
    model_call_idx: int,
    call_role: str,
    response: Any,
    input_digests: Mapping[str, Any],
    visible_tail_state: Mapping[str, Any],
    completion_contract: Mapping[str, Any],
    pre_step_ledger: Mapping[str, Any],
    post_step_ledger: Mapping[str, Any],
    tool_invocations: list[Any],
    task_done_call: tuple[dict[str, Any], Any] | None,
    decision_kind: str,
    plan_text: str | None,
    model_exchange_ref: str,
    verification_round_index: int | None = None,
    blocker_state: Mapping[str, Any] | None = None,
    finalize_reason: str | None = None,
    frozen_success_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a single reasoning-trace step dict from all per-turn state."""

    from harness.aether2.control.completion import _ledger_progress  # local import to avoid cycle

    requirement_advanced, stronger_evidence_added = _ledger_progress(pre_step_ledger, post_step_ledger)
    task_done_summary: dict[str, Any] = {
        "called": task_done_call is not None,
        "summary": None,
        "checks": [],
    }
    if task_done_call is not None:
        task_done_summary["summary"] = str(task_done_call[0].get("summary", ""))
        task_done_summary["checks"] = [str(item) for item in task_done_call[0].get("checks", [])]

    return {
        "schema_version": 1,
        "step": step,
        "model_call_idx": model_call_idx,
        "call_role": call_role,
        "decision_kind": decision_kind,
        "assistant_text": getattr(response, "text", ""),
        "assistant_plan_after_turn": plan_text,
        "tool_call_count": len(tool_invocations),
        "tool_calls": [_trace_tool_invocation_summary(record) for record in tool_invocations],
        "model_input_digests": dict(input_digests),
        "visible_context": {
            "model_exchange_ref": model_exchange_ref,
            "frozen_success_contract": dict(frozen_success_contract or {}),
            "tail_state": visible_tail_state,
            "completion_contract": completion_contract,
            "model_visible_requirements": _model_visible_requirement_summary(completion_contract, post_step_ledger),
        },
        "pre_step_evidence_ledger": pre_step_ledger,
        "post_step_evidence_ledger": post_step_ledger,
        "verification_round_index": verification_round_index,
        "blocker_state": dict(blocker_state or {}),
        "progress": {
            "requirement_advanced": requirement_advanced,
            "stronger_evidence_added": stronger_evidence_added,
            "no_progress": not (requirement_advanced or stronger_evidence_added),
        },
        "task_done": task_done_summary,
        "finalize_reason": finalize_reason,
    }


def _write_reasoning_trace(
    *,
    trace_path: Path,
    task_id: str,
    task_dir: Path,
    workspace_root: Path,
    receipts_root: Path,
    steps: list[dict[str, Any]],
    non_step_model_calls: list[dict[str, Any]],
    model_call_count: int,
    finalize_reason: str,
    finalize_pass: bool,
) -> Path:
    """Serialize the full reasoning trace to disk and return the path."""

    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "task_dir": str(task_dir),
        "workspace_root": str(workspace_root),
        "receipts_root": str(receipts_root),
        "step_count": len(steps),
        "model_call_count": model_call_count,
        "finalize_reason": finalize_reason,
        "verifier_readiness": finalize_pass,
        "verifier_clean": finalize_pass,
        "steps": steps,
        "non_step_model_calls": non_step_model_calls,
    }
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    return trace_path


def _response_usage(response: Any) -> Mapping[str, Any]:
    """Extract the usage mapping from a model response."""

    usage = getattr(response, "usage", {})
    if isinstance(usage, Mapping):
        return usage
    return {}


def _estimate_token_cost(
    usage: Mapping[str, Any],
    *,
    input_per_mtok: float,
    output_per_mtok: float,
    cached_input_discount: float,
    large_context_threshold: int = 272_000,
    large_context_input_multiplier: float = 2.0,
    large_context_output_multiplier: float = 1.5,
) -> float:
    """Estimate USD cost for one response from token counts and known rates.

    Budget backstop for when the provider does not populate a `cost` field (e.g. a
    newly released model litellm has no price for). Cached input is priced at
    `cached_input_discount` of the input rate. Returns 0.0 when rates are unset.

    When a single request's total input exceeds `large_context_threshold` tokens,
    the long-context surcharge (e.g. Azure's 2x input / 1.5x output above 272K) is
    applied, so the estimate does not silently under-count large-context calls.
    """

    def _i(key: str) -> int:
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    cached = _i("cached_input_tokens")
    fresh = _i("fresh_input_tokens")
    if fresh == 0 and cached == 0:
        fresh = _i("input_tokens")
    output = _i("output_tokens")

    if (fresh + cached) > large_context_threshold:
        input_per_mtok *= large_context_input_multiplier
        output_per_mtok *= large_context_output_multiplier

    input_cost = fresh * input_per_mtok + cached * input_per_mtok * cached_input_discount
    return (input_cost + output * output_per_mtok) / 1_000_000


def _response_cost(response: Any) -> float:
    """Extract the cost float from a model response's raw_response."""

    raw_response = getattr(response, "raw_response", {})
    if isinstance(raw_response, Mapping):
        direct = raw_response.get("cost")
        if isinstance(direct, (int, float)):
            return float(direct)
        usage = raw_response.get("usage")
        if isinstance(usage, Mapping):
            value = usage.get("cost")
            if isinstance(value, (int, float)):
                return float(value)
    return 0.0


def _trace_non_step_model_calls(
    *,
    receipts_dir: Path,
    step_model_call_indices: set[int],
) -> list[dict[str, Any]]:
    """Collect model exchange records not already covered by a step."""

    non_step_calls: list[dict[str, Any]] = []
    for path in sorted(receipts_dir.glob("model_exchange_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        call_idx = payload.get("call_idx")
        if not isinstance(call_idx, int) or call_idx in step_model_call_indices:
            continue
        request_context = payload.get("request_context")
        if not isinstance(request_context, Mapping):
            request_context = {}
        non_step_calls.append(
            {
                "model_call_idx": call_idx,
                "call_role": payload.get("call_role"),
                "model_exchange_ref": str(path),
                "request_context": {
                    "env_contract": request_context.get("env_contract"),
                    "tool_schema_digest": request_context.get("tool_schema_digest"),
                    "tail_state_digest": _tail_payload_digest(request_context.get("tail_state")),
                },
            }
        )
    return non_step_calls


def _tail_payload_digest(payload: Any) -> str | None:
    """Return a SHA-256 hex digest of a JSON-serializable payload, or None."""

    if payload is None:
        return None
    try:
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except TypeError:
        return None
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
