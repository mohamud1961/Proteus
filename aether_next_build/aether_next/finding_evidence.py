"""Mechanical relevance rules for active Verifier findings.

The kernel does not judge task semantics.  It binds findings to model-authored
clause/target identifiers and permits progress only when a later concrete
receipt touches those identifiers, or when a generic evidence finding receives
a fresh current-state inspection.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


_RELEVANT_EVIDENCE_KINDS = frozenset({
    "read_file",
    "read_file_page",
    "read_output",
    "grep_output",
    "write_file",
    "run_command",
    "bootstrap_acquire",
    "process_launch",
    "process_stop",
    "service_probe",
    "inspect_artifact",
    "inspect_diff",
    "query_artifact_history",
    "check_result",
    "schema_validation",
    "inspection_record",
    "observation_batch_result",
})

_GENERIC_TARGETS = frozenset({
    "completion_evidence",
    "current_state",
    "task_state",
    "deliverable",
    "result",
})


def _normalise_target(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    for prefix in (
        "path:", "handle:", "target:", "check_id:", "service_name:",
        "process_id:", "clause:", "clause_id:",
    ):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.startswith("/app/"):
        text = text[5:]
    elif text == "/app":
        text = "."
    return text.strip("/").lower()


def _target_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if left in _GENERIC_TARGETS or right in _GENERIC_TARGETS:
        return False
    return left.endswith("/" + right) or right.endswith("/" + left)


def finding_targets(finding: Any) -> set[str]:
    raw = finding.get("applies_to", ()) if isinstance(finding, Mapping) else getattr(finding, "applies_to", ())
    return {_normalise_target(item) for item in (raw or ()) if _normalise_target(item)}


def receipt_targets(receipt: Any) -> set[str]:
    payload = getattr(receipt, "payload", {})
    if not isinstance(payload, Mapping):
        payload = {}
    values: list[Any] = []
    for key in (
        "path", "handle", "target", "check_id", "service_name", "process_id",
        "clause_id", "target_identity",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            values.append(value)
    for key in (
        "artifact_paths", "modified_paths", "created_paths", "removed_paths",
        "inspection_ids", "clause_ids",
    ):
        raw = payload.get(key, ())
        if isinstance(raw, (list, tuple, set)):
            values.extend(raw)
    command = str(payload.get("command", "")).strip()
    if command:
        values.append(command)
    return {_normalise_target(item) for item in values if _normalise_target(item)}


def receipt_is_relevant(receipt: Any, finding: Any) -> bool:
    if getattr(receipt, "kind", "") not in _RELEVANT_EVIDENCE_KINDS:
        return False
    if not bool(getattr(receipt, "success", False)):
        return False
    targets = finding_targets(finding)
    observed = receipt_targets(receipt)
    if targets & _GENERIC_TARGETS:
        # Generic evidence gaps require a concrete current-state observation or
        # mutation, never memory/prose/control-plane activity.
        return bool(observed) or getattr(receipt, "kind", "") in {
            "inspection_record", "check_result", "schema_validation",
            "observation_batch_result",
        }
    if not targets:
        return bool(observed)
    return any(_target_matches(left, right) for left in targets for right in observed)


def evidence_after_latest_verifier(ledger: Any, finding: Any) -> tuple[Any, ...]:
    receipts = tuple(ledger.all_receipts())
    last_verifier = -1
    for index, receipt in enumerate(receipts):
        if getattr(receipt, "kind", "") == "model_verifier_result":
            last_verifier = index
    return tuple(
        receipt for receipt in receipts[last_verifier + 1:]
        if receipt_is_relevant(receipt, finding)
    )


def active_findings_need_relevant_evidence(ledger: Any) -> bool:
    active = ledger.active_finding_context(len(ledger.all_receipts()), limit=1000)
    return any(not evidence_after_latest_verifier(ledger, finding) for finding in active)


def _current_registered_inspections(
    ledger: Any,
    inspection_ids: Iterable[str],
) -> tuple[Any, ...]:
    current_generation = int(ledger.task_state_generation())
    wanted = {str(item).strip() for item in inspection_ids if str(item).strip()}
    rows: list[Any] = []
    for receipt in ledger.all_receipts():
        if getattr(receipt, "kind", "") != "inspection_record":
            continue
        payload = getattr(receipt, "payload", {})
        inspection_id = str(payload.get("inspection_id", getattr(receipt, "receipt_id", ""))).strip()
        if inspection_id not in wanted or not bool(getattr(receipt, "success", False)):
            continue
        if not bool(payload.get("eligible_for_proof", False)):
            continue
        try:
            generation = int(payload.get("task_state_generation"))
        except (TypeError, ValueError):
            continue
        if generation == current_generation:
            rows.append(receipt)
    return tuple(rows)


def resolved_finding_ids_for_completed(ledger: Any, result: Any) -> set[str]:
    """Findings directly covered by current registered completion evidence."""
    entries = tuple(getattr(result, "completion_evidence", ()) or ())
    active = tuple(getattr(ledger.findings, "active", {}).values())
    resolved: set[str] = set()
    for finding in active:
        targets = finding_targets(finding)
        observed_generation = int(getattr(finding, "observed_task_state_generation", -1))
        for entry in entries:
            inspection_ids = tuple(getattr(entry, "inspection_refs", ()) or ())
            inspections = _current_registered_inspections(ledger, inspection_ids)
            if not inspections:
                continue
            clause_ids = {
                _normalise_target(item)
                for item in (getattr(entry, "clause_ids", ()) or ())
                if _normalise_target(item)
            }
            if targets & clause_ids:
                resolved.add(finding.finding_id)
                break
            if targets & _GENERIC_TARGETS or not targets:
                if any(
                    int(item.payload.get("task_state_generation", -1)) >= observed_generation
                    for item in inspections
                ):
                    resolved.add(finding.finding_id)
                    break
            if any(
                receipt_is_relevant(item, finding)
                and int(item.payload.get("task_state_generation", -1)) >= observed_generation
                for item in inspections
            ):
                resolved.add(finding.finding_id)
                break
    return resolved
