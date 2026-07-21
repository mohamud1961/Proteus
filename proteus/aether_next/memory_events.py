"""Structured ledger-memory helpers.

These helpers deliberately project existing receipts into stable, queryable
memory/event views.  They do not create a separate database yet; they give the
context compiler, kernel-owned tools, and verifier packets a common structured
surface over the receipt ledger.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Iterable

from .ledger import Receipt
from .redaction import redact_secrets

PATH_PAYLOAD_KEYS = ("path", "artifact_path")
PATH_LIST_PAYLOAD_KEYS = ("artifact_paths", "modified_paths")


def content_hash(text: str) -> str:
    return sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def receipt_paths(receipt: Receipt) -> tuple[str, ...]:
    payload = receipt.payload or {}
    paths: list[str] = []
    for key in PATH_PAYLOAD_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    for key in PATH_LIST_PAYLOAD_KEYS:
        value = payload.get(key, ()) or ()
        if isinstance(value, str):
            if value.strip():
                paths.append(value.strip())
        else:
            paths.extend(str(item).strip() for item in value if str(item).strip())
    return tuple(dict.fromkeys(paths))


def event_type_for_receipt(receipt: Receipt) -> str:
    mapping = {
        "read_file": "file_read",
        "write_file": "file_write",
        "run_command": "command_run",
        "check_result": "check_result",
        "model_verifier_result": "model_verifier_result",
        "context_packet": "context_packet",
        "config_realization": "config_realization",
        "query_memory": "memory_query",
        "record_observation": "observation",
    }
    return mapping.get(receipt.kind, receipt.kind)


def receipt_to_memory_event(receipt: Receipt) -> dict[str, Any]:
    payload = receipt.payload or {}
    event: dict[str, Any] = {
        "event_id": receipt.receipt_id,
        "step": receipt.step,
        "event_type": event_type_for_receipt(receipt),
        "receipt_kind": receipt.kind,
        "success": receipt.success,
        "summary": receipt.summary,
        "state_change": receipt.state_change,
    }
    if receipt.failure_class:
        event["failure_class"] = receipt.failure_class
    paths = receipt_paths(receipt)
    if paths:
        event["paths"] = list(paths)
    for key in (
        "path", "command", "check_id", "passed", "origin", "detail",
        "content_hash", "before_content_hash", "after_content_hash", "bytes",
        "excerpt", "stdout_tail", "stderr_tail", "exit_code", "observation",
        "source", "confidence", "tags",
    ):
        if key in payload:
            event[key] = redact_secrets(payload[key])
    return event


def memory_events(receipts: Iterable[Receipt], *, limit: int | None = None) -> list[dict[str, Any]]:
    rows = [receipt_to_memory_event(receipt) for receipt in receipts]
    if limit is None:
        return rows
    return rows[-max(0, int(limit)):]


def artifact_history(
    receipts: Iterable[Receipt],
    *,
    path: str | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    wanted = str(path or "").strip()
    rows: list[dict[str, Any]] = []
    for receipt in receipts:
        paths = receipt_paths(receipt)
        if wanted and wanted not in paths:
            continue
        if not paths and wanted:
            continue
        event = receipt_to_memory_event(receipt)
        if paths:
            event["matched_paths"] = list(paths if not wanted else (wanted,))
        rows.append(event)
    return rows[-max(0, int(limit)):]


def diff_summary_for_path(receipts: Iterable[Receipt], *, path: str) -> dict[str, Any]:
    history = artifact_history(receipts, path=path, limit=50)
    writes = [event for event in history if event["receipt_kind"] == "write_file"]
    reads = [event for event in history if event["receipt_kind"] == "read_file"]
    latest = history[-1] if history else None
    before_hashes = [str(event.get("before_content_hash", "")) for event in writes if event.get("before_content_hash")]
    after_hashes = [str(event.get("after_content_hash", "")) for event in writes if event.get("after_content_hash")]
    read_hashes = [str(event.get("content_hash", "")) for event in reads if event.get("content_hash")]
    return {
        "path": path,
        "event_count": len(history),
        "write_count": len(writes),
        "read_count": len(reads),
        "latest_event": latest,
        "known_hashes": list(dict.fromkeys(before_hashes + after_hashes + read_hashes)),
        "changed_by_write": any(
            event.get("before_content_hash") != event.get("after_content_hash")
            for event in writes
            if event.get("after_content_hash")
        ),
        "history": history[-12:],
    }
