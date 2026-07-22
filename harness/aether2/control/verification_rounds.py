"""Verification rounds execution logic for the continuous executor loop."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from harness.aether2.runtime.compactor import build_receipt_continuity_snapshot, rebase
from harness.aether2.runtime.context import ContextManager
from harness.aether2.traces.delta import (
    diff as delta_diff,
    mark_blockers_candidate_resolved,
    mark_blockers_exhausted,
    record_check_results,
    record_verifier_report,
    snapshot as delta_snapshot,
    with_evidence_ledger,
)
from harness.aether2.runtime.verify import replay_checks, verify_fresh_context
from harness.aether2.runtime.adaptive_profile_helpers import solver_visible_orientation
from harness.aether2.runtime.orientation import orient
from harness.aether2.control.requirements import (
    _current_evidence_ledger,
    _primary_requirement,
)
from harness.aether2.control.completion import (
    _build_completion_contract,
    _build_completion_evidence_gate_report,
    _build_operational_verification_feedback,
)
from harness.aether2.control.reasoning_trace import (
    _build_reasoning_trace_step,
    _response_cost,
    _response_usage,
)
from harness.aether2.control.verification_context import _ReadOnlyVerificationContext
from harness.aether2.control.runtime_support import (
    _env_contract_drift,
    _monitor_persistent_runtime,
)
from harness.aether2.control.tail_helpers import (
    _check_result_summary,
    _diff_to_dict,
    _model_requested_rebase,
    _sync_fact_ledger_state,
    _update_plan_text,
)
from harness.aether2.control.tool_dispatch import _execute_tool_calls
from harness.aether2.traces.redaction import _clean_hidden_refs

# Default to one verifier feedback packet that normal loop state can consume.
# A future config spine can override this via state["verification_round_limit"].
MAX_VERIFICATION_ROUNDS = 1


def _run_verification_rounds(
    task: Any,
    model_client: Any,
    executor: Any,
    ctx: Any,
    receipts: Any,
    mirror: Any,
    job_registry: Any,
    session_registry: Any,
    job_ids: list[str],
    session_ids: list[str],
    stated_requirements: Any,
    verifier_task_contract: Any,
    seen_artifacts: set[str],
    known_job_status: dict[str, tuple[bool, int | None]],
    tool_invocations: list[Any],
    mirror_notes: list[Any],
    discrepancy_reports: list[Any],
    reasoning_trace_steps: list[dict[str, Any]],
    step: int,
    deadline_ts: float,
    started_at: float,
    orientation_dict: dict[str, Any],
    active_tool_schemas: list[Any],
    state: dict[str, Any],
    verifier_policy: Any | None = None,
    completion_policy: Any | None = None,
    repeat_policy: Any | None = None,
) -> dict[str, Any]:
    """Execute the inner verification/repair loop."""
    verification_rounds = state["verification_rounds"]
    model_calls = state["model_calls"]
    tokens_cached = state["tokens_cached"]
    tokens_fresh = state["tokens_fresh"]
    total_cost = state["total_cost"]
    compaction_count = state["compaction_count"]
    recoveries = state["recoveries"]
    no_delta_streaks = state["no_delta_streaks"]
    finalize_pass = state["finalize_pass"]
    finalize_summary = state["finalize_summary"]
    plan_text = state["plan_text"]
    context = state["context"]
    failure_tracker = state["failure_tracker"]
    claim_checks = state["claim_checks"]
    finalize_reason = state["finalize_reason"]
    proof_state = state.get("proof_state")
    ctx.proof_state = proof_state
    verification_round_limit = max(1, int(state.get("verification_round_limit", MAX_VERIFICATION_ROUNDS)))
    feedback_only = bool(state.get("feedback_only", False))

    def record_exchange(
        call_idx: int,
        request_messages: list[dict[str, Any]],
        response: Any,
        *,
        tool_schemas: Any,
        call_role: str,
    ) -> None:
        receipts.record_model_exchange(
            call_idx,
            request_messages,
            response,
            tool_schemas=tool_schemas,
            call_role=call_role,
            tail_state=context.current_tail_payload(),
            ledger_state=_current_evidence_ledger(context),
            frozen_success_contract=context.current_frozen_success_contract_text(),
        )

    def make_exchange_recorder(counter: dict[str, int]):
        def _record(
            request_messages: list[dict[str, Any]],
            response: Any,
            tool_schemas: Any,
            *,
            call_role: str,
            **_: Any,
        ) -> None:
            call_idx = counter["next"]
            counter["next"] += 1
            record_exchange(
                call_idx,
                request_messages,
                response,
                tool_schemas=tool_schemas,
                call_role=call_role,
            )
        return _record

    def append_trace_step(
        *,
        step_index: int | None,
        model_call_idx: int,
        call_role: str,
        response: Any,
        visible_tail_state: Mapping[str, Any],
        completion_contract: Mapping[str, Any],
        pre_step_ledger: Mapping[str, Any],
        post_step_ledger: Mapping[str, Any],
        tool_invocations_for_step: list[Any],
        task_done_call: tuple[dict[str, Any], Any] | None,
        decision_kind: str,
        finalize_reason: str | None = None,
        verification_round_index: int | None = None,
        blocker_state: Mapping[str, Any] | None = None,
    ) -> None:
        reasoning_trace_steps.append(
            _build_reasoning_trace_step(
                step=step_index,
                model_call_idx=model_call_idx,
                call_role=call_role,
                response=response,
                input_digests=context.digest_snapshot(),
                visible_tail_state=visible_tail_state,
                completion_contract=completion_contract,
                pre_step_ledger=pre_step_ledger,
                post_step_ledger=post_step_ledger,
                tool_invocations=tool_invocations_for_step,
                task_done_call=task_done_call,
                decision_kind=decision_kind,
                plan_text=plan_text,
                model_exchange_ref=str(receipts.receipts_dir / f"model_exchange_{model_call_idx}.json"),
                verification_round_index=verification_round_index,
                blocker_state=blocker_state,
                finalize_reason=finalize_reason,
                frozen_success_contract=context.current_frozen_success_contract(),
            )
        )

    rounds = 0
    claim_summary = finalize_summary
    while rounds < verification_round_limit:
        rounds += 1
        verification_rounds += 1
        check_results = replay_checks(claim_checks, executor) if claim_checks else []
        verification_ledger = _current_evidence_ledger(context)
        successful_check_summaries = [
            _check_result_summary(result)
            for result in check_results
            if getattr(result, "exit_code", None) == 0 and not bool(getattr(result, "timed_out", False))
        ]
        if check_results:
            verification_ledger = record_check_results(
                verification_ledger,
                requirement=_primary_requirement(verification_ledger, stated_requirements),
                check_results=check_results,
                step=step,
                raw_log_path=None,
            )
        curr_snapshot = delta_snapshot(task.workspace_root)
        workspace_diff = delta_diff(ctx.last_snapshot, curr_snapshot)
        relevant_artifact_paths = [*workspace_diff.added_paths, *workspace_diff.modified_paths]
        verification_ledger = mark_blockers_candidate_resolved(
            verification_ledger,
            step=step,
            relevant_failed_checks=successful_check_summaries,
            relevant_artifact_paths=relevant_artifact_paths,
        )
        curr_snapshot = with_evidence_ledger(curr_snapshot, verification_ledger)
        service_monitoring, monitored_snapshot = _monitor_persistent_runtime(
            ctx=ctx,
            job_registry=job_registry,
            session_registry=session_registry,
            job_ids=job_ids,
            session_ids=session_ids,
            claim_checks=claim_checks,
            check_results=check_results,
            remaining_sec=deadline_ts - time.monotonic(),
            start_snapshot=curr_snapshot,
        )
        curr_snapshot = monitored_snapshot
        workspace_diff = delta_diff(ctx.last_snapshot, curr_snapshot)
        relevant_artifact_paths = [*workspace_diff.added_paths, *workspace_diff.modified_paths]
        ctx.last_snapshot = curr_snapshot
        context.delta_state = curr_snapshot
        verification_orientation_dict = orient(executor).as_dict()

        action_digest = {
            "environment_contract": _env_contract_drift(orientation_dict, verification_orientation_dict),
            "service_monitoring": service_monitoring,
            "tool_calls": [
                {"step": record.step, "tool": record.tool_name, "arguments": record.arguments}
                for record in tool_invocations[-20:]
            ]
        }
        completion_gate_report = _build_completion_evidence_gate_report(
            verification_ledger,
            stated_requirements=stated_requirements,
            finalize_reason=finalize_reason,
            check_results=check_results,
            action_digest=action_digest,
            completion_policy=completion_policy,
            proof_state=proof_state,
        )
        if completion_gate_report is not None:
            action_digest["completion_runtime_floor"] = {
                "status": "evidence_floor_warning",
                "summary": completion_gate_report.summary,
                "reason_codes": list(completion_gate_report.reason_codes),
                "requirements": [item.__dict__ for item in completion_gate_report.requirements],
            }
        verifier_counter = {"next": model_calls + 1}
        discrepancy_report = verify_fresh_context(
            verifier_task_contract,
            solver_visible_orientation(orientation_dict),
            _diff_to_dict(workspace_diff),
            {"summary": claim_summary, "trigger": finalize_reason},
            check_results,
            action_digest,
            model_client,
            inspection_ctx=_ReadOnlyVerificationContext(ctx, receipts),
            record_exchange=make_exchange_recorder(verifier_counter),
            stated_requirements=stated_requirements,
            verifier_system_prompt=str(getattr(verifier_policy, "system_prompt", "") or ""),
            verifier_focus=list(getattr(verifier_policy, "focus", ()) or ()),
            verifier_do_not_assume=list(getattr(verifier_policy, "do_not_assume", ()) or ()),
            required_final_evidence=list(getattr(verifier_policy, "required_final_evidence", ()) or ()),
        )
        model_calls = verifier_counter["next"] - 1
        updated_ledger = record_verifier_report(
            _current_evidence_ledger(context),
            report=discrepancy_report,
            verifier_ref=f"verification_round={verification_rounds}",
            step=step,
            exhaustion_round_limit=verification_round_limit - 1,
        )
        if discrepancy_report.has_discrepancies and rounds >= verification_round_limit:
            updated_ledger = mark_blockers_exhausted(
                updated_ledger,
                step=step,
                exhaustion_round_limit=verification_round_limit - 1,
                force=True,
            )
        discrepancy_reports.append(discrepancy_report)
        ctx.last_snapshot = with_evidence_ledger(ctx.last_snapshot, updated_ledger)
        context.delta_state = ctx.last_snapshot

        remaining_sec = deadline_ts - time.monotonic()
        if not discrepancy_report.has_discrepancies or remaining_sec <= 0:
            finalize_pass = not discrepancy_report.has_discrepancies
            finalize_summary = claim_summary if finalize_pass else discrepancy_report.summary
            break

        report_message = {
            "role": "system",
            "content": json.dumps(
                _clean_hidden_refs(
                    {
                        "verification_blocker": discrepancy_report.summary,
                        "next_step_instruction": (
                            "The task is not complete. Continue the normal solving loop now: "
                            "use concrete tool calls to address the unsatisfied requirements, "
                            "or call task_blocked with evidence, attempts, missing external "
                            "state, and recommended next evidence if the blocker is genuinely "
                            "external. Do not merely acknowledge this verifier feedback."
                        ),
                        "verifier_continuation_state": (
                            _build_operational_verification_feedback(
                                discrepancy_report,
                                proof_state=proof_state,
                            )
                        ),
                        "verification_report": {
                            "requirements": [item.__dict__ for item in discrepancy_report.requirements],
                            "reason_codes": list(discrepancy_report.reason_codes),
                            "summary": discrepancy_report.summary,
                        },
                        "checks_results": [result.__dict__ for result in check_results],
                        "time_remaining_sec": remaining_sec,
                    }
                ),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        }
        context.append_turn(report_message)

        if feedback_only:
            state.update({
                "verification_rounds": verification_rounds,
                "model_calls": model_calls,
                "tokens_cached": tokens_cached,
                "tokens_fresh": tokens_fresh,
                "total_cost": total_cost,
                "compaction_count": compaction_count,
                "recoveries": recoveries,
                "no_delta_streaks": no_delta_streaks,
                "finalize_pass": False,
                "finalize_summary": discrepancy_report.summary,
                "plan_text": plan_text,
                "context": context,
                "failure_tracker": failure_tracker,
                "claim_checks": claim_checks,
                "finalize_reason": None,
                "verifier_feedback_pending": True,
            })
            return state

        messages = [*context.message_history()]
        repair_pre_ledger = _current_evidence_ledger(context)
        repair_completion_contract = _build_completion_contract(
            verifier_task_contract,
            repair_pre_ledger,
            completion_policy=completion_policy,
        )
        repair_tail_state = context.current_tail_payload()
        response = model_client.call(messages, active_tool_schemas, cache_prefix_len=context.prefix.token_estimate)
        model_calls += 1
        record_exchange(model_calls, messages, response, tool_schemas=active_tool_schemas, call_role="repair")
        usage = _response_usage(response)
        tokens_cached += int(usage.get("cached_input_tokens", 0))
        tokens_fresh += int(usage.get("fresh_input_tokens", 0))
        total_cost += _response_cost(response)

        assistant_message = {"role": "assistant", "content": response.text}
        if response.tool_calls:
            assistant_message["tool_calls"] = [dict(tool_call) for tool_call in response.tool_calls]
        context.append_turn(assistant_message)

        claim_summary = response.text
        if _model_requested_rebase(response.text, response.tool_calls):
            append_trace_step(
                step_index=step,
                model_call_idx=model_calls,
                call_role="repair",
                response=response,
                visible_tail_state=repair_tail_state,
                completion_contract=repair_completion_contract,
                pre_step_ledger=repair_pre_ledger,
                post_step_ledger=_current_evidence_ledger(context),
                tool_invocations_for_step=[],
                task_done_call=None,
                decision_kind="repair_rebase_request",
                verification_round_index=rounds,
                blocker_state={
                    "verification_summary": discrepancy_report.summary,
                    "reason_codes": list(discrepancy_report.reason_codes),
                },
            )
            _sync_fact_ledger_state(context, ctx)
            compaction_counter = {"next": model_calls + 1}
            receipt_snapshot = None
            if getattr(ctx, "receipt_store", None) is not None and getattr(ctx, "receipt_context_pack_policy", None) is not None:
                local_tools = getattr(ctx, "task_local_tools", None)
                receipt_snapshot = build_receipt_continuity_snapshot(
                    ctx.receipt_store,
                    ctx.receipt_context_pack_policy,
                    local_tools=None if local_tools is None else local_tools.summary(),
                    proof_state=proof_state if isinstance(proof_state, Mapping) else None,
                )
            context = rebase(
                context,
                model_client,
                record_exchange=make_exchange_recorder(compaction_counter),
                receipt_continuity_snapshot=receipt_snapshot,
            )
            compaction_count += 1
            model_calls = compaction_counter["next"] - 1
            continue

        repair_step_tool_invocation_start = len(tool_invocations)
        previous_claim_checks = list(claim_checks)
        failure_tracker, recoveries, no_delta_streaks, new_checks, new_task_done = _execute_tool_calls(
            response=response,
            response_messages=messages,
            step=step,
            executor=executor,
            ctx=ctx,
            context=context,
            receipts=receipts,
            mirror=mirror,
            tool_invocations=tool_invocations,
            mirror_notes=mirror_notes,
            failure_tracker=failure_tracker,
            recoveries=recoveries,
            no_delta_streaks=no_delta_streaks,
            job_ids=job_ids,
            session_ids=session_ids,
            stated_requirements=stated_requirements,
            plan_text=plan_text,
            elapsed_sec=time.monotonic() - started_at,
            remaining_sec=remaining_sec,
            model_request_index=model_calls,
            repeat_policy=repeat_policy,
        )
        repair_step_tool_invocations = tool_invocations[repair_step_tool_invocation_start:]
        if new_task_done is not None:
            claim_summary = str(new_task_done[0].get("summary", claim_summary))
        if new_checks:
            claim_checks = new_checks
        append_trace_step(
            step_index=step,
            model_call_idx=model_calls,
            call_role="repair",
            response=response,
            visible_tail_state=repair_tail_state,
            completion_contract=repair_completion_contract,
            pre_step_ledger=repair_pre_ledger,
            post_step_ledger=_current_evidence_ledger(context),
            tool_invocations_for_step=repair_step_tool_invocations,
            task_done_call=new_task_done,
            decision_kind="repair_task_done" if new_task_done is not None else ("repair_tool_calls" if response.tool_calls else "repair_implicit_stop"),
            verification_round_index=rounds,
            blocker_state={
                "verification_summary": discrepancy_report.summary,
                "reason_codes": list(discrepancy_report.reason_codes),
                "previous_checks": previous_claim_checks,
            },
            finalize_reason=finalize_reason if new_task_done is not None else None,
        )
        if new_task_done is None and not response.tool_calls:
            # implicit stop during a verification round: treat as resubmission with no new checks
            continue

    state.update({
        "verification_rounds": verification_rounds,
        "model_calls": model_calls,
        "tokens_cached": tokens_cached,
        "tokens_fresh": tokens_fresh,
        "total_cost": total_cost,
        "compaction_count": compaction_count,
        "recoveries": recoveries,
        "no_delta_streaks": no_delta_streaks,
        "finalize_pass": finalize_pass,
        "finalize_summary": finalize_summary,
        "plan_text": plan_text,
        "context": context,
        "failure_tracker": failure_tracker,
        "claim_checks": claim_checks,
        "finalize_reason": finalize_reason,
    })
    return state
