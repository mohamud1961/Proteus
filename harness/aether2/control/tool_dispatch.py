"""Tool-call dispatch loop for the Aether-2 control loop.

Pure extraction from loop.py — zero behaviour change.
"""

from __future__ import annotations

import json
from typing import Any

from harness.aether2.traces.envelope import ObservationEnvelope, build_envelope
from harness.aether2.traces.mirror import Mirror, MirrorNote, SemanticObservation
from harness.aether2.runtime.context import ContextManager
from harness.aether2.runtime.executor import ContainerExecutor
from harness.aether2.traces.receipts import ReceiptWriter
from harness.aether2.traces.delta import record_observation_evidence, record_terminal_claim, with_evidence_ledger

from harness.aether2.control.action_helpers import (
    _action_signature,
    _build_blind_retry_blocked_envelope,
    _envelope_failed,
    _envelope_to_message,
    _parse_tool_call_arguments,
    _tool_call_name,
)
from harness.aether2.control.completion import (
    _build_recovery_warning,
    _build_task_done_warning,
    _failure_class,
    _ledger_progress,
    _mutation_risk_from_tool_call,
    _semantic_action_family,
)
from harness.aether2.control.requirements import _current_evidence_ledger, _relevant_requirement
from harness.aether2.control.tail_helpers import _collect_established_facts, _unused_affordances

__all__ = [
    "_execute_tool_calls",
]


def _execute_tool_calls(
    *,
    response: Any,
    response_messages: list[dict[str, Any]],
    step: int,
    executor: ContainerExecutor,
    ctx: Any,
    context: ContextManager,
    receipts: ReceiptWriter,
    mirror: Mirror,
    tool_invocations: list[Any],
    mirror_notes: list[MirrorNote],
    failure_tracker: dict[str, Any],
    recoveries: int,
    no_delta_streaks: int,
    job_ids: list[str],
    session_ids: list[str],
    stated_requirements: list[str],
    plan_text: str | None,
    elapsed_sec: float,
    remaining_sec: float | None,
    model_request_index: int,
    repeat_policy: Any | None = None,
) -> tuple[dict[str, Any], int, int, list[str], tuple[dict[str, Any], ObservationEnvelope] | None]:
    """Iterate over tool_calls in a model response, dispatch each, and update state."""
    # Inline import to avoid circular dependency with execution_context.py
    from harness.aether2.control.execution_context import ToolInvocationRecord

    most_recent_checks: list[str] = []
    task_done_call: tuple[dict[str, Any], ObservationEnvelope] | None = None
    step_primary_action: dict[str, Any] | None = None

    for tool_call in getattr(response, "tool_calls", ()) or ():
        tool_name = _tool_call_name(tool_call)
        tool_call_id = tool_call.get("id")
        if tool_name is None:
            envelope = build_envelope(
                {
                    "tool": "unknown",
                    "exit_code": 1,
                    "duration_sec": 0.0,
                    "cwd": str(executor.workspace_root),
                    "stdout": "",
                    "stderr": "malformed tool call: missing name",
                    "error": {
                        "kind": "malformed_tool_call",
                        "message": "tool call is missing a name",
                        "reason_code": "malformed_tool_call",
                    },
                },
                raw_log_dir=ctx.raw_log_dir,
            )
            context.append_turn(_envelope_to_message("unknown", tool_call_id, envelope))
            continue

        arguments = _parse_tool_call_arguments(tool_call)
        proof_state = getattr(ctx, "proof_state", None)
        warned_signatures = getattr(ctx, "_warning_signatures", set())
        warning_signature = json.dumps({"tool": tool_name, "arguments": arguments}, sort_keys=True, ensure_ascii=True)
        task_done_warning = None
        mutation_warning = None
        if warning_signature not in warned_signatures:
            if tool_name == "task_done":
                task_done_warning = _build_task_done_warning(proof_state)
            else:
                mutation_warning = _mutation_risk_from_tool_call(tool_name, arguments, proof_state=proof_state)
            warning_payload = task_done_warning or mutation_warning
            if warning_payload is not None:
                warned_signatures = set(warned_signatures)
                warned_signatures.add(warning_signature)
                ctx._warning_signatures = warned_signatures
                if getattr(ctx, "receipt_store", None) is not None:
                    ctx.receipt_store.append(
                        "warning_event",
                        step,
                        warning_payload.get("message", "warning"),
                        warning_payload,
                    )
                context.append_turn(
                    {
                        "role": "system",
                        "content": json.dumps(
                            {"warning": warning_payload},
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                        ),
                    }
                )
                continue
        signature = _action_signature(tool_name, arguments)
        blind_retry = tool_name == "run_command" and failure_tracker.get("last_failure_signature") == signature
        permission_decision: dict[str, Any] | None = None
        hook_trace: list[dict[str, Any]] = []
        if blind_retry:
            envelope = _build_blind_retry_blocked_envelope(
                tool_name,
                arguments,
                str(executor.workspace_root),
                raw_log_dir=ctx.raw_log_dir,
                guidance=str(getattr(repeat_policy, "guidance", "") or ""),
            )
            failure_tracker = {"last_failure_signature": None, "streak": 0}
        else:
            try:
                outcome = ctx.tool_registry.invoke(
                    tool_name,
                    arguments,
                    ctx,
                    call_id=None if tool_call_id is None else str(tool_call_id),
                )
                envelope = outcome.envelope
                permission_decision = outcome.permission_decision
                hook_trace = outcome.hook_trace
            except Exception as exc:  # noqa: BLE001
                envelope = build_envelope(
                    {
                        "tool": tool_name,
                        "exit_code": 1,
                        "duration_sec": 0.0,
                        "cwd": str(executor.workspace_root),
                        "stdout": "",
                        "stderr": str(exc),
                        "error": {
                            "kind": "dispatch_error",
                            "message": str(exc),
                            "reason_code": "dispatch_error",
                            "tool_name": tool_name,
                        },
                    },
                    raw_log_dir=ctx.raw_log_dir,
                )

        failed = _envelope_failed(envelope)
        failure_class_before = str(failure_tracker.get("last_failure_class") or "") or None
        failure_class_after = _failure_class(envelope)
        if failed:
            prior_signature = failure_tracker.get("last_failure_signature")
            streak = failure_tracker.get("streak", 0) + 1 if prior_signature == signature else 1
            failure_tracker = {
                "last_failure_signature": signature,
                "last_failure_class": failure_class_after,
                "streak": streak,
            }
        else:
            if failure_tracker.get("last_failure_signature") is not None:
                recoveries += 1
            failure_tracker = {"last_failure_signature": None, "last_failure_class": None, "streak": 0}

        context.append_turn(_envelope_to_message(tool_name, tool_call_id, envelope))
        recovery_warning = _build_recovery_warning(tool_name, arguments, envelope)
        recovery_warning_signature = json.dumps(
            {"recovery_warning": recovery_warning, "tool": tool_name, "arguments": arguments},
            sort_keys=True,
            ensure_ascii=True,
        )
        if recovery_warning is not None and recovery_warning_signature not in warned_signatures:
            warned_signatures = set(warned_signatures)
            warned_signatures.add(recovery_warning_signature)
            ctx._warning_signatures = warned_signatures
            if getattr(ctx, "receipt_store", None) is not None:
                ctx.receipt_store.append(
                    "warning_event",
                    step,
                    recovery_warning.get("message", "warning"),
                    recovery_warning,
                )
            context.append_turn(
                {
                    "role": "system",
                    "content": json.dumps(
                        {"warning": recovery_warning},
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ),
                }
            )
        _record = ToolInvocationRecord(
            step=step,
            tool_name=tool_name,
            arguments=arguments,
            envelope=envelope,
            permission_decision=permission_decision,
            hook_trace=hook_trace,
        )
        tool_invocations.append(_record)
        ctx._run_tool_invocations.append(_record)
        receipts.record_step(
            len(tool_invocations),
            request={"messages_len": len(response_messages)},
            response={"text": getattr(response, "text", "")},
            action={
                "tool": tool_name,
                "arguments": arguments,
                "permission_decision": permission_decision,
                "hook_trace": hook_trace,
            },
            raw_output=_envelope_to_message(tool_name, tool_call_id, envelope),
        )

        if tool_name == "start_job":
            job_id_arg = arguments.get("job_id")
            stdout = envelope.stdout_head
            started_job_id = job_id_arg
            if started_job_id is None and "started job " in stdout:
                started_job_id = stdout.split("started job ", 1)[1].split(" ", 1)[0]
            if started_job_id and started_job_id not in job_ids:
                job_ids.append(started_job_id)
        if tool_name == "session_start":
            session_id_arg = arguments.get("session_id")
            if session_id_arg and session_id_arg not in session_ids:
                session_ids.append(session_id_arg)

        ledger_before = _current_evidence_ledger(context)
        artifact_paths = [
            item.path
            for item in envelope.files_changed
            if item.change_type in {"added", "modified"}
        ]
        primary_requirement = _relevant_requirement(
            ledger_before,
            stated_requirements,
            tool_name=tool_name,
            arguments=arguments,
            artifact_paths=artifact_paths,
        )
        observation_note: str | None = None
        if tool_name == "task_done":
            observation_note = str(arguments.get("summary", "")).strip() or "task completion claimed"
        elif tool_name != "run_command" and envelope.exit_code == 0:
            observation_note = f"{tool_name} completed"
        elif artifact_paths:
            observation_note = f"{tool_name} changed visible workspace state"

        updated_ledger = record_observation_evidence(
            ledger_before,
            requirement=primary_requirement,
            tool_name=tool_name,
            step=step,
            exit_code=envelope.exit_code,
            raw_log_path=envelope.raw_log_path,
            artifact_paths=artifact_paths,
            note=observation_note,
            failure_family=failure_class_after,
        )
        if tool_name in {"task_done", "task_blocked"}:
            updated_ledger = record_terminal_claim(
                updated_ledger,
                claim=arguments,
                outcome=tool_name,
                step=step,
                raw_log_path=envelope.raw_log_path,
            )
        ctx.last_snapshot = with_evidence_ledger(ctx.last_snapshot, updated_ledger)
        context.delta_state = ctx.last_snapshot
        requirement_advanced, stronger_evidence_added = _ledger_progress(ledger_before, updated_ledger)
        action_family, semantic_target, semantic_target_kind = _semantic_action_family(tool_name, arguments)
        result_summary = _step_result_summary(tool_name, envelope, failure_class_after)
        current_action_memory = {
            "signature": " ".join(
                part for part in [action_family, None if semantic_target is None else f"{semantic_target_kind or 'target'}={semantic_target}"] if part
            ),
            "result": result_summary,
        }
        if step_primary_action is None:
            step_primary_action = current_action_memory
        semantic_observation = SemanticObservation(
            action_family=action_family,
            target=semantic_target,
            target_kind=semantic_target_kind,
            failure_class_before=failure_class_before,
            failure_class_after=failure_class_after,
            requirement_advanced=requirement_advanced,
            stronger_evidence_added=stronger_evidence_added,
            artifact_evidence=tuple(artifact_paths),
            meaningful_artifact_change=bool(artifact_paths),
            legitimate_polling=bool(
                tool_name in {"job_status", "session_read", "wait"}
                and (
                    envelope.process_delta.job_log_growth
                    or envelope.process_delta.service_log_growth
                )
            ),
            bounded_retry=bool(tool_name == "wait"),
        )
        semantic_payload = (
            semantic_observation
            if (
                failure_class_after is not None
                or requirement_advanced
                or stronger_evidence_added
                or bool(artifact_paths)
                or tool_name in {"job_status", "session_read", "wait"}
            )
            else None
        )
        note = mirror.observe(
            signature,
            ctx.last_delta_report,
            semantic_observation=semantic_payload,
            established_facts=_collect_established_facts(context),
            unused_affordances=_unused_affordances(),
            fuel_gauge_text=(
                json.dumps(
                    {
                        "elapsed_sec": round(elapsed_sec, 3),
                        "remaining_sec": None if remaining_sec is None else round(remaining_sec, 3),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
            ),
        )
        if note is not None:
            mirror_notes.append(note)
            no_delta_streaks += 1
            context.append_turn({"role": "system", "content": f"[mirror_note]\n{note.text}"})

        if tool_name == "task_done":
            task_done_call = (arguments, envelope)
            most_recent_checks = [str(item) for item in arguments.get("checks", [])]

    ctx._step_primary_action = step_primary_action
    return failure_tracker, recoveries, no_delta_streaks, most_recent_checks, task_done_call


def _step_result_summary(tool_name: str, envelope: ObservationEnvelope, failure_class_after: str | None) -> str:
    if failure_class_after:
        return failure_class_after.replace("_", " ")
    if getattr(envelope, "exit_code", None) not in {None, 0}:
        return f"exit_code={envelope.exit_code}"
    for text in (
        getattr(envelope, "stdout_head", "") or "",
        getattr(envelope, "stderr_head", "") or "",
    ):
        compact = " ".join(str(text).split())
        if compact:
            return compact[:180]
    return f"{tool_name} completed without stronger evidence"
