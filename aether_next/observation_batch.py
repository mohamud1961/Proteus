"""Certified read-only observation batches for one Solver decision frontier."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from typing import Any, Mapping

from .kernel_actions import handle_kernel_owned_action
from .kernel_dispatch import dispatch_action
from .ledger import ExecutionLedger, Receipt
from .runtime_ir import (
    ActionRequest,
    CERTIFIED_READ_ONLY_ACTION_KINDS,
    CompiledRuntime,
    EnvMap,
)
from .execution import Executor


MAX_OBSERVATIONS_PER_BATCH = 8


def mutation_generation(ledger: ExecutionLedger) -> str:
    """Return a stable token for the currently observed mutation frontier."""
    mutations = [receipt for receipt in ledger.all_receipts() if receipt.state_change]
    last = mutations[-1].receipt_id if mutations else "initial"
    material = f"{len(mutations)}\x00{last}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:20]


def execute_observation_batch(
    kernel: Any,
    action: ActionRequest,
    *,
    step: int,
    compiled: CompiledRuntime,
    executor: Executor,
    envmap: EnvMap,
    ledger: ExecutionLedger,
) -> tuple[Receipt, ...]:
    """Execute a bounded set of certified read-only operations.

    Children are independent observations, not deferred task actions.  They all
    bind to the same pre-batch mutation generation and every success/failure is
    returned.  Any child outside the certified set rejects the whole batch
    before execution.
    """
    raw_operations = action.arguments.get("operations")
    if not isinstance(raw_operations, (list, tuple)):
        return (_validation_receipt(action, step, "operations must be a list"),)
    operations = tuple(raw_operations)
    if not 1 <= len(operations) <= MAX_OBSERVATIONS_PER_BATCH:
        return (_validation_receipt(
            action,
            step,
            f"observation batch requires 1-{MAX_OBSERVATIONS_PER_BATCH} operations",
        ),)

    children: list[ActionRequest] = []
    for index, raw in enumerate(operations):
        if not isinstance(raw, Mapping):
            return (_validation_receipt(action, step, f"operations[{index}] must be an object"),)
        kind = str(raw.get("kind", "")).strip()
        if kind not in CERTIFIED_READ_ONLY_ACTION_KINDS:
            return (_validation_receipt(
                action,
                step,
                f"operations[{index}] kind {kind!r} is not certified read-only",
            ),)
        arguments = raw.get("arguments", {})
        if not isinstance(arguments, Mapping):
            return (_validation_receipt(action, step, f"operations[{index}].arguments must be an object"),)
        child_id = str(raw.get("request_id") or raw.get("action_id") or f"obs-{index}").strip()
        child = ActionRequest(
            action_id=f"{action.action_id}:{child_id}",
            kind=kind,
            capability_id=str(raw.get("capability_id", "") or ""),
            arguments=dict(arguments),
            intent="certified read-only observation",
            expected_observation=str(raw.get("expected_observation", "") or ""),
            if_fail_next="",
            target=dict(raw.get("target", {}) or {}) if isinstance(raw.get("target", {}), Mapping) else {},
        )
        errors = child.validate(compiled.action_schema)
        if errors:
            return (_validation_receipt(
                action,
                step,
                f"operations[{index}] invalid: {'; '.join(errors)}",
            ),)
        safety_violation = kernel.safety_guard.violation(compiled, child, network_scope=envmap.network_scope)
        if safety_violation:
            return (_validation_receipt(
                action,
                step,
                f"operations[{index}] rejected by safety policy: {safety_violation}",
                failure_class="safety_violation",
            ),)
        children.append(child)

    generation = mutation_generation(ledger)
    results: list[Receipt] = []
    for index, child in enumerate(children):
        handled = handle_kernel_owned_action(child, step, compiled, executor, envmap, ledger)
        child_results = (handled,) if handled is not None else tuple(
            dispatch_action(kernel, child, step, compiled, executor, envmap, ledger)
        )
        if not child_results:
            child_results = (Receipt(
                receipt_id=f"step-{step}:{child.action_id}:empty_result",
                step=step,
                kind="observation_result_missing",
                success=False,
                summary="certified observation returned no receipt",
                failure_class="harness_observation_result_missing",
            ),)
        for receipt in child_results:
            results.append(replace(receipt, payload={
                **dict(receipt.payload or {}),
                "observation_batch_id": action.action_id,
                "observation_child_index": index,
                "observation_child_id": child.action_id,
                "observed_mutation_generation": generation,
                "certified_read_only": True,
                "model_requested_action": True,
                "solver_action_id": action.action_id,
                "solver_action_kind": "observe_batch",
            }))

    mutated = [receipt.receipt_id for receipt in results if receipt.state_change]
    after_generation = mutation_generation(ledger)
    if mutated or after_generation != generation:
        results.append(Receipt(
            receipt_id=f"step-{step}:{action.action_id}:mutation_detected",
            step=step,
            kind="observation_batch_mutation_detected",
            success=False,
            summary="certified read-only observation batch changed task state",
            failure_class="harness_read_only_contract_violation",
            payload={
                "observation_batch_id": action.action_id,
                "before_generation": generation,
                "after_generation": after_generation,
                "state_changing_receipt_ids": mutated,
            },
        ))
    results.append(Receipt(
        receipt_id=f"step-{step}:{action.action_id}:batch_result",
        step=step,
        kind="observation_batch_result",
        success=not mutated and after_generation == generation,
        summary=f"completed {len(children)} certified read-only observations",
        failure_class=(
            "" if not mutated and after_generation == generation
            else "harness_read_only_contract_violation"
        ),
        payload={
            "observation_batch_id": action.action_id,
            "operation_count": len(children),
            "observed_mutation_generation": generation,
            "child_action_ids": [child.action_id for child in children],
            "complete_result_set": True,
        },
    ))
    return tuple(results)


def _validation_receipt(
    action: ActionRequest,
    step: int,
    detail: str,
    *,
    failure_class: str = "action_validation",
) -> Receipt:
    return Receipt(
        receipt_id=f"step-{step}:{action.action_id}:observation_batch_validation",
        step=step,
        kind="action_validation",
        success=False,
        summary=detail,
        failure_class=failure_class,
        payload={
            "observation_batch_id": action.action_id,
            "model_requested_action": True,
            "solver_action_id": action.action_id,
            "solver_action_kind": "observe_batch",
        },
    )
