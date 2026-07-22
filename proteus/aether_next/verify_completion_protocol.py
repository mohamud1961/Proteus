"""Completion-evidence protocol enforcement and the verify_with_inspector loop.

Extracted from model_hooks.py for the 500-LOC cap. The record/independence
gate helpers (problem detection, retry instructions, and refusal builders)
were further extracted to verify_completion_gates.py for the same cap;
imported back below unchanged.

Design of record: audit addendum (FABLE5_ADVERSARIAL_AUDIT_20260708T165639Z.md)
Concern 1 -- the harness checks presence, non-emptiness, and that
inspection_refs resolve to inspections actually performed in the round. It
never evaluates reasoning content. Phase 1.5 (FABLE5_BATCH_AUDIT_20260709T101515Z.md
secs 4/6) adds a content-blind independence-kind requirement: when the
architect flags a task's decisive claims as machine-re-derivable
(``verifier_packet.re_derivable_claims``), a completed verdict must also cite
an inspection of an independent-derivation kind, not only a read of a
solver-produced artifact.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping

from .ledger import ExecutionLedger
from .inspection_registry import inspection_ceilings_from_results
from .model_prompts import VERIFIER_RUNTIME_CONTRACT
from .runtime_ir import CompiledRuntime
from .verifier import CompletionEvidenceShapeError, parse_model_verifier_result
from .verifier_inspector import parse_verifier_inspection_requests
from .verify_completion_gates import (
    _completion_independence_problem,
    _completion_record_problem,
    _completion_record_retry_instruction,
    _independent_derivation_retry_instruction,
    _refuse_completion_independence,
    _refuse_completion_record,
)
from .verify_inspection_requests import (
    _completed_inspection_is_semantically_grounded,
    _default_completion_inspection_requests,
    _independent_derivation_refs,
    _inspections_from_missing_evidence,
    _model_output_error,
    _refs_from_inspections,
    _structured_missing_evidence_requests,
    _verifier_identity_prompt_for,
    _verifier_max_output_tokens,
)

if TYPE_CHECKING:
    from .model_hooks import ModelHooks


def verify_with_inspector(
    hooks: "ModelHooks",
    packet: Mapping[str, Any],
    compiled: CompiledRuntime,
    ledger: ExecutionLedger,
    inspector,
) -> str:
    """Bounded-rounds verifier loop with read-only inspection and the completion-evidence gate.

    ``hooks`` is the owning ``ModelHooks`` instance: this is a pure move of
    the former ``ModelHooks.verify_with_inspector`` method body into a
    module-level function (``self`` -> ``hooks``) so the gate machinery can
    live outside model_hooks.py while ``ModelHooks`` keeps the same bound
    method other callers (e.g. kernel_verifier.py) already depend on.
    """
    hooks.last_parse_errors = []
    user_payload = {
        "verifier_runtime_contract": VERIFIER_RUNTIME_CONTRACT,
        "verifier_packet": dict(packet),
        "compiled_summary": compiled.task_prompt[:500],
        "ledger_receipt_count": len(ledger.all_receipts()),
    }
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": _verifier_identity_prompt_for(compiled),
        },
        {"role": "user", "content": json.dumps(user_payload, default=str, sort_keys=True)},
    ]
    max_rounds = int(VERIFIER_RUNTIME_CONTRACT["read_only_inspector"]["max_rounds"])
    inspected = False
    missing_evidence_realized = False
    record_retry_used = False
    independence_retry_used = False
    # Architect-flagged trigger (Phase 1.5): when the task names claims
    # that are machine-re-derivable, a completed verdict must cite an
    # independent-derivation inspection. Absent/empty means unflagged --
    # unchanged legacy behavior, no independence requirement applied.
    require_independent_derivation = bool(packet.get("re_derivable_claims"))
    performed_refs: set[str] = set()
    performed_independent_refs: set[str] = set()
    performed_ceilings: dict[str, str] = {}
    last_inspection_results: list[dict[str, Any]] = []
    for round_idx in range(max_rounds + 1):
        try:
            raw = hooks.call_verifier(
                messages,
                max_output_tokens=_verifier_max_output_tokens(),
            )
            setattr(hooks, "last_raw_verifier_output", raw)
        except Exception as exc:
            hooks.last_parse_errors.append(str(exc))
            raise
        try:
            result = parse_model_verifier_result(raw)
        except CompletionEvidenceShapeError as shape_exc:
            # The verdict JSON itself parsed and named completion_evidence;
            # only that field's shape is wrong (e.g. a list of strings,
            # the pre-Phase-1 stub shape). Route to the SAME record-
            # problem retry the structural gate below uses, sharing its
            # one-retry budget -- never the generic "not valid protocol
            # JSON" path, which would misdirect the model to resend a
            # bare verdict/confidence/summary and could burn every
            # remaining round chasing the wrong fix.
            hooks.last_parse_errors.append(str(shape_exc))
            if round_idx < max_rounds and not record_retry_used:
                record_retry_used = True
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": json.dumps(
                        _completion_record_retry_instruction(str(shape_exc)),
                        default=str,
                        sort_keys=True,
                    ),
                })
                continue
            return _refuse_completion_record(str(shape_exc))
        except Exception as verdict_exc:
            try:
                requests = parse_verifier_inspection_requests(raw)
            except Exception as inspection_exc:
                hooks.last_parse_errors.append(f"{verdict_exc}; {inspection_exc}")
                if round_idx < max_rounds:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({
                        "role": "user",
                        "content": json.dumps(
                            {
                                "instruction": (
                                    "Your previous verifier message was not valid protocol JSON. "
                                    "Return exactly one JSON object and no prose. The object must "
                                    "be either a final verifier verdict with fields verdict, "
                                    "confidence, and summary, or an inspection request with "
                                    "kind='inspect' and a non-empty requests list."
                                ),
                            },
                            default=str,
                            sort_keys=True,
                        ),
                    })
                    continue
                raise
            results = inspector(requests)
            inspected = True
            performed_refs |= _refs_from_inspections(requests, results)
            performed_independent_refs |= _independent_derivation_refs(requests, results)
            performed_ceilings.update(inspection_ceilings_from_results(results))
            last_inspection_results = list(results)
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": json.dumps(
                    {
                        "verifier_inspection_results": results,
                        "instruction": "Use these observations together with the original verifier_packet and return either a final verdict or another bounded inspection request.",
                    },
                    default=str,
                    sort_keys=True,
                ),
            })
            continue
        if result.verdict == "blocked_by_tooling" and not inspected and round_idx < max_rounds:
            auto_requests = _default_completion_inspection_requests(packet)
            if auto_requests:
                results = inspector(auto_requests)
                inspected = True
                performed_refs |= _refs_from_inspections(auto_requests, results)
                performed_independent_refs |= _independent_derivation_refs(auto_requests, results)
                performed_ceilings.update(inspection_ceilings_from_results(results))
                last_inspection_results = list(results)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": json.dumps(
                        {
                            "verifier_inspection_results": results,
                            "instruction": (
                                "blocked_by_tooling is only valid after at least one read-only "
                                "inspection attempt fails. The runtime supplied available inspection "
                                "results. Use them with the original verifier_packet and return a "
                                "final verdict; request another bounded inspection only if these "
                                "observations are genuinely insufficient."
                            ),
                        },
                        default=str,
                        sort_keys=True,
                    ),
                })
                continue
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": (
                            "Do not return blocked_by_tooling before attempting inspection. "
                            "Emit kind='inspect' with read_file, read_output, rerun_check, "
                            "overlay_run_command, probe_port/probe_http/probe_process, "
                            "inspect_artifact, or perceive_artifact as appropriate."
                        ),
                    },
                    default=str,
                    sort_keys=True,
                ),
            })
            continue

        if (
            result.verdict == "uncertain_missing_evidence"
            and round_idx < max_rounds
            and not missing_evidence_realized
        ):
            # Realize once per verification round: inspect, re-judge, and
            # if the verdict is still uncertain let durable findings and
            # unchanged-state memoization take over instead of looping.
            missing_evidence_realized = True
            auto_requests = _inspections_from_missing_evidence(result, packet=packet)
            if auto_requests:
                results = inspector(auto_requests)
                inspected = True
                performed_refs |= _refs_from_inspections(auto_requests, results)
                performed_independent_refs |= _independent_derivation_refs(auto_requests, results)
                performed_ceilings.update(inspection_ceilings_from_results(results))
                last_inspection_results = list(results)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": json.dumps(
                        {
                            "verifier_inspection_results": results,
                            "instruction": (
                                "The runtime executed read-only inspections for the files "
                                "your missing-evidence requests named: the solver cannot "
                                "supply packet evidence, only your own inspection can. "
                                "Judge the current state now and return a final verdict; "
                                "request further bounded inspections only if these "
                                "observations are genuinely insufficient."
                            ),
                        },
                        default=str,
                        sort_keys=True,
                    ),
                })
                continue
        # Runtime-enforced, not prompt-only: a completed verdict must be
        # backed by at least one real independent inspection when the
        # inspector is available -- a model that judges "completed"
        # straight from the packet's narrative is exactly the false-clean
        # failure mode this mechanism exists to close. Force one more
        # round requiring inspection rather than trusting the prompt alone.
        if result.verdict == "completed" and not inspected and round_idx < max_rounds:
            auto_requests = _default_completion_inspection_requests(packet)
            if auto_requests:
                results = inspector(auto_requests)
                inspected = True
                performed_refs |= _refs_from_inspections(auto_requests, results)
                performed_independent_refs |= _independent_derivation_refs(auto_requests, results)
                performed_ceilings.update(inspection_ceilings_from_results(results))
                last_inspection_results = list(results)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": json.dumps(
                        {
                            "verifier_inspection_results": results,
                            "instruction": (
                            "The runtime supplied a minimal read-only current-state "
                            "inspection because completed cannot be accepted from "
                            "packet evidence alone. Use these observations together "
                            "with the original verifier_packet and return your final "
                            "verdict. Treat solver-authored validation commands and "
                            "recomputation receipts as claims to audit, not as proof; "
                            "inspect whether their method matches the task semantics "
                            "before returning completed."
                        ),
                    },
                        default=str,
                        sort_keys=True,
                    ),
                })
                continue
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": (
                            "Protocol requires at least one read-only inspection before a "
                            "completed verdict can be accepted. Submit a bounded inspection "
                            "request (kind: inspect) that independently confirms the claim "
                            "your verdict depends on, then return your verdict."
                        ),
                    },
                    default=str,
                    sort_keys=True,
                ),
            })
            continue
        if (
            result.verdict == "completed"
            and inspected
            and not _completed_inspection_is_semantically_grounded(packet, last_inspection_results)
            and round_idx < max_rounds
        ):
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": (
                            "Do not return completed yet. The inspections so far only prove shape or artifact presence, "
                            "not semantically grounded current-state support for the produced result. Inspect concrete "
                            "result-bearing evidence next, such as the latest command output, produced output artifact, "
                            "or an independent overlay check against the deliverable, then judge again."
                        ),
                    },
                    default=str,
                    sort_keys=True,
                ),
            })
            continue
        if result.verdict == "completed" and inspected and round_idx < max_rounds:
            record_problem = _completion_record_problem(
                result, performed_refs, packet=packet,
                inspection_ceilings=performed_ceilings,
            )
            if record_problem and not record_retry_used:
                # Content-blind protocol enforcement, mirroring the
                # inspection-required gate: the record must exist and its
                # inspection_refs must resolve to inspections that actually
                # happened this round. Whether the evidence is GOOD stays
                # the model's judgment.
                record_retry_used = True
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": json.dumps(
                        _completion_record_retry_instruction(record_problem),
                        default=str,
                        sort_keys=True,
                    ),
                })
                continue
            if (
                not record_problem
                and require_independent_derivation
                and not independence_retry_used
            ):
                # Phase 1.5: the record is structurally valid, but this
                # task flags its decisive claim(s) as machine-re-derivable
                # -- require at least one cited inspection to be an
                # independent-derivation kind, not only a read of a
                # solver-produced artifact. Same one-retry-then-refuse
                # shape as the structural gate above, its own budget.
                independence_problem = _completion_independence_problem(
                    result, performed_independent_refs,
                )
                if independence_problem:
                    independence_retry_used = True
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({
                        "role": "user",
                        "content": json.dumps(
                            _independent_derivation_retry_instruction(independence_problem),
                            default=str,
                            sort_keys=True,
                        ),
                    })
                    continue
        if result.verdict == "uncertain_missing_evidence" and round_idx < max_rounds:
            try:
                missing_requests = _structured_missing_evidence_requests(raw)
            except Exception as exc:
                hooks.last_parse_errors.append(str(exc))
                missing_requests = ()
            if missing_requests:
                results = inspector(missing_requests)
                inspected = True
                performed_refs |= _refs_from_inspections(missing_requests, results)
                performed_independent_refs |= _independent_derivation_refs(missing_requests, results)
                performed_ceilings.update(inspection_ceilings_from_results(results))
                last_inspection_results = list(results)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": json.dumps(
                        {
                            "verifier_inspection_results": results,
                            "instruction": (
                                "The runtime executed the structured read-only evidence "
                                "requests from your uncertain_missing_evidence verdict. "
                                "Use these observations with the original verifier_packet "
                                "and return a final verdict or another bounded inspection request."
                            ),
                        },
                        default=str,
                        sort_keys=True,
                    ),
                })
                continue
        if result.verdict == "completed" and not inspected:
            # Out of rounds and still uninspected: do not accept the
            # completion. Return an explicit non-completion verdict so the
            # solver sees a durable evidence gap instead of an opaque
            # verifier protocol error. This is protocol enforcement, not a
            # harness-side judgment that the task is wrong.
            return json.dumps({
                "verdict": "uncertain_missing_evidence",
                "confidence": "high",
                "summary": (
                    "Completion cannot be accepted because the verifier "
                    "did not perform a read-only current-state inspection."
                ),
                "missing_evidence_requests": [
                    "Provide independent current-state evidence, such as a relevant file read, recent receipt, or rerun check, before accepting completed.",
                ],
                "findings": [
                    {
                        "finding_id": "vf-uninspected-completion",
                        "verdict": "uncertain_missing_evidence",
                        "priority": "blocking",
                        "summary": "The verifier attempted to mark the task completed without read-only inspection.",
                        "evidence": [
                            "A completed verifier verdict requires read-only inspection when inspector tools are available.",
                        ],
                        "repair_instruction": (
                            "Surface concrete current-state evidence and resubmit only after the evidence gap is closed."
                        ),
                        "applies_to": ["completion_evidence"],
                    },
                ],
            })
        if result.verdict == "completed":
            record_problem = _completion_record_problem(
                result, performed_refs, packet=packet,
                inspection_ceilings=performed_ceilings,
            )
            if record_problem:
                # Out of retries and the record is still structurally
                # invalid: refuse the completion as a protocol event.
                # This is not a harness judgment that the task is wrong.
                return _refuse_completion_record(record_problem)
            if require_independent_derivation:
                independence_problem = _completion_independence_problem(
                    result, performed_independent_refs,
                )
                if independence_problem:
                    # Out of retries and no cited inspection resolves to
                    # an independent-derivation kind: refuse. This is the
                    # Phase 1.5 closure for the false-clean failure mode
                    # -- a model that only ever read solver-produced
                    # artifacts cannot self-certify a machine-re-derivable
                    # claim. Not a harness judgment that the task is wrong.
                    return _refuse_completion_independence(independence_problem)
        return raw
    raise _model_output_error("verifier exceeded bounded inspection rounds without returning a verdict")
