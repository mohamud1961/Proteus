"""Event pairing and reasoning trace parsing for decision trace extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from harness.aether2.traces.dt_row_loading import (
    _resolved,
)
from harness.aether2.traces.dt_receipts import (
    summarize_text,
    _short_json,
    _dedupe_dicts,
)
from harness.aether2.traces.dt_observation_summarize import (
    _classify_action_kind,
    _summarize_embedded_observation,
    _summarize_route_observation,
    _summarize_route_receipt,
    _summarize_embedded_invocation,
    _extract_command_from_tool_invocation,
    _extract_command_from_route_details,
)

_SEED_EVENT_TYPES = {"oriented", "sandbox_started"}
_REASONING_TRACE_TERMINAL_KINDS = {
    "implicit_stop",
    "task_done",
    "verification_requested",
    "closing",
    "repair_task_done",
    "repair_implicit_stop",
    "repair_rebase_request",
}


def _pair_embedded_invocations(
    invocations: list[dict[str, Any]],
    *,
    provenance: dict[str, Any],
    row_gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    receipt_bundle = [_summarize_embedded_invocation(inv, source_ref=provenance["source_row_ref"]) for inv in invocations]
    events: list[dict[str, Any]] = []
    previous_observation: dict[str, Any] = {"status": "start_of_run", "note": "no prior observation recorded"}
    last_index = len(invocations) - 1
    for index, inv in enumerate(invocations):
        envelope = inv.get("envelope") if isinstance(inv.get("envelope"), dict) else {}
        command = _extract_command_from_tool_invocation(inv)
        observation = _summarize_embedded_observation(envelope)
        tool_name = inv.get("tool_name") or inv.get("tool") or ""
        event = {
            "step": inv.get("step", index),
            "tool_name": tool_name,
            "visible_action": summarize_text(command, limit=220),
            "preceding_observation": previous_observation,
            "resulting_observation": observation,
            "evidence_classification": {
                "mode": "embedded_tool_invocation",
                "action_kind": _classify_action_kind(command, tool_name if isinstance(tool_name, str) else None),
                "signal_scope": "visible" if envelope.get("exit_code") == 0 else "gap",
                "result_kind": "success" if envelope.get("exit_code") == 0 else "nonzero_exit",
                "result_exit_code": envelope.get("exit_code"),
                "non_cot": True,
            },
            "unresolved_verifier_gaps": row_gaps if index == last_index else [],
            "source_provenance": provenance,
            "receipt_refs": [f"{provenance['source_row_ref']}#tool_invocation:{inv.get('step', index)}"],
        }
        events.append(event)
        previous_observation = observation
    return receipt_bundle, events


def _find_route_result_for_step(route_events: list[dict[str, Any]], *, step: Any, tool_call_id: str | None) -> dict[str, Any] | None:
    for event in route_events:
        if event.get("event_type") != "raw_bash_result":
            continue
        details = event.get("payload", {}).get("details", {})
        if not isinstance(details, dict):
            continue
        if details.get("step") != step:
            continue
        raw_payload = details.get("raw_payload")
        if tool_call_id and isinstance(raw_payload, dict):
            raw_id = raw_payload.get("id")
            if isinstance(raw_id, str) and raw_id and raw_id != tool_call_id:
                continue
        return event
    return None


def _route_seed_observation(route_receipts: list[dict[str, Any]]) -> dict[str, Any]:
    seed: dict[str, Any] | None = None
    for receipt in route_receipts:
        event_type = receipt.get("event_type")
        if event_type in _SEED_EVENT_TYPES:
            seed = {
                "receipt_ref": receipt.get("receipt_ref"),
                "event_type": event_type,
                "phase": receipt.get("phase"),
                "observation": receipt.get("observation", {}),
            }
            continue
        break
    return seed or {"status": "start_of_run", "note": "no seed observation receipts recorded"}


def _pair_route_trace_events(
    raw_events: list[dict[str, Any]],
    *,
    source_path: Path,
    provenance: dict[str, Any],
    row_gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    receipt_bundle = [_summarize_route_receipt(event, source_path=source_path) for event in raw_events]
    seed_observation = _route_seed_observation(receipt_bundle)

    completions_by_step: dict[Any, list[dict[str, Any]]] = {}
    for event in raw_events:
        if event.get("event_type") != "model_completion":
            continue
        details = event.get("payload", {}).get("details", {})
        if not isinstance(details, dict):
            continue
        step = details.get("step")
        completions_by_step.setdefault(step, []).append(event)

    primary_events: list[dict[str, Any]] = []
    previous_observation = seed_observation
    ordered_steps = sorted(
        completions_by_step.keys(),
        key=lambda item: (item is None, item if isinstance(item, (int, float, str)) else str(item)),
    )
    for step in ordered_steps:
        for completion in completions_by_step.get(step, []):
            details = completion.get("payload", {}).get("details", {})
            if not isinstance(details, dict):
                continue
            tool_calls = details.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                continue
            for tool_call_index, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict):
                    continue
                arguments = tool_call.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = {}
                command = arguments.get("command") if isinstance(arguments.get("command"), str) else ""
                tool_name = tool_call.get("name") if isinstance(tool_call.get("name"), str) else ""
                tool_call_id = tool_call.get("id") if isinstance(tool_call.get("id"), str) else None
                result_event = _find_route_result_for_step(raw_events, step=step, tool_call_id=tool_call_id)
                if result_event is None:
                    result_observation: dict[str, Any] = {
                        "status": "missing_result_receipt",
                        "step": step,
                        "tool_call_id": tool_call_id,
                    }
                    result_ref = None
                else:
                    result_details = result_event.get("payload", {}).get("details", {})
                    if not isinstance(result_details, dict):
                        result_details = {}
                    result_observation = _summarize_route_observation(result_details)
                    result_ref = f"{_resolved(source_path)}#seq={result_event.get('seq')}"
                event = {
                    "step": step,
                    "tool_call_index": tool_call_index,
                    "tool_name": tool_name,
                    "visible_action": summarize_text(command, limit=220),
                    "preceding_observation": previous_observation,
                    "resulting_observation": result_observation,
                    "evidence_classification": {
                        "mode": "route_trace",
                        "action_kind": _classify_action_kind(command, tool_name),
                        "signal_scope": result_observation.get("signal_attribution_scope", "unknown")
                        if isinstance(result_observation, dict)
                        else "unknown",
                        "result_kind": result_observation.get("result_class") or result_observation.get("status") or "unknown",
                        "result_reason_code": result_observation.get("reason_code"),
                        "result_exit_code": result_observation.get("exit_code"),
                        "non_cot": True,
                    },
                    "source_provenance": provenance,
                    "receipt_refs": [f"{_resolved(source_path)}#seq={completion.get('seq')}"] + ([result_ref] if result_ref else []),
                }
                primary_events.append(event)
                previous_observation = result_observation

    if primary_events:
        primary_events[-1]["unresolved_verifier_gaps"] = row_gaps
    return receipt_bundle, primary_events


def _extract_reasoning_trace_events(
    trace_payload: dict[str, Any] | None,
    *,
    trace_path: Path,
    provenance: dict[str, Any],
    row_gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if not isinstance(trace_payload, dict):
        return [], [], [f"{trace_path}: expected a JSON object reasoning trace payload"]

    steps = trace_payload.get("steps")
    if not isinstance(steps, list):
        return [], [], [f"{trace_path}: reasoning trace missing steps list"]

    receipt_bundle: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    parse_issues: list[str] = []
    previous_observation: dict[str, Any] = {"status": "start_of_run", "note": "no prior observation recorded"}

    for index, step_payload in enumerate(steps):
        if not isinstance(step_payload, dict):
            parse_issues.append(f"{trace_path}: steps[{index}] is not an object")
            continue

        step_no = step_payload.get("step", index + 1)
        visible_context = step_payload.get("visible_context")
        if not isinstance(visible_context, dict):
            visible_context = {}
            parse_issues.append(f"{trace_path}: steps[{index}] missing visible_context object")

        model_exchange_ref = visible_context.get("model_exchange_ref")
        if not isinstance(model_exchange_ref, str) or not model_exchange_ref:
            parse_issues.append(f"{trace_path}: steps[{index}] missing model_exchange_ref")
        else:
            receipt_bundle.append(
                {
                    "receipt_ref": model_exchange_ref,
                    "receipt_kind": "reasoning_trace_model_exchange_ref",
                    "receipt_name": Path(model_exchange_ref).name,
                    "step": step_no,
                    "call_role": step_payload.get("call_role"),
                }
            )

        tool_calls = step_payload.get("tool_calls")
        if not isinstance(tool_calls, list):
            parse_issues.append(f"{trace_path}: steps[{index}] missing tool_calls list")
            tool_calls = []

        if not tool_calls:
            result_observation = {
                "status": step_payload.get("decision_kind") or "no_tool_calls",
                "finalize_reason": step_payload.get("finalize_reason"),
            }
            if step_payload.get("decision_kind") in _REASONING_TRACE_TERMINAL_KINDS:
                events.append(
                    {
                        "step": step_no,
                        "tool_call_index": None,
                        "tool_name": str(step_payload.get("decision_kind") or ""),
                        "visible_action": summarize_text(step_payload.get("assistant_text"), limit=220),
                        "preceding_observation": previous_observation,
                        "resulting_observation": result_observation,
                        "evidence_classification": {
                            "mode": "reasoning_trace",
                            "action_kind": "finalize",
                            "signal_scope": "visible",
                            "result_kind": result_observation["status"],
                            "result_reason_code": step_payload.get("finalize_reason"),
                            "result_exit_code": None,
                            "non_cot": True,
                        },
                        "source_provenance": provenance,
                        "receipt_refs": [str(trace_path)] + ([model_exchange_ref] if isinstance(model_exchange_ref, str) else []),
                    }
                )
                previous_observation = result_observation
            continue

        for tool_call_index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                parse_issues.append(f"{trace_path}: steps[{index}].tool_calls[{tool_call_index}] is not an object")
                continue
            tool_name = str(tool_call.get("tool_name") or "")
            arguments = tool_call.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            observation = tool_call.get("observation")
            if not isinstance(observation, dict):
                observation = {}
                parse_issues.append(
                    f"{trace_path}: steps[{index}].tool_calls[{tool_call_index}] missing observation object"
                )
            raw_log_path = observation.get("raw_log_path")
            receipt_refs = [str(trace_path)]
            if isinstance(model_exchange_ref, str) and model_exchange_ref:
                receipt_refs.append(model_exchange_ref)
            if isinstance(raw_log_path, str) and raw_log_path:
                receipt_refs.append(raw_log_path)
                receipt_bundle.append(
                    {
                        "receipt_ref": raw_log_path,
                        "receipt_kind": "tool_raw_log_ref",
                        "receipt_name": Path(raw_log_path).name,
                        "step": step_no,
                        "tool_name": tool_name,
                    }
                )

            command = ""
            for key in ("command", "cmd", "summary", "path", "session_id", "job_id"):
                value = arguments.get(key)
                if isinstance(value, str) and value:
                    command = value
                    break
            if not command and tool_name:
                command = tool_name

            result_observation = _summarize_embedded_observation(observation)
            event = {
                "step": step_no,
                "tool_call_index": tool_call_index,
                "tool_name": tool_name,
                "visible_action": summarize_text(command, limit=220),
                "preceding_observation": previous_observation,
                "resulting_observation": result_observation,
                "evidence_classification": {
                    "mode": "reasoning_trace",
                    "action_kind": _classify_action_kind(command, tool_name),
                    "signal_scope": "visible",
                    "result_kind": result_observation.get("status") or ("error" if result_observation.get("error") else "observation"),
                    "result_reason_code": (
                        result_observation.get("error", {}).get("reason_code")
                        if isinstance(result_observation.get("error"), dict)
                        else None
                    ),
                    "result_exit_code": result_observation.get("exit_code"),
                    "non_cot": True,
                },
                "source_provenance": provenance,
                "receipt_refs": receipt_refs,
                "call_role": step_payload.get("call_role"),
                "decision_kind": step_payload.get("decision_kind"),
                "model_visible_requirements": _short_json(
                    visible_context.get("model_visible_requirements"),
                    limit=6,
                ),
                "model_input_digests": _short_json(step_payload.get("model_input_digests"), limit=8),
            }
            events.append(event)
            previous_observation = result_observation

    if events:
        events[-1]["unresolved_verifier_gaps"] = row_gaps
    elif steps:
        parse_issues.append(f"{trace_path}: reasoning trace yielded zero visible events")
    return _dedupe_dicts(receipt_bundle), events, parse_issues


def _extract_verification_gaps_from_route_receipts(route_receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for receipt in route_receipts:
        event_type = receipt.get("event_type")
        if event_type != "verification_completed":
            continue
        observation = receipt.get("observation")
        if not isinstance(observation, dict):
            details = receipt.get("payload", {}).get("details", {})
            observation = details if isinstance(details, dict) else {}
        if isinstance(observation, dict):
            layer_statuses = observation.get("layer_statuses")
            if isinstance(layer_statuses, dict):
                for layer, status in sorted(layer_statuses.items()):
                    if status != "pass":
                        gaps.append({"gap_type": "verification_layer", "layer": layer, "status": status})
            for reason_code in observation.get("reason_codes") or []:
                gaps.append({"gap_type": "verification_reason_code", "reason_code": reason_code})
        if receipt.get("verified") is False:
            gaps.append({"gap_type": "verification_unverified"})
        if receipt.get("verified") is None and event_type == "verification_completed" and not observation.get("verified", True):
            gaps.append({"gap_type": "verification_unverified"})
    return gaps


def _extract_verification_gaps_from_row(row: dict[str, Any] | None) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    from harness.aether2.traces.dt_receipts import _extract_row_discrepancy_reports
    for report in _extract_row_discrepancy_reports(row):
        requirements = report.get("requirements")
        if not isinstance(requirements, list):
            continue
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            verdict = requirement.get("verdict")
            verdict_text = str(verdict).casefold() if verdict is not None else ""
            if verdict_text and verdict_text not in {"pass", "passed", "satisfied", "ok"}:
                gaps.append(
                    {
                        "gap_type": "discrepancy_report",
                        "requirement": requirement.get("requirement"),
                        "verdict": verdict,
                        "evidence": requirement.get("evidence"),
                    }
                )
    return gaps
