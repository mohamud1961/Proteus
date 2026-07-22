"""Act/submit turn execution, extracted from kernel.py for the 500-LOC cap.

``kernel`` is the AetherNextKernel instance supplying guards, engines, and
the completion gate; the gate decision is stored on the kernel as before.
"""
from __future__ import annotations

import csv
import json
from typing import Any, TYPE_CHECKING

from .automatic_memory import automatic_memory_receipt
from .execution import Executor
from .kernel_actions import handle_kernel_owned_action
from .kernel_checks import probe_checks
from .kernel_dispatch import dispatch_action
from .ledger import ExecutionLedger, Receipt
from .no_progress import NoProgressController
from .observation_batch import execute_observation_batch, mutation_generation
from .runtime_ir import CompiledRuntime, EnvMap, SolverTurn, normalize_relpath
from .world import WorldState, WorldStateDeltaError

if TYPE_CHECKING:
    from .tracing import RunTrace


def run_act_turn(kernel: Any, turn: SolverTurn, step: int, compiled: CompiledRuntime,
                 executor: Executor, envmap: EnvMap, ledger: ExecutionLedger,
                 world_state: WorldState | None = None) -> None:
    """Execute an act turn: validate, guard, dispatch, probe."""
    step_receipts: list[Receipt] = []

    def _record(r: Receipt) -> None:
        ledger.record(r)
        step_receipts.append(r)

    # SolverTurn.validate already guarantees one action frontier.  Persist the
    # compact decision before execution so intent and the exact next
    # observation remain causally paired.
    action = turn.actions[0]
    _record(Receipt(
        receipt_id=f"step-{step}:{action.action_id}:solver_decision_state",
        step=step,
        kind="solver_decision_state",
        success=True,
        summary=turn.summary,
        payload={
            "current_subgoal": turn.summary,
            "evidence_gap": turn.evidence_gap,
            "action_id": action.action_id,
            "action_kind": action.kind,
            "intent": action.intent,
            "expected_observation": action.expected_observation,
            "if_fail_next": action.if_fail_next,
            "mutation_generation": mutation_generation(ledger),
        },
    ))

    for action in turn.actions:
        action_errors = action.validate(compiled.action_schema)
        if action_errors:
            _record(Receipt(
                receipt_id=f"step-{step}:{action.action_id}:validation", step=step,
                kind="action_validation", success=False,
                summary=f"invalid action: {'; '.join(action_errors)}",
                failure_class="action_validation",
            ))
            _record(ledger.record_accounting(
                receipt_id=f"step-{step}:{action.action_id}:refused_invalid",
                step=step, counter="solver_refused_actions",
                event="action_validation_failed", action_id=action.action_id,
            ))
            continue
        safety_violation = kernel.safety_guard.violation(compiled, action, network_scope=envmap.network_scope)
        if safety_violation:
            _record(Receipt(
                receipt_id=f"step-{step}:{action.action_id}:safety", step=step,
                kind="safety_block", success=False,
                summary=safety_violation, failure_class="safety_violation",
            ))
            _record(ledger.record_accounting(
                receipt_id=f"step-{step}:{action.action_id}:refused_safety",
                step=step, counter="solver_refused_actions",
                event="safety_violation", action_id=action.action_id,
            ))
            continue
        if action.kind == "write_file":
            path = str(action.arguments.get("path", "")).strip()
            if path:
                norm = normalize_relpath(path, envmap.workspace_root)
                violation = kernel.integrity_guards.explain_path_violation(
                    compiled.objective_graph, norm,
                )
                if violation:
                    _record(Receipt(
                        receipt_id=f"step-{step}:{action.action_id}:integrity",
                        step=step, kind="integrity_block", success=False,
                        summary=violation, failure_class="integrity_violation",
                        payload={"integrity_violation": violation},
                    ))
                    _record(ledger.record_accounting(
                        receipt_id=f"step-{step}:{action.action_id}:refused_integrity",
                        step=step, counter="solver_refused_actions",
                        event="integrity_violation", action_id=action.action_id,
                    ))
                    continue
        if (
            kernel.max_accepted_task_actions is not None
            and ledger.accounting_value("solver_accepted_task_actions")
            >= kernel.max_accepted_task_actions
        ):
            _record(Receipt(
                receipt_id=f"step-{step}:{action.action_id}:action_budget",
                step=step,
                kind="action_budget_refused",
                success=False,
                summary=(
                    f"accepted task-action limit reached: {kernel.max_accepted_task_actions}; "
                    "action was not dispatched"
                ),
                failure_class="accepted_action_budget_exhausted",
                payload={"action_id": action.action_id, "action_kind": action.kind},
            ))
            _record(ledger.record_accounting(
                receipt_id=f"step-{step}:{action.action_id}:refused_action",
                step=step,
                counter="solver_refused_actions",
                event="accepted_action_budget_exhausted",
                action_id=action.action_id,
            ))
            continue
        _record(ledger.record_accounting(
            receipt_id=f"step-{step}:{action.action_id}:accepted_action",
            step=step,
            counter="solver_accepted_task_actions",
            event="accepted_for_dispatch",
            action_id=action.action_id,
            detail=f"accepted solver action {action.kind}",
        ))
        if compiled.automatic_memory_policy.mode != "off":
            automatic = automatic_memory_receipt(
                action, step=step, envmap=envmap, ledger=ledger,
            )
            if automatic is not None:
                _record(automatic)
                block_reason = _automatic_memory_block_reason(compiled, automatic)
                if block_reason:
                    _record(Receipt(
                        receipt_id=f"step-{step}:{action.action_id}:automatic_memory_advisory",
                        step=step,
                        kind="automatic_memory_advisory",
                        success=True,
                        summary=block_reason,
                        failure_class="",
                        payload={
                            "source_receipt_id": automatic.receipt_id,
                            "policy": compiled.automatic_memory_policy.mode,
                            "target": automatic.payload.get("target"),
                            "authority": "advisory_only",
                        },
                    ))
        no_progress = kernel.no_progress_controller.evaluate(action, ledger)
        if no_progress is not None:
            _record(NoProgressController.receipt(no_progress, step=step, action_id=action.action_id))
        if action.kind == "observe_batch":
            for receipt in execute_observation_batch(
                kernel,
                action,
                step=step,
                compiled=compiled,
                executor=executor,
                envmap=envmap,
                ledger=ledger,
            ):
                _record(receipt)
            continue
        handled = handle_kernel_owned_action(action, step, compiled, executor, envmap, ledger)
        if handled is not None:
            _record(handled)
            _update_world_from_receipt(world_state, handled, step=step, ledger=ledger)
            continue
        for receipt in dispatch_action(kernel, action, step, compiled, executor, envmap, ledger):
            _record(receipt)
            _update_world_from_receipt(world_state, receipt, step=step, ledger=ledger)

    action_results = [
        receipt for receipt in step_receipts
        if receipt.kind not in {
            "runtime_accounting", "solver_decision_state", "automatic_memory",
            "automatic_memory_advisory", "no_progress_control",
        }
    ]
    if any(receipt.state_change for receipt in action_results):
        progress = "state_changed"
    elif any(receipt.success for receipt in action_results):
        progress = "new_evidence"
    else:
        progress = "no_relevant_progress"
    _record(Receipt(
        receipt_id=f"step-{step}:{action.action_id}:solver_progress_assessment",
        step=step,
        kind="solver_progress_assessment",
        success=progress != "no_relevant_progress",
        summary=f"deterministic progress classification: {progress}",
        failure_class="" if progress != "no_relevant_progress" else progress,
        payload={
            "classification": progress,
            "action_id": action.action_id,
            "action_kind": action.kind,
            "result_receipt_ids": [receipt.receipt_id for receipt in action_results],
            "mutation_generation": mutation_generation(ledger),
        },
    ))
    probe_checks(step, compiled, executor, envmap, ledger, tuple(step_receipts))


def _update_world_from_receipt(
    world_state: WorldState | None,
    receipt: Receipt,
    *,
    step: int,
    ledger: ExecutionLedger,
) -> None:
    """Project compact, receipt-backed state into the verifier WorldState.

    The verifier must see current state, but never needs raw command output.
    Keep only typed hashes/handles/status fields and treat malformed projection
    as a harness-owned receipt rather than mutating state partially.
    """
    if world_state is None:
        return
    payload = receipt.payload if isinstance(receipt.payload, dict) else {}
    delta: dict[str, Any] = {}
    path = str(payload.get("path", "") or "").strip()
    if receipt.kind in {"write_file", "read_file", "read_file_page"} and path:
        delta["files"] = {
            path: {
                "status": "modified" if receipt.kind == "write_file" else "present",
                "step": step,
                "bytes": payload.get("bytes", payload.get("before_bytes", 0)),
                "sha256": payload.get("after_content_hash", payload.get("content_hash", "")),
                "handle": payload.get("file_handle", f"file:{path}"),
            }
        }
    elif receipt.kind in {"run_command", "check_result", "schema_validation"}:
        delta["latest_result"] = {
            "status": "passed" if receipt.success else "failed",
            "step": step,
            "kind": receipt.kind,
            "returncode": payload.get("exit_code", 0 if receipt.success else 1),
            "stdout_handle": payload.get("stdout_handle", ""),
            "stderr_handle": payload.get("stderr_handle", ""),
            "sha256": payload.get("content_hash", ""),
        }
    elif receipt.kind in {"process_launch", "process_stop", "service_probe"}:
        # Production process receipts use ``service_name`` / ``process_id``.
        # Accept older aliases only as a read-side compatibility projection;
        # the executor remains the sole owner of process creation/teardown.
        identifier = str(
            payload.get("service_name")
            or payload.get("process_id")
            or payload.get("service")
            or payload.get("name")
            or payload.get("process")
            or ""
        ).strip()
        if identifier:
            if receipt.kind == "process_stop":
                state = "stopped"
            elif receipt.kind == "process_launch":
                state = "running" if receipt.success else "failed"
            else:
                state = "ready" if receipt.success else "not_ready"
            delta["services"] = {identifier: {
                "state": state,
                "step": step,
                "pid": payload.get("pid"),
                "port": payload.get("port"),
                "readiness": payload.get("readiness", receipt.success if receipt.kind == "service_probe" else None),
            }}
    if not delta:
        return
    try:
        world_state.apply_delta(delta, step=step)
    except WorldStateDeltaError as exc:
        ledger.record(Receipt(
            receipt_id=f"step-{step}:world_state_projection:{receipt.receipt_id}",
            step=step,
            kind="world_state_projection_failed",
            success=False,
            summary=f"world-state projection rejected receipt {receipt.receipt_id}: {exc}",
            failure_class="harness_state_projection",
            payload={"source_receipt_id": receipt.receipt_id},
        ))


def _automatic_memory_block_reason(compiled: CompiledRuntime, receipt: Receipt) -> str:
    mode = compiled.automatic_memory_policy.mode
    if mode not in {"require_justification", "soft_block_exact_repeat"}:
        return ""
    payload = receipt.payload or {}
    if bool(payload.get("repeat_justified")):
        return ""
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    action_kind = str(payload.get("action_kind", ""))
    exact_repeat = bool(payload.get("same_content_hash")) or action_kind in {"run_command", "run_check"}
    if mode == "require_justification":
        return (
            "automatic memory requires repeat_justification before repeating "
            f"{action_kind} on {target.get('key', 'this target')}"
        )
    if mode == "soft_block_exact_repeat" and exact_repeat:
        return (
            "automatic memory soft-blocked an exact repeated action without new state or repeat_justification: "
            f"{action_kind} on {target.get('key', 'this target')}"
        )
    return ""

def run_submit_turn(
    kernel: Any,
    step: int,
    compiled: CompiledRuntime,
    executor: Executor,
    envmap: EnvMap,
    ledger: ExecutionLedger,
    trace: "RunTrace | None",
) -> None:
    """Execute submit_outcome logic; stores gate decision on the kernel."""
    for check in compiled.planned_checks():
        if not _check_inputs_changed(ledger, check.check_id):
            # Nothing in the workspace changed since this check last ran; the
            # prior outcome (still held by the ledger's check store) stands.
            # Re-executing identical checks burned hundreds of receipts live.
            ledger.record(Receipt(
                receipt_id=f"step-{step}:check_skipped:{check.check_id}", step=step,
                kind="check_skipped_unchanged", success=True,
                summary=f"check {check.label} not re-run: no state change since last execution",
                payload={"check_id": check.check_id, "command": check.command},
            ))
            continue
        result = executor.run_command(check.command, cwd=envmap.workspace_root)
        failure_class = kernel.failure_parser.classify(
            result.stdout + "\n" + result.stderr, exit_code=result.exit_code,
        ) if not result.success else ""
        ledger.record(Receipt(
            receipt_id=f"step-{step}:check:{check.check_id}", step=step,
            kind="check_result", success=result.success,
            summary=f"check {check.label}: exit={result.exit_code}",
            state_change=result.success, failure_class=failure_class,
            payload={
                "check_id": check.check_id, "command": check.command,
                "passed": result.success, "origin": check.origin,
                "detail": (result.stderr or result.stdout)[:500],
            },
        ))
    if compiled.objective_graph.output_schema and compiled.objective_graph.output_schema_target:
        target_path = compiled.objective_graph.output_schema_target
        try:
            content = executor.read_file(target_path)
            schema = dict(compiled.objective_graph.output_schema)
            if target_path.lower().endswith(".csv"):
                reader = csv.DictReader(content.splitlines())
                fields = reader.fieldnames or []
                missing_keys = [k for k in schema if k not in fields]
            else:
                parsed = json.loads(content)
                missing_keys = [k for k in schema if k not in parsed]
            valid = not missing_keys
            detail = "" if valid else f"missing keys: {', '.join(missing_keys)}"
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as exc:
            valid = False
            detail = str(exc)
        ledger.record(Receipt(
            receipt_id=f"step-{step}:schema_validation", step=step,
            kind="schema_validation", success=valid,
            summary=f"schema validation: {'pass' if valid else detail}",
            failure_class="" if valid else "schema_mismatch",
        ))
    alerts = kernel.monitor_runner.run(compiled, ledger)
    decision = kernel.completion_gate.evaluate(compiled, ledger, alerts)
    if trace is not None:
        trace.add_gate(step, decision)
    kernel._last_gate_decision = decision

def _check_inputs_changed(ledger: ExecutionLedger, check_id: str) -> bool:
    """True when the check has never run, or any state change happened after
    its most recent execution."""
    receipts = ledger.all_receipts()
    last_index = -1
    for index, receipt in enumerate(receipts):
        if receipt.kind == "check_result" and (receipt.payload or {}).get("check_id") == check_id:
            last_index = index
    if last_index < 0:
        return True
    return any(r.state_change for r in receipts[last_index + 1:])
