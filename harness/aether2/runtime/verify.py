"""Fresh-context verification and replay checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import json
import re

from harness.aether2.traces.redaction import _clean_hidden_refs
from harness.aether2.runtime.verify_evidence import (
    _classify_evidence_provenance,
    _classify_evidence_strength,
    _finalize_evidence_refs,
    _has_clean_support,
    _normalize_provenance_labels,
)
from harness.aether2.runtime.verify_report import (
    _CONSTRAINT_SIGNAL_TOKENS,
    _assistant_message,
    _build_evidence_source_catalog,
    _call_model,
    _constraint_coverage_tokens,
    _extract_text,
    _extract_tool_calls,
    _inspection_payload,
    _looks_like_constraint,
    _normalize_requirement_item,
    _normalize_verdict,
    _parse_report,
    _parse_tool_call_arguments,
    _read_error_field,
    _read_field,
    _strip_transcript_fields,
    _tool_call_name,
    _verifier_output_failure,
)


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    cwd: str
    duration_sec: float
    timed_out: bool = False
    error_kind: str | None = None
    error_reason_code: str | None = None


@dataclass(frozen=True)
class RequirementResult:
    requirement: str
    verdict: str
    evidence: str
    evidence_strength: str = "weak"
    evidence_strength_reasons: tuple[str, ...] = ()
    evidence_provenance: tuple[str, ...] = ()
    confidence: str = "low"
    evidence_refs: tuple[str, ...] = ()
    unresolved: bool = False
    blocks_readiness: bool = True


@dataclass(frozen=True)
class DiscrepancyReport:
    requirements: tuple[RequirementResult, ...]
    reason_codes: tuple[str, ...]
    summary: str
    raw_response: str

    @property
    def unresolved_requirements(self) -> tuple[RequirementResult, ...]:
        return tuple(
            item
            for item in self.requirements
            if item.blocks_readiness
            and (item.unresolved or item.verdict in {"unsatisfied", "unverifiable"})
        )

    @property
    def has_unresolved_gaps(self) -> bool:
        """True when verification still has any unresolved requirement or parse failure."""
        if "verifier_parse_failed" in self.reason_codes:
            return True
        return any(
            item.blocks_readiness
            and (item.unresolved or item.verdict in {"unsatisfied", "unverifiable"})
            for item in self.requirements
        )

    @property
    def has_discrepancies(self) -> bool:
        """Compatibility alias for unresolved-gap semantics."""
        return self.has_unresolved_gaps


VERIFIER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a read-only inspection command in the live task environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "timeout_sec": {"type": "integer", "default": 120},
                    "cwd": {"type": ["string", "null"]},
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the live task workspace without modifying it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": ["integer", "null"]},
                    "limit": {"type": ["integer", "null"]},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_status",
            "description": "Inspect a detached job without modifying it.",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_read",
            "description": "Read the current contents of a session without sending input.",
            "parameters": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
    },
]

MAX_VERIFIER_INSPECTION_CALLS = 3


def replay_checks(checks: list[str], executor: Any) -> list[CheckResult]:
    results: list[CheckResult] = []
    for command in checks:
        raw = executor.run(command, timeout_sec=120, cwd=None)
        results.append(
            CheckResult(
                command=command,
                exit_code=_read_field(raw, "exit_code"),
                stdout=str(_read_field(raw, "stdout", "")),
                stderr=str(_read_field(raw, "stderr", "")),
                cwd=str(_read_field(raw, "cwd", "")),
                duration_sec=float(_read_field(raw, "duration_sec", _read_field(raw, "duration", 0.0))),
                timed_out=bool(_read_field(raw, "timed_out", False)),
                error_kind=_read_error_field(raw, "kind"),
                error_reason_code=_read_error_field(raw, "reason_code"),
            )
        )
    return results


def verify_fresh_context(
    task: str,
    orientation: Mapping[str, Any],
    diff: Mapping[str, Any],
    claim: Mapping[str, Any],
    checks_results: list[CheckResult],
    action_digest: Mapping[str, Any],
    model_client: Any,
    inspection_ctx: Any | None = None,
    record_exchange: Any | None = None,
    stated_requirements: list[str] | None = None,
    verifier_system_prompt: str = "",
    verifier_focus: list[str] | None = None,
    verifier_do_not_assume: list[str] | None = None,
    required_final_evidence: list[str] | None = None,
    max_inspection_calls: int = MAX_VERIFIER_INSPECTION_CALLS,
) -> DiscrepancyReport:
    clean_orientation = _strip_transcript_fields(_clean_hidden_refs(dict(orientation)))
    clean_diff = _strip_transcript_fields(_clean_hidden_refs(dict(diff)))
    clean_claim = _strip_transcript_fields(_clean_hidden_refs(dict(claim)))
    clean_checks = [
        _strip_transcript_fields(_clean_hidden_refs(result.__dict__))
        for result in checks_results
    ]
    clean_action_digest = _strip_transcript_fields(_clean_hidden_refs(dict(action_digest)))
    clean_stated_requirements = [
        str(item).strip() for item in (stated_requirements or []) if str(item).strip()
    ]
    payload = {
        "task": task,
        "orientation": clean_orientation,
        "workspace_diff": clean_diff,
        "claim": clean_claim,
        "checks_results": clean_checks,
        "action_digest": clean_action_digest,
    }
    if clean_stated_requirements:
        payload["stated_requirements"] = clean_stated_requirements
    if verifier_focus or verifier_do_not_assume or required_final_evidence:
        payload["verifier_policy"] = {
            "focus": [str(item).strip() for item in (verifier_focus or []) if str(item).strip()],
            "do_not_assume": [str(item).strip() for item in (verifier_do_not_assume or []) if str(item).strip()],
            "required_final_evidence": [
                str(item).strip() for item in (required_final_evidence or []) if str(item).strip()
            ],
        }
    verifier_contract = (
        "You are a fresh-context verifier. Evaluate the claim requirement by requirement, "
        "using only the provided task, orientation, workspace diff, replayed checks, and action digest. "
        "Do not assume access to any executor transcript.\n\n"
        "Respond with a single JSON object and nothing else, using EXACTLY this schema "
        "(no other top-level keys are allowed):\n"
        "{\n"
        '  "requirements": [\n'
        '    {"requirement": "<short description of one task requirement>", '
        '"verdict": "satisfied" | "unsatisfied" | "unverifiable", '
        '"evidence": "<evidence for this verdict>", '
        '"evidence_refs": ["<source ref such as checks_results[0], workspace_diff, '
        'action_digest.tool_calls[1], inspection.run_command[0]>", "..."]}\n'
        "    ... one entry per distinct requirement implied by the task ...\n"
        "  ],\n"
        '  "reason_codes": [<list of short machine-readable strings; empty list if no problems>],\n'
        '  "summary": "<one or two sentence overall summary>"\n'
        "}\n"
        'Do NOT use alternative keys such as "claim_satisfied", "verdict" at the top level, or "overall_evidence". '
        'Every requirement entry must use exactly the keys "requirement", "verdict", "evidence", and "evidence_refs". '
        '"evidence_refs" must be grounded in the provided payload or read-only inspection results, '
        'and "verdict" must be exactly one of "satisfied", "unsatisfied", or "unverifiable". '
        "For service or persistence claims, treat a running process, open port, or single startup probe as weak evidence; "
        "prefer bounded survival checks, correct-environment client probes, response/state validation, and any crash/restart/replacement evidence.\n\n"
        "If the payload includes \"stated_requirements\", your \"requirements\" list MUST include at least one entry "
        "for each stated requirement (positive behavior, negative/forbidden side-effect constraints, required "
        "artifact/install paths, final-state/directory invariants, and persistence/service requirements). "
        "Do not invent constraints beyond the provided task text or stated_requirements; cite the exact stated "
        "requirement text in the requirement field."
    )
    prompt_parts = []
    architect_prompt = " ".join(str(verifier_system_prompt or "").split())
    if architect_prompt:
        prompt_parts.append("[architect_verifier_prompt]\n" + architect_prompt)
    prompt_parts.append("[harness_verifier_schema_contract]\n" + verifier_contract)
    messages = [
        {
            "role": "system",
            "content": "\n\n".join(prompt_parts),
        },
        {"role": "user", "content": json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)},
    ]
    response = _call_model(
        model_client,
        messages,
        VERIFIER_TOOL_SCHEMAS if inspection_ctx is not None else [],
    )
    if record_exchange is not None:
        record_exchange(
            messages,
            response,
            VERIFIER_TOOL_SCHEMAS if inspection_ctx is not None else [],
            call_role="verifier",
        )
    messages_for_parse = list(messages)
    inspection_records: list[dict[str, Any]] = []
    tool_calls = _extract_tool_calls(response)
    if inspection_ctx is not None and tool_calls:
        inspection_budget = max(0, int(max_inspection_calls))
        executed_inspections = 0
        messages_for_parse.append(_assistant_message(response))
        for tool_call in tool_calls:
            tool_name = _tool_call_name(tool_call)
            arguments = _parse_tool_call_arguments(tool_call)
            tool_call_id = tool_call.get("id")
            if tool_name is None:
                tool_name = "unknown"
                sanitized_result = _inspection_error_payload(
                    kind="verification_unknown_tool",
                    message="verifier requested an unknown inspection tool",
                    reason_code="verification_unknown_tool",
                )
                inspection_records.append(
                    {
                        "tool_name": tool_name,
                        "arguments": dict(arguments),
                        "result": sanitized_result,
                    }
                )
                messages_for_parse.append(_inspection_tool_message(tool_name, tool_call_id, sanitized_result))
                continue
            handler = getattr(inspection_ctx, tool_name, None)
            if handler is None:
                sanitized_result = _inspection_error_payload(
                    kind="verification_unknown_tool",
                    message=f"verifier requested unavailable inspection tool: {tool_name}",
                    reason_code="verification_unknown_tool",
                )
            elif executed_inspections >= inspection_budget:
                sanitized_result = _inspection_error_payload(
                    kind="verification_inspection_budget_exhausted",
                    message="verifier read-only inspection budget exhausted",
                    reason_code="verification_inspection_budget_exhausted",
                )
            else:
                result = handler(**arguments)
                sanitized_result = _inspection_payload(result)
                executed_inspections += 1
            inspection_records.append(
                {
                    "tool_name": tool_name,
                    "arguments": dict(arguments),
                    "result": sanitized_result,
                }
            )
            messages_for_parse.append(_inspection_tool_message(tool_name, tool_call_id, sanitized_result))
        response = _call_model(model_client, messages_for_parse, [])
        if record_exchange is not None:
            record_exchange(
                messages_for_parse,
                response,
                [],
                call_role="verifier",
            )
    raw_text = _extract_text(response)
    parsed = _parse_report(raw_text)
    source_catalog = _build_evidence_source_catalog(
        clean_orientation,
        clean_diff,
        clean_claim,
        clean_checks,
        clean_action_digest,
        inspection_records,
        raw_response=raw_text,
        parsed_report=parsed,
    )
    requirements = tuple(
        _requirement_result_from_report_item(
            item,
            reason_codes=parsed.get("reason_codes", []),
            source_catalog=source_catalog,
        )
        for item in parsed.get("requirements", [])
        if str(item.get("requirement", "")).strip()
    )
    requirements = _downgrade_nonblocking_process_gaps(requirements, clean_action_digest)
    requirements = requirements + _uncovered_constraint_results(
        clean_stated_requirements, requirements
    )
    return DiscrepancyReport(
        requirements=requirements,
        reason_codes=tuple(str(code) for code in parsed.get("reason_codes", [])),
        summary=str(parsed.get("summary", "")),
        raw_response=raw_text,
    )


def _inspection_error_payload(*, kind: str, message: str, reason_code: str) -> dict[str, Any]:
    return {
        "exit_code": 1,
        "cwd": "",
        "stdout_head": "",
        "stdout_tail": "",
        "stderr_head": message,
        "stderr_tail": "",
        "error": {
            "kind": kind,
            "message": message,
            "reason_code": reason_code,
        },
    }


def _inspection_tool_message(
    tool_name: str,
    tool_call_id: Any,
    sanitized_result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "role": "tool",
        "name": tool_name,
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            dict(sanitized_result),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ),
    }


def _requirement_result_from_report_item(
    item: Mapping[str, Any],
    *,
    reason_codes: list[str],
    source_catalog: Mapping[str, Any],
) -> RequirementResult:
    requirement = str(item.get("requirement", ""))
    verdict = _normalize_verdict(str(item.get("verdict", "unverifiable")))
    evidence = str(item.get("evidence", ""))
    evidence_refs = _finalize_evidence_refs(item.get("evidence_refs"), evidence, source_catalog)
    evidence_provenance = _normalize_provenance_labels(item.get("evidence_provenance"))
    assessment = _classify_evidence_strength(
        requirement=requirement,
        verdict=verdict,
        evidence=evidence,
        evidence_refs=evidence_refs,
        source_catalog=source_catalog,
        report_reason_codes=reason_codes,
    )
    if not evidence_provenance:
        evidence_provenance = _classify_evidence_provenance(
            requirement=requirement,
            verdict=verdict,
            evidence=evidence,
            evidence_refs=evidence_refs,
            source_catalog=source_catalog,
            report_reason_codes=reason_codes,
            assessment=assessment,
        )
    blocks_readiness = _requirement_blocks_readiness(requirement)
    return RequirementResult(
        requirement=requirement,
        verdict=verdict,
        evidence=evidence,
        evidence_strength=assessment["strength"],
        evidence_strength_reasons=tuple(assessment["reasons"]),
        evidence_provenance=tuple(evidence_provenance),
        confidence=assessment["confidence"],
        evidence_refs=evidence_refs,
        unresolved=verdict in {"unsatisfied", "unverifiable"} or not _has_clean_support(
            requirement=requirement,
            verdict=verdict,
            strength=assessment["strength"],
            evidence_provenance=tuple(evidence_provenance),
            evidence_strength_reasons=tuple(assessment["reasons"]),
        ),
        blocks_readiness=blocks_readiness,
    )


def _uncovered_constraint_results(
    stated_requirements: list[str],
    requirements: tuple[RequirementResult, ...],
) -> tuple[RequirementResult, ...]:
    """W5.2: hard stated task constraints that the verifier did not address at
    all become explicit readiness gaps.

    Lower-authority inferred/watchpoint lines can inform the verifier without
    becoming hard readiness blockers on their own.
    """
    if not stated_requirements:
        return ()
    covered_tokens: set[str] = set()
    for item in requirements:
        covered_tokens |= _constraint_coverage_tokens(item.requirement)
        covered_tokens |= _constraint_coverage_tokens(item.evidence)
    extras: list[RequirementResult] = []
    for stated in stated_requirements:
        if not _requirement_blocks_readiness(stated):
            continue
        if not _looks_like_constraint(stated):
            continue
        stated_tokens = _constraint_coverage_tokens(stated)
        if not stated_tokens:
            continue
        overlap = stated_tokens & covered_tokens
        if len(overlap) >= max(1, len(stated_tokens) // 2):
            continue
        extras.append(
            RequirementResult(
                requirement=stated,
                verdict="unverifiable",
                evidence=(
                    "Stated task requirement/constraint was not addressed by any "
                    "verifier requirement entry."
                ),
                evidence_strength="weak",
                evidence_strength_reasons=("uncovered_stated_requirement",),
                evidence_provenance=("unknown",),
                confidence="low",
                evidence_refs=("claim",),
                unresolved=True,
                blocks_readiness=True,
            )
        )
    return tuple(extras)


def _downgrade_nonblocking_process_gaps(
    requirements: tuple[RequirementResult, ...],
    action_digest: Mapping[str, Any],
) -> tuple[RequirementResult, ...]:
    """Do not fail readiness on unobservable process/intent claims alone.

    The verifier should block on missing task evidence. It should not force the
    agent to prove mental intent, or prove a negative hidden-asset claim when
    the visible action digest contains no hidden/reviewer access.
    """

    hidden_access_observed = _action_digest_mentions_hidden_or_reviewer(action_digest)
    normalized: list[RequirementResult] = []
    for item in requirements:
        if not item.unresolved:
            normalized.append(item)
            continue
        text = f"{item.requirement} {item.evidence}".lower()
        if _is_planning_or_process_observability_gap(text):
            normalized.append(
                RequirementResult(
                    requirement=item.requirement,
                    verdict="satisfied",
                    evidence=(
                        item.evidence
                        + " This process-only observability gap is not a task-readiness blocker."
                    ),
                    evidence_strength=item.evidence_strength,
                    evidence_strength_reasons=item.evidence_strength_reasons,
                    evidence_provenance=item.evidence_provenance,
                    confidence=item.confidence,
                    evidence_refs=item.evidence_refs,
                    unresolved=False,
                )
            )
            continue
        if _is_hidden_access_absence_gap(text) and not hidden_access_observed:
            normalized.append(
                RequirementResult(
                    requirement=item.requirement,
                    verdict="satisfied",
                    evidence=(
                        "No hidden/reviewer access is visible in the action digest; absence-only proof is "
                        "not treated as a task-readiness blocker."
                    ),
                    evidence_strength="moderate",
                    evidence_strength_reasons=("no_hidden_access_in_action_digest",),
                    evidence_provenance=("action_digest",),
                    confidence="medium",
                    evidence_refs=("action_digest.tool_calls",),
                    unresolved=False,
                )
            )
            continue
        normalized.append(item)
    return tuple(normalized)


def _is_planning_or_process_observability_gap(text: str) -> bool:
    has_process_signal = (
        "plan before" in text
        or "planning" in text
        or "pre-action plan" in text
        or "explicit pre-action" in text
        or "does not expose" in text
        or "intent" in text
        or "mental" in text
        or "process-only" in text
    )
    has_observability_gap = (
        "unverifiable" in text
        or "not directly" in text
        or "not explicitly" in text
        or "does not expose" in text
    )
    return has_process_signal and has_observability_gap


def _is_hidden_access_absence_gap(text: str) -> bool:
    return (
        ("hidden" in text or "reviewer" in text)
        and ("absence" in text or "no direct evidence" in text or "cannot be fully verified" in text)
    )


def _action_digest_mentions_hidden_or_reviewer(action_digest: Mapping[str, Any]) -> bool:
    tool_calls = action_digest.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        if "hidden" in str(call).lower() or "reviewer" in str(call).lower():
            return True
    return False


def _is_external_grader_authority_note(stated_requirement: str) -> bool:
    """Return True for grader-authority notes that the agent cannot prove.

    Internal verification should check readiness evidence. It should not require
    the model to prove the hidden/official grader's future behavior from inside
    the task workspace.
    """

    text = stated_requirement.strip().lower()
    if not text:
        return False
    return (
        "hidden grading" in text
        or "hidden grader" in text
        or "official grader" in text
        or "grader remains" in text
    )


def _requirement_blocks_readiness(requirement: str) -> bool:
    text = requirement.strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("[inferred]") or lowered.startswith("[watchpoint]"):
        return False
    return not _is_external_grader_authority_note(text)
