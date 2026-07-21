"""Completion-evidence record gates: detect problem -> retry instruction -> refusal.

Extracted from verify_completion_protocol.py (the structural record gate and
the independence-kind gate helpers) and verify_inspection_requests.py (the
matching refusal builders) for the 500-LOC cap. Grouped here because each
gate is a cohesive detect/retry/refuse triple used by
verify_completion_protocol.py's ``verify_with_inspector`` loop:

- structural record gate: ``_completion_record_problem`` ->
  ``_completion_record_retry_instruction`` -> ``_refuse_completion_record``
- independence-kind gate (Phase 1.5, FABLE5_BATCH_AUDIT_20260709T101515Z.md
  secs 4/6): ``_completion_independence_problem`` ->
  ``_independent_derivation_retry_instruction`` ->
  ``_refuse_completion_independence``

Both gates are content-blind: they check presence, non-emptiness, and
inspection_refs provenance/kind only, never the reasoning content. Judging
whether the evidence is actually good stays the verifier model's job.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .verifier_recovery import CompiledEvidenceRequirement, EvidenceClass, validate_compiled_evidence


def _completion_record_problem(
    result: Any,
    performed_refs: set[str],
    *,
    packet: Mapping[str, Any] | None = None,
    inspection_ceilings: Mapping[str, Any] | None = None,
) -> str:
    """Structural validity of the completion_evidence record; '' when valid.

    Presence, non-emptiness, and inspection_refs resolution only. The
    reasoning content is never evaluated -- judging evidence quality stays
    the verifier model's job; this only makes skipping the record visible.
    """
    entries = tuple(getattr(result, "completion_evidence", ()) or ())
    if not entries:
        return "completion_evidence is missing or empty"
    problems: list[str] = []
    for idx, entry in enumerate(entries):
        if not entry.requirement or not entry.observed or not entry.falsification_check:
            problems.append(
                f"completion_evidence[{idx}] has an empty requirement/observed/falsification_check field"
            )
            continue
        if not entry.inspection_refs:
            problems.append(f"completion_evidence[{idx}].inspection_refs is empty")
            continue
        if not any(ref in performed_refs for ref in entry.inspection_refs):
            problems.append(
                f"completion_evidence[{idx}].inspection_refs {list(entry.inspection_refs)} "
                "do not match any inspection performed this round"
            )
    # V5 wiring may provide a compiled clause contract in the neutral packet.
    # Do not infer thresholds from model prose: absent a compiled contract the
    # legacy structural gate remains the only applicable check.
    if isinstance(packet, Mapping):
        raw_requirements = packet.get("compiled_evidence_requirements")
        if raw_requirements is None:
            evidence_meta = packet.get("evidence_requirements")
            if isinstance(evidence_meta, Mapping):
                raw_requirements = evidence_meta.get("compiled_clauses")
        requirements: list[CompiledEvidenceRequirement] = []
        if isinstance(raw_requirements, (list, tuple)):
            for item in raw_requirements:
                if not isinstance(item, Mapping):
                    continue
                clause_id = str(item.get("clause_id", "")).strip()
                minimum = str(item.get("minimum_class", item.get("required_evidence_class", ""))).strip()
                if not clause_id:
                    continue
                try:
                    requirements.append(CompiledEvidenceRequirement(clause_id, EvidenceClass(minimum)))
                except ValueError:
                    problems.append(f"unknown compiled evidence class for clause {clause_id}: {minimum}")
        if requirements:
            packet_ceilings = packet.get("inspection_evidence_ceilings", {})
            if not isinstance(packet_ceilings, Mapping):
                packet_ceilings = {}
            ceilings_raw = dict(packet_ceilings)
            ceilings_raw.update(dict(inspection_ceilings or {}))
            errors = validate_compiled_evidence(
                entries,
                requirements=requirements,
                known_inspection_ids=performed_refs,
                inspection_ceilings=ceilings_raw,
            )
            problems.extend(error.message for error in errors)
    return "; ".join(dict.fromkeys(problems))


def _completion_record_retry_instruction(problem: str) -> dict[str, Any]:
    return {
        "instruction": (
            "Protocol requires a completed verdict to carry completion_evidence "
            "per verifier_runtime_contract.completion_evidence_shape: map each "
            "decisive requirement to what your own inspection observed, cite "
            "inspection_refs containing the registered inspection_id values returned "
            "by inspections performed this round, and state the falsification_check. "
            f"Current problem: {problem}. Return your final verdict again "
            "with a valid record, or a different verdict if the inspected state "
            "does not actually support completion."
        ),
    }


def _refuse_completion_record(problem: str) -> str:
    """Build the uncertain_missing_evidence refusal for an invalid completion_evidence record.

    Shared by the structural gate (missing/empty/unresolved refs) and the
    malformed-shape path (present but wrong-typed record) -- same protocol
    event either way: the completed verdict is refused, never silently
    accepted or crashed on.
    """
    return json.dumps({
        "verdict": "uncertain_missing_evidence",
        "confidence": "high",
        "summary": (
            "Completion cannot be accepted: the completed verdict's "
            f"completion_evidence record is invalid ({problem})."
        ),
        "missing_evidence_requests": [
            "Return completed only with completion_evidence.inspection_refs containing registered inspection_id values from this verification round.",
        ],
        "findings": [
            {
                "finding_id": "vf-completion-evidence-record",
                "verdict": "uncertain_missing_evidence",
                "priority": "blocking",
                "summary": "Completed verdict lacked a valid requirement->observed completion_evidence record.",
                "evidence": [problem],
                "repair_instruction": (
                    "Surface inspectable current-state evidence for each completion requirement; "
                    "completion is accepted only with a resolvable completion_evidence record."
                ),
                "applies_to": ["completion_evidence"],
            },
        ],
    })


def _completion_independence_problem(result: Any, independent_refs: set[str]) -> str:
    """Whether a completed verdict cites an independent-derivation inspection.

    '' when at least one completion_evidence.inspection_refs entry (anywhere
    in the record) resolves to an independent-derivation inspection kind.
    Callers gate this on the architect having flagged the task's claims as
    machine-re-derivable (packet.re_derivable_claims) -- when not flagged,
    this check is not consulted and behavior is unchanged.

    Content-blind: this checks only the KIND of the cited inspection
    (already resolved into ``independent_refs`` by
    ``_independent_derivation_refs``), never whether the reasoning in the
    record is correct.
    """
    entries = tuple(getattr(result, "completion_evidence", ()) or ())
    cited: set[str] = set()
    for entry in entries:
        cited |= set(getattr(entry, "inspection_refs", ()) or ())
    if cited & independent_refs:
        return ""
    return (
        "no completion_evidence.inspection_refs resolve to an independent-derivation "
        "inspection performed this round (overlay_run_command, rerun_check, probe_port, "
        "probe_http, probe_process, or perceive_artifact); this task flags its decisive "
        "claim(s) as machine-re-derivable, so reading a solver-produced artifact alone "
        "is not sufficient"
    )


def _independent_derivation_retry_instruction(problem: str) -> dict[str, Any]:
    return {
        "instruction": (
            "This task's re_derivable_claims marks its decisive claim(s) as "
            "machine-re-derivable. Protocol requires at least one completion_evidence "
            "inspection_ref to resolve to an inspection you performed independently -- "
            "overlay_run_command, rerun_check, a probe_port/probe_http/probe_process live "
            "check, or your own perceive_artifact reading -- not only read_file, "
            "read_output, or receipt/history inspection of a solver-produced artifact. "
            f"Current problem: {problem}. Submit a bounded inspection request performing "
            "that independent derivation, then return your final verdict."
        ),
    }


def _refuse_completion_independence(problem: str) -> str:
    """Build the uncertain_missing_evidence refusal for a missing independent-derivation ref.

    Phase 1.5 closure for the false-clean failure mode: a completed verdict
    whose record is structurally valid but backed only by inspection of
    solver-produced artifacts, on a task whose decisive claims are flagged
    machine-re-derivable, is refused rather than accepted.
    """
    return json.dumps({
        "verdict": "uncertain_missing_evidence",
        "confidence": "high",
        "summary": (
            "Completion cannot be accepted: the completed verdict's "
            f"completion_evidence record is invalid ({problem})."
        ),
        "missing_evidence_requests": [
            "Return completed only after independently deriving the decisive claim via overlay_run_command, rerun_check, a live probe, or your own perceive_artifact reading.",
        ],
        "findings": [
            {
                "finding_id": "vf-completion-evidence-independence",
                "verdict": "uncertain_missing_evidence",
                "priority": "blocking",
                "summary": "Completed verdict did not cite an independent-derivation inspection for a machine-re-derivable claim.",
                "evidence": [problem],
                "repair_instruction": (
                    "Independently re-derive the decisive claim (overlay execution, a live probe, "
                    "or your own perception) rather than only reading solver-produced artifacts, "
                    "then resubmit a completed verdict citing that inspection."
                ),
                "applies_to": ["completion_evidence"],
            },
        ],
    })
