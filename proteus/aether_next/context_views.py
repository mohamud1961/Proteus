"""Receipt/section view builders for the solver context packet.

Extracted from context_compiler.py to honor the 500-LOC module cap.  Pure
functions over the ledger; no compiler state.
"""
from __future__ import annotations

import json
from typing import Any

from .ledger import ExecutionLedger, Receipt
from .memory_events import artifact_history

_TOOL_RESULT_KINDS = frozenset({
    "read_file",
    "write_file",
    "run_command",
    "bootstrap",
    "process_launch",
    "service_probe",
    "process_stop",
    "artifact_inspection",
    "inspect_checks",
    "query_memory",
    "automatic_memory",
    "query_artifact_history",
    "inspect_diff",
    "record_observation",
    "experiment",
})


_RECENT_RECIPE_SELECTORS = frozenset({
    "recent_progress",
    "tool_results",
    "file_reads",
    "file_writes",
    "command_results",
    "check_results",
    "query_memory_results",
    "verifier_results",
    "artifact_history",
    "observations",
})


_STANDARD_SECTION_KEYS = (
    "open_obligations",
    "obligation_status",
    "monitor_alerts",
    "live_processes",
    "recent_progress",
    "failure_clusters",
    "artifacts_present",
    "candidate_leaderboard",
    "installed_capabilities",
    "planned_checks",
    "tool_results",
    "command_results",
)

_SUPPORTED_EXACT_RECIPE_SELECTORS = frozenset({
    *_STANDARD_SECTION_KEYS,
    "pending_checks",
    "active_completion_findings",
    "repeated_actions",
    "repeat_efficiency_guidance",
    "files_already_read",
    "latest_file_reads",
    "memory_loop_feedback",
    "automatic_memory_findings",
    "no_progress_controls",
    "action_constraints",
    "stuck",
    "artifact_history",
    "memory_events",
    "latest_failure",
    "failed_checks",
    "observations",
})


def item_count(value: Any) -> int:
    if isinstance(value, (list, tuple, dict, set)):
        return len(value)
    if value in (None, "", False):
        return 0
    return 1


def recent_receipts(selector: str, ledger: ExecutionLedger, count: int) -> list[Receipt]:
    receipts = list(ledger.all_receipts())
    if selector == "recent_progress":
        matches = [
            receipt
            for receipt in receipts
            if receipt.state_change
            or (receipt.kind == "check_result" and receipt.success)
            or (receipt.kind == "schema_validation" and receipt.success)
        ]
    elif selector == "tool_results":
        matches = [receipt for receipt in receipts if receipt.kind in _TOOL_RESULT_KINDS]
    elif selector == "file_reads":
        matches = [receipt for receipt in receipts if receipt.kind == "read_file"]
    elif selector == "file_writes":
        matches = [receipt for receipt in receipts if receipt.kind == "write_file"]
    elif selector == "command_results":
        matches = [receipt for receipt in receipts if receipt.kind == "run_command"]
    elif selector == "check_results":
        matches = [receipt for receipt in receipts if receipt.kind == "check_result"]
    elif selector == "query_memory_results":
        matches = [receipt for receipt in receipts if receipt.kind == "query_memory"]
    elif selector == "verifier_results":
        matches = [receipt for receipt in receipts if receipt.kind == "model_verifier_result"]
    elif selector == "artifact_history":
        matches = [receipt for receipt in receipts if artifact_history((receipt,), limit=1)]
    elif selector == "observations":
        matches = [receipt for receipt in receipts if receipt.kind == "record_observation"]
    else:
        matches = []
    return matches[-count:]

def last_failures(ledger: ExecutionLedger, count: int) -> list[Receipt]:
    failures = [receipt for receipt in ledger.all_receipts() if not receipt.success]
    return failures[-count:]

def latest_tool_receipt(ledger: ExecutionLedger) -> dict[str, Any] | None:
    tool_kinds = {"read_file", "write_file", "run_command", "query_memory", "check_result", "schema_validation"}
    for receipt in reversed(ledger.all_receipts()):
        if receipt.kind in tool_kinds:
            return {
                "receipt_id": receipt.receipt_id,
                "step": receipt.step,
                "kind": receipt.kind,
                "success": receipt.success,
                "summary": receipt.summary,
                "failure_class": receipt.failure_class,
            }
    return None

def queryable_section_meta(selector: str, value: Any) -> dict[str, Any]:
    return {
        "selector": selector,
        "section": selector,
        "item_count": item_count(value),
        "access": "query_memory",
        "reason": "recipe_make_queryable_not_inline",
    }

def queryable_receipt_meta(selector: str, receipts: list[Receipt], requested_count: int) -> dict[str, Any]:
    return {
        "selector": selector,
        "section": selector,
        "requested_count": requested_count,
        "matching_count": len(receipts),
        "receipt_ids": [receipt.receipt_id for receipt in receipts],
        "access": "query_memory",
        "reason": "recipe_make_queryable_not_inline",
    }

def _compact_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    half = max(1, (limit - 80) // 2)
    return f"{text[:half]}\n... [truncated {len(text) - (2 * half)} chars] ...\n{text[-half:]}"


def receipt_inline_view(receipt: Receipt) -> dict[str, Any]:
    row: dict[str, Any] = {
        "receipt_id": receipt.receipt_id,
        "step": receipt.step,
        "kind": receipt.kind,
        "success": receipt.success,
        "summary": receipt.summary,
    }
    if receipt.failure_class:
        row["failure_class"] = receipt.failure_class
    payload = receipt.payload
    for key in (
        "path", "command", "check_id", "exit_code", "bytes", "query", "detail",
        "blocker_code", "stdout_handle", "stderr_handle", "file_handle", "offset", "span",
        "target", "service_name", "live", "fresh", "process_id", "interactive",
        "mode", "media_type", "extraction_route", "extraction_authority",
        "content_hash", "sha256", "owner", "permissions", "file_type", "status",
    ):
        value = payload.get(key)
        if value not in (None, "", (), [], {}):
            row[key] = value
    if receipt.kind in {"read_file", "write_file"} and payload.get("excerpt"):
        row["excerpt"] = _compact_text(payload["excerpt"], 4000)
    if receipt.kind == "artifact_inspection":
        extracted = payload.get("extracted_text") or payload.get("transcription") or payload.get("description")
        if extracted:
            row["extracted_text"] = _compact_text(extracted, 4000)
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata:
            row["metadata"] = {k: v for k, v in metadata.items() if v not in (None, "", (), [], {})}
    if receipt.kind == "run_command":
        stdout = str(payload.get("stdout", ""))
        stderr = str(payload.get("stderr", ""))
        if stdout:
            row["stdout"] = stdout
        if stderr:
            row["stderr"] = stderr
    if receipt.kind in {"read_output", "grep_output", "read_file_page"}:
        chunk = str(payload.get("chunk", ""))
        if chunk:
            row["chunk"] = chunk
    if receipt.kind == "query_memory":
        results = payload.get("results", [])
        if isinstance(results, list):
            row["result_count"] = len(results)
        for key in ("no_new_evidence", "guidance"):
            value = payload.get(key)
            if value not in (None, "", (), [], {}):
                row[key] = value
    if receipt.kind == "no_progress_control":
        for key in ("consequence", "target", "action_family", "repeat_count"):
            value = payload.get(key)
            if value not in (None, "", (), [], {}):
                row[key] = value
    return row

def latest_file_reads(ledger: ExecutionLedger, limit: int = 3) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for receipt in ledger.all_receipts():
        if receipt.kind != "read_file" or not receipt.success:
            continue
        payload = receipt.payload or {}
        row = {
            "receipt_id": receipt.receipt_id,
            "step": receipt.step,
            "path": payload.get("path", ""),
            "content_hash": payload.get("content_hash", ""),
            "bytes": payload.get("bytes", 0),
            "excerpt": payload.get("excerpt", ""),
        }
        rows.append({k: v for k, v in row.items() if v not in (None, "", (), [], {})})
    return rows[-max(0, limit):]

def memory_loop_feedback(ledger: ExecutionLedger) -> dict[str, Any] | None:
    queries = [r for r in ledger.all_receipts() if r.kind == "query_memory"]
    if len(queries) < 2:
        return None
    recent = queries[-3:]
    empty_or_same = []
    for receipt in recent:
        payload = receipt.payload or {}
        results = payload.get("results", [])
        empty_or_same.append(len(results) == 0 or bool(payload.get("no_new_evidence")))
    if len(recent) >= 2 and all(empty_or_same[-2:]):
        return {
            "repeated_memory_queries": len(recent),
            "latest_query": str((recent[-1].payload or {}).get("query", "")),
            "guidance": (
                "Repeated query_memory calls produced no new evidence. Act on existing file/check evidence, "
                "inspect a concrete file, write/repair the artifact, or request missing capability; do not keep querying memory."
            ),
            "recent_receipt_ids": [r.receipt_id for r in recent],
        }
    return None

def automatic_memory_findings(ledger: ExecutionLedger, limit: int = 4) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for receipt in ledger.all_receipts():
        if receipt.kind != "automatic_memory" or not receipt.success:
            continue
        payload = receipt.payload or {}
        rows.append({
            "receipt_id": receipt.receipt_id,
            "step": receipt.step,
            "summary": receipt.summary,
            "action_kind": payload.get("action_kind"),
            "target": payload.get("target"),
            "match_count": payload.get("match_count"),
            "latest_receipt_id": payload.get("latest_receipt_id"),
            "same_content_hash": payload.get("same_content_hash"),
            "repeat_justified": payload.get("repeat_justified"),
            "guidance": payload.get("guidance"),
            "recent_evidence": payload.get("recent_evidence", [])[:2],
        })
    return rows[-max(0, limit):]

def action_constraints_from_no_progress(no_progress_controls: list[dict[str, Any]]) -> dict[str, Any]:
    latest = no_progress_controls[-1]
    payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
    target = str(payload.get("target", latest.get("target", ""))).strip()
    consequence = str(payload.get("consequence", latest.get("consequence", ""))).strip()
    action_family = payload.get("action_family", latest.get("action_family", "evidence_display_command"))
    return {
        "source": "no_progress_control",
        "consequence": consequence or "soft_block",
        "blocked_action_family": action_family,
        "blocked_target": target,
        "do_not_repeat": [
            {
                "action_family": action_family,
                "target": target,
            }
        ],
        "allowed_next_action_families": [
            "repair_or_write_artifact",
            "execute_or_semantically_validate_artifact",
            "inspect_new_target",
            "declare_concrete_blocker",
        ],
        "message": (
            "The runtime already blocked repeated evidence display. The next action must repair the artifact, "
            "execute or semantically validate it, inspect a different target, or declare a concrete blocker."
        ),
    }

def maybe_compress(packet: dict[str, Any], policy: Any) -> dict[str, Any]:
    budget = int(policy.model_context_window_tokens * policy.compression_trigger_ratio)
    estimated_tokens = max(1, len(json.dumps(packet, sort_keys=True, default=str)) // 4)
    if estimated_tokens <= budget:
        return packet
    compressed = dict(packet)
    preserved_exact = {"active_completion_findings", "pending_checks", "repeat_efficiency_guidance", "no_progress_controls", "action_constraints", "stuck", "tool_results", "command_results", "latest_file_reads", "solver_parse_errors", "blocked_denied_receipts", "output_handles"}
    recipe = getattr(policy, "recipe", None)
    if recipe is not None:
        preserved_exact.update(str(item) for item in recipe.preserve_exact)
    for key in ("recent_progress", "failure_clusters", "candidate_leaderboard"):
        if key in preserved_exact:
            continue
        if isinstance(compressed.get(key), list):
            original = compressed[key]
            compressed[key] = original[-3:]
            compressed[f"{key}_compressed"] = {"original_count": len(original), "kept_last": len(compressed[key])}
    compressed["context_compression"] = {
        "triggered": True,
        "estimated_tokens_before": estimated_tokens,
        "budget_tokens": budget,
        "threshold_ratio": policy.compression_trigger_ratio,
        "preserved_exact": sorted(preserved_exact),
    }
    return compressed
