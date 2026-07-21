"""Canonical current-state inspection registry.

Verifier evidence is accepted only through immutable inspection records.  Model
text may cite an ``inspection_id``; route, target, generation, tool identity,
result hash, and evidence ceiling are always derived from the executed result.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from .ledger import ExecutionLedger, Receipt
from .proof_contract import ROUTE_EVIDENCE_CEILINGS


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)


def _request_view(request: Any) -> dict[str, Any]:
    if request is None:
        return {}
    if is_dataclass(request):
        return dict(asdict(request))
    if isinstance(request, Mapping):
        return dict(request)
    return {
        key: getattr(request, key)
        for key in (
            "request_id", "kind", "path", "handle", "check_id", "receipt_kind",
            "limit", "command", "content", "target", "offset", "span",
        )
        if hasattr(request, key)
    }


def _target_identity(request: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    for key in ("path", "handle", "check_id", "target", "command", "process_id", "service_name"):
        value = str(result.get(key, "") or request.get(key, "")).strip()
        if value:
            return f"{key}:{value}"
    return "target:unspecified"


def _target_generation(result: Mapping[str, Any]) -> str:
    for key in (
        "content_hash", "sha256", "generation", "process_generation",
        "artifact_generation", "source_receipt_id",
    ):
        value = str(result.get(key, "")).strip()
        if value:
            return f"{key}:{value}"
    return "result:" + hashlib.sha256(_stable_json(result).encode("utf-8")).hexdigest()


def _tool_identity(result: Mapping[str, Any], *, executor: Any, overlay: Any | None) -> str:
    executed_in = str(result.get("executed_in", "")).strip()
    owner = overlay if executed_in == "verifier_overlay" and overlay is not None else executor
    cls = type(owner)
    return f"{cls.__module__}.{cls.__qualname__}:{executed_in or 'task_executor'}"


def _route(request: Mapping[str, Any], result: Mapping[str, Any]) -> tuple[str, str]:
    kind = str(result.get("kind", "") or request.get("kind", "")).strip()
    target = ""
    for key in ("path", "handle", "check_id", "target", "command"):
        target = str(result.get(key, "") or request.get(key, "")).strip()
        if target:
            break
    return kind, f"{kind}:{target}" if target else kind


def register_inspection_results(
    requests: Sequence[Any],
    results: Sequence[Mapping[str, Any]],
    *,
    ledger: ExecutionLedger,
    step: int,
    requester: str,
    executor: Any,
    overlay: Any | None,
    packet_signature: str,
) -> list[dict[str, Any]]:
    """Register every executed inspection and return rows enriched with IDs.

    Registration happens before rows return to the Verifier, so the model can
    cite only identities that already exist in the append-only ledger.
    """
    request_by_id: dict[str, dict[str, Any]] = {}
    positional: list[dict[str, Any]] = []
    for request in requests:
        view = _request_view(request)
        positional.append(view)
        request_id = str(view.get("request_id", "")).strip()
        if request_id and request_id not in request_by_id:
            request_by_id[request_id] = view

    existing = sum(1 for receipt in ledger.all_receipts() if receipt.kind == "inspection_record")
    enriched: list[dict[str, Any]] = []
    for index, raw_result in enumerate(results):
        result = dict(raw_result)
        request_id = str(result.get("request_id", "")).strip()
        request = request_by_id.get(request_id, positional[index] if index < len(positional) else {})
        kind, route = _route(request, result)
        ceiling = ROUTE_EVIDENCE_CEILINGS.get(kind, "")
        target_identity = _target_identity(request, result)
        target_generation = _target_generation(result)
        result_hash = hashlib.sha256(_stable_json(result).encode("utf-8")).hexdigest()
        inspection_id = f"inspection:{step}:{existing + index}:{request_id or kind or 'unknown'}"
        error = str(result.get("error", "")).strip()
        success = not bool(error)
        task_generation = ledger.task_state_generation()
        payload = {
            "inspection_id": inspection_id,
            "request_id": request_id,
            "requester": requester,
            "route_kind": kind,
            "route": route,
            "route_parameters": request,
            "target_identity": target_identity,
            "target_generation": target_generation,
            "task_state_generation": task_generation,
            "packet_signature": packet_signature,
            "tool_identity": _tool_identity(result, executor=executor, overlay=overlay),
            "result_hash": result_hash,
            "result_summary": error or str(result.get("summary", "") or result.get("excerpt", ""))[:1000],
            "evidence_ceiling": ceiling,
            "eligible_for_proof": bool(success and ceiling),
            "success": success,
            "error": error,
        }
        ledger.record(Receipt(
            receipt_id=inspection_id,
            step=step,
            kind="inspection_record",
            success=success,
            summary=(
                f"registered {kind or 'unknown'} inspection of {target_identity}"
                if success else f"inspection failed for {target_identity}: {error}"
            ),
            failure_class="" if success else "verifier_inspection_failed",
            payload=payload,
        ))
        result.update({
            "inspection_id": inspection_id,
            "registered_route": route,
            "target_identity": target_identity,
            "target_generation": target_generation,
            "observed_task_state_generation": task_generation,
            "tool_identity": payload["tool_identity"],
            "result_hash": result_hash,
            "evidence_ceiling": ceiling,
            "eligible_for_proof": payload["eligible_for_proof"],
        })
        enriched.append(result)
    return enriched


def inspection_records_by_id(ledger: ExecutionLedger) -> dict[str, Receipt]:
    return {
        str(receipt.payload.get("inspection_id", receipt.receipt_id)): receipt
        for receipt in ledger.all_receipts()
        if receipt.kind == "inspection_record"
    }


def inspection_ceilings_from_results(results: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    ceilings: dict[str, str] = {}
    for row in results:
        inspection_id = str(row.get("inspection_id", "")).strip()
        ceiling = str(row.get("evidence_ceiling", "")).strip()
        if inspection_id:
            ceilings[inspection_id] = ceiling
    return ceilings
