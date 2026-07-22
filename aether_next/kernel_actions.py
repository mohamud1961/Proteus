"""Kernel-owned action handlers kept outside kernel.py for LOC discipline."""
from __future__ import annotations

from .kernel_checks import run_planned_check
from .ledger import ExecutionLedger, Receipt
from .memory_events import artifact_history, diff_summary_for_path
from .runtime_ir import ActionRequest, CompiledRuntime, EnvMap

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .execution import Executor


def handle_kernel_owned_action(
    action: ActionRequest,
    step: int,
    compiled: CompiledRuntime,
    executor: "Executor",
    envmap: EnvMap,
    ledger: ExecutionLedger,
) -> Receipt | None:
    if action.kind == "query_memory":
        q = str(action.arguments.get("query", ""))
        filters = action.arguments.get("filters")
        hits = ledger.query_memory(q, filters=filters if isinstance(filters, dict) else None)
        prior_queries = [r for r in ledger.all_receipts() if r.kind == "query_memory"]
        latest_prior = prior_queries[-1] if prior_queries else None
        no_new_evidence = False
        guidance = ""
        if latest_prior is not None:
            prior_payload = latest_prior.payload or {}
            same_query = str(prior_payload.get("query", "")) == q
            same_filters = dict(prior_payload.get("filters", {}) or {}) == dict(filters or {})
            prior_ids = {str(item.get("receipt_id", "")) for item in prior_payload.get("results", []) if isinstance(item, dict)}
            hit_ids = {str(item.get("receipt_id", "")) for item in hits if isinstance(item, dict)}
            if same_query and same_filters and hit_ids <= prior_ids:
                no_new_evidence = True
        if not hits:
            no_new_evidence = True
        if no_new_evidence:
            guidance = (
                "query_memory returned no new evidence. Use existing file/read/check evidence, inspect a concrete file, "
                "write or repair the artifact, or request reconfiguration; do not repeat the same memory query."
            )
        payload = {"query": q, "filters": filters or {}, "results": hits}
        if no_new_evidence:
            payload["no_new_evidence"] = True
            payload["guidance"] = guidance
        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:query", step=step, kind="query_memory",
            success=True, summary=f"memory query '{q[:60]}': {len(hits)} matches",
            payload=payload,
        )
    if action.kind == "query_artifact_history":
        path = str(action.arguments.get("path", "")).strip()
        rows = artifact_history(ledger.all_receipts(), path=path, limit=12)
        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:artifact_history", step=step,
            kind="query_artifact_history", success=True,
            summary=f"artifact history {path}: {len(rows)} events",
            payload={"path": path, "events": rows},
        )
    if action.kind == "inspect_diff":
        path = str(action.arguments.get("path", "")).strip()
        summary = diff_summary_for_path(ledger.all_receipts(), path=path)
        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:diff", step=step,
            kind="inspect_diff", success=True,
            summary=f"diff/history summary {path}: {summary['event_count']} events",
            payload=summary,
        )
    if action.kind == "record_observation":
        observation = str(action.arguments.get("observation", "")).strip()
        if not observation:
            return Receipt(
                receipt_id=f"step-{step}:{action.action_id}:observation", step=step,
                kind="record_observation", success=False,
                summary="record_observation requires non-empty observation",
                failure_class="action_validation",
            )
        payload = {
            "observation": observation,
            "source": str(action.arguments.get("source", "solver")).strip() or "solver",
            "confidence": str(action.arguments.get("confidence", "medium")).strip() or "medium",
            "tags": tuple(str(tag) for tag in action.arguments.get("tags", ()) or ()),
            "path": str(action.arguments.get("path", "")).strip(),
        }
        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:observation", step=step,
            kind="record_observation", success=True,
            summary=f"recorded observation: {observation[:80]}",
            state_change=True,
            payload={k: v for k, v in payload.items() if v not in ("", (), [])},
        )
    if action.kind == "inspect_checks":
        checks = [{"check_id": c.check_id, "label": c.label, "origin": c.origin} for c in compiled.planned_checks()]
        return Receipt(
            receipt_id=f"step-{step}:{action.action_id}:checks", step=step, kind="inspect_checks",
            success=True, summary=f"listed {len(checks)} planned checks", payload={"checks": checks},
        )
    if action.kind == "run_check":
        check_id = str(action.arguments.get("check_id", ""))
        return run_planned_check(step, compiled, executor, envmap, check_id, receipt_prefix=f"step-{step}:{action.action_id}")
    return None
