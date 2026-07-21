"""Structured retrieval helpers for ExecutionLedger receipts."""
from __future__ import annotations

from typing import Any, Iterable

from .ledger import Receipt
from .memory_events import receipt_paths


def _haystack(receipt: Receipt) -> str:
    payload = receipt.payload or {}
    parts = [receipt.receipt_id, receipt.kind, receipt.summary, receipt.failure_class]
    for key in (
        "path", "command", "check_id", "detail", "excerpt", "content_hash",
        "stdout_tail", "stderr_tail", "observation", "after_content_hash", "before_content_hash",
    ):
        value = payload.get(key)
        if value is not None:
            parts.append(str(value))
    for value in payload.get("tags", ()) or ():
        parts.append(str(value))
    return " ".join(parts).lower()


def _score(receipt: Receipt, terms: list[str]) -> int:
    hay = _haystack(receipt)
    score = sum(1 for term in terms if term and term in hay)
    payload = receipt.payload or {}
    if payload.get("path") and any(term in str(payload["path"]).lower() for term in terms):
        score += 3
    if payload.get("check_id") and any(term in str(payload["check_id"]).lower() for term in terms):
        score += 3
    if receipt.failure_class and any(term in receipt.failure_class.lower() for term in terms):
        score += 2
    return score


def receipt_to_memory_result(receipt: Receipt) -> dict[str, Any]:
    payload = receipt.payload or {}
    result: dict[str, Any] = {
        "receipt_id": receipt.receipt_id,
        "step": receipt.step,
        "kind": receipt.kind,
        "success": receipt.success,
        "summary": receipt.summary,
        "state_change": receipt.state_change,
        "failure_class": receipt.failure_class,
    }
    for key in (
        "path", "command", "check_id", "content_hash", "bytes", "excerpt",
        "detail", "passed", "origin", "modified_paths", "artifact_paths",
        "stdout_tail", "stderr_tail", "exit_code", "observation", "after_content_hash", "before_content_hash",
    ):
        if key in payload:
            result[key] = payload[key]
    return result


def query_receipts(
    receipts: Iterable[Receipt],
    query: str,
    *,
    filters: dict[str, Any] | None = None,
    max_results: int = 8,
) -> list[dict[str, Any]]:
    filters = filters or {}
    terms = [term for term in str(query).lower().replace("/", " ").replace("_", " ").split() if term]
    event_types = set(filters.get("event_type", ()) or filters.get("kind", ()) or ())
    include_query_receipts = "query_memory" in event_types
    path = str(filters.get("path", "")).strip().lower()
    check_id = str(filters.get("check_id", "")).strip().lower()
    failure_kind = str(filters.get("failure_kind", "")).strip().lower()
    since_step = int(filters.get("since_step", 0) or 0)

    ranked: list[tuple[int, int, Receipt]] = []
    for receipt in receipts:
        payload = receipt.payload or {}
        # A previous query_memory receipt is not substantive task evidence and
        # can create self-referential memory loops. It is searchable only when
        # explicitly requested by kind/event_type.
        if receipt.kind == "query_memory" and not include_query_receipts:
            continue
        if event_types and receipt.kind not in event_types:
            continue
        if since_step and receipt.step < since_step:
            continue
        if path:
            paths = " ".join(receipt_paths(receipt)).lower()
            if path not in paths and path not in str(payload.get("path", "")).lower():
                continue
        if check_id and check_id != str(payload.get("check_id", "")).lower():
            continue
        if failure_kind and failure_kind != receipt.failure_class.lower():
            continue
        score = _score(receipt, terms) if terms else 1
        if score <= 0 and not filters:
            continue
        ranked.append((score, receipt.step, receipt))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [receipt_to_memory_result(receipt) for _, _, receipt in ranked[:max_results]]


def repeat_guard(receipts: Iterable[Receipt], *, kind: str, target: str) -> dict[str, Any]:
    target = str(target).strip()
    matches: list[Receipt] = []
    for receipt in receipts:
        payload = receipt.payload or {}
        if receipt.kind != kind:
            continue
        observed = payload.get("path") if kind == "read_file" else payload.get("command")
        if str(observed).strip() == target:
            matches.append(receipt)
    hashes = {str(r.payload.get("content_hash", "")) for r in matches if r.payload.get("content_hash")}
    return {
        "kind": kind,
        "target": target,
        "repeat_count": len(matches),
        "steps": [r.step for r in matches],
        "same_content_hash": len(hashes) == 1 and bool(hashes),
        "likely_wasteful": len(matches) >= 2 and (kind != "read_file" or len(hashes) <= 1),
    }
