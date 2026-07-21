"""Deterministic run-result metrics derived from kernel receipts.

These helpers deliberately read the ledger/result evidence, not transient model
hook state, so result rows cannot hide parse/protocol errors that occurred
before a later successful model call reset hook-local buffers.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from .kernel import KernelResult
from .ledger import Receipt


def _summary(receipt: Receipt, *, max_chars: int = 500) -> dict[str, Any]:
    payload = receipt.payload or {}
    return {
        "receipt_id": receipt.receipt_id,
        "step": receipt.step,
        "kind": receipt.kind,
        "success": receipt.success,
        "failure_class": receipt.failure_class,
        "summary": receipt.summary[:max_chars],
        "error": str(payload.get("error", ""))[:max_chars],
        "retry_attempted": payload.get("retry_attempted"),
    }


def parse_protocol_metrics(result: KernelResult) -> dict[str, Any]:
    """Aggregate solver parse/protocol evidence from immutable receipts."""
    receipts = tuple(result.receipts or ())
    solver_parse = [r for r in receipts if r.kind == "solver_parse_error"]
    turn_validation = [r for r in receipts if r.kind == "turn_validation"]
    unknown_actions = [r for r in receipts if r.kind == "unknown_action"]
    action_validation = [r for r in receipts if r.kind == "action_validation"]
    valid_action_steps: list[int] = []
    for receipt in receipts:
        if receipt.kind in {"run_command", "read_file", "write_file", "check_result", "service_probe", "artifact_inspection", "record_observation"}:
            valid_action_steps.append(receipt.step)
    repair_attempts = sum(1 for r in solver_parse if bool((r.payload or {}).get("retry_attempted")))
    repair_failures = sum(1 for r in solver_parse if (r.receipt_id or "").endswith("retry"))
    return {
        "solver_parse_error_count": len(solver_parse),
        "solver_parse_error_examples": [_summary(r) for r in solver_parse[:5]],
        "turn_validation_error_count": len(turn_validation),
        "unknown_action_count": len(unknown_actions),
        "action_validation_error_count": len(action_validation),
        "tool_schema_error_count": len(turn_validation) + len(unknown_actions) + len(action_validation),
        "parse_repair_attempts": repair_attempts,
        "parse_repair_failures": repair_failures,
        "parse_repair_successes": max(0, repair_attempts - repair_failures),
        "first_valid_action_step": min(valid_action_steps) if valid_action_steps else None,
    }


def model_parse_errors_for_row(result: KernelResult, hook_errors: Sequence[str] | None = None) -> list[Any]:
    """Return row-safe parse/protocol errors from receipts plus live hook state."""
    metrics = parse_protocol_metrics(result)
    row: list[Any] = list(metrics["solver_parse_error_examples"])
    for error in hook_errors or ():
        text = str(error).strip()
        if text:
            row.append({"source": "model_hooks.last_parse_errors", "error": text[:500]})
    return row


def repeated_action_metrics(result: KernelResult) -> dict[str, Any]:
    """Summarise repeated/low-information loop evidence from receipts."""
    receipts = tuple(result.receipts or ())
    commands: Counter[str] = Counter()
    writes: Counter[str] = Counter()
    submits = 0
    unchanged_submit_skips = 0
    for receipt in receipts:
        payload = receipt.payload or {}
        if receipt.kind == "run_command":
            command = str(payload.get("command", "")).strip()
            if command:
                commands[command] += 1
        elif receipt.kind == "write_file":
            path = str(payload.get("path", "")).strip()
            content_hash = str(payload.get("content_hash", "") or payload.get("sha256", "")).strip()
            if path:
                writes[f"{path}:{content_hash}"] += 1
        elif receipt.kind == "model_verifier_skipped" and payload.get("reason") == "active_findings_without_intervening_evidence":
            unchanged_submit_skips += 1
            submits += 1
        elif receipt.kind == "submit_outcome":
            submits += 1
    return {
        "repeated_command_count": sum(count - 1 for count in commands.values() if count > 1),
        "repeated_write_count": sum(count - 1 for count in writes.values() if count > 1),
        "submit_without_new_evidence_count": unchanged_submit_skips,
        "submit_receipt_count": submits,
        "repeated_commands": [
            {"command": command, "count": count}
            for command, count in commands.most_common()
            if count > 1
        ][:10],
    }


def run_metrics_for_row(result: KernelResult, hook_errors: Sequence[str] | None = None) -> dict[str, Any]:
    metrics = parse_protocol_metrics(result)
    metrics.update(repeated_action_metrics(result))
    metrics["model_parse_errors"] = model_parse_errors_for_row(result, hook_errors)
    return metrics
