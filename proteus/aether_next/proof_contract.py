"""Minimal typed work-and-proof contract for certified completion.

This module does not decide arbitrary task semantics. It certifies whether an
Architect-declared evidence route can reach the required evidential strength,
and whether current receipted evidence satisfies each critical clause.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .ledger import ExecutionLedger, Receipt


EVIDENCE_STRENGTH: dict[str, int] = {
    "model_claim": 0,
    "shape": 1,
    "metadata_proxy": 2,
    "solver_authored_test": 3,
    "same_method": 3,
    "behavioral": 4,
    "exact_contract": 5,
    "independent_semantic": 6,
    "official_grader": 7,
}

# Maximum evidence class that a route can establish. The route still has to
# execute successfully and produce content supporting the clause. In
# particular, process/port liveness can never establish protocol semantics.
ROUTE_EVIDENCE_CEILINGS: dict[str, str] = {
    "read_file": "exact_contract",
    "read_output": "behavioral",
    "inspect_artifact": "exact_contract",
    "rerun_check": "exact_contract",
    "overlay_run_command": "exact_contract",
    "probe_port": "metadata_proxy",
    "probe_process": "metadata_proxy",
    "probe_http": "behavioral",
    "perceive_artifact": "independent_semantic",
    "inspect_recent_receipts": "metadata_proxy",
    "inspect_artifact_history": "metadata_proxy",
}

INDEPENDENT_ROUTE_KINDS = frozenset({
    "read_file",
    "inspect_artifact",
    "rerun_check",
    "overlay_run_command",
    "probe_port",
    "probe_process",
    "probe_http",
    "perceive_artifact",
})

INDEPENDENT_PROVENANCE = frozenset({
    "direct_current_state",
    "independent_interface_probe",
    "task_provided_check",
    "verifier_inspection",
    "official_grader",
})


@dataclass(frozen=True)
class CertifiedProofClause:
    clause_id: str
    requirement: str
    solver_handling: str
    verifier_route: str
    fallback_route: str
    falsification_check: str
    required_evidence_class: str
    route_kind: str
    route_evidence_ceiling: str
    requires_independent_evidence: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "requirement": self.requirement,
            "solver_handling": self.solver_handling,
            "verifier_route": self.verifier_route,
            "fallback_route": self.fallback_route,
            "falsification_check": self.falsification_check,
            "required_evidence_class": self.required_evidence_class,
            "route_kind": self.route_kind,
            "route_evidence_ceiling": self.route_evidence_ceiling,
            "requires_independent_evidence": self.requires_independent_evidence,
        }


@dataclass(frozen=True)
class ProofContractIssue:
    code: str
    clause_id: str
    detail: str


@dataclass(frozen=True)
class ClauseEvidenceDecision:
    clause_id: str
    satisfied: bool
    code: str
    detail: str
    evidence_receipt_ids: tuple[str, ...] = ()


def _route_kind(route: str) -> str:
    return str(route or "").strip().split(":", 1)[0]


def _strength(value: str) -> int:
    return EVIDENCE_STRENGTH.get(str(value).strip(), -1)


def certify_proof_contract(
    coverage_rows: Sequence[Mapping[str, Any]],
    check_rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[CertifiedProofClause, ...], tuple[ProofContractIssue, ...]]:
    """Certify clause/route alignment and evidential ceilings."""
    if not coverage_rows and not check_rows:
        return (), ()
    issues: list[ProofContractIssue] = []
    coverage_by_id: dict[str, Mapping[str, Any]] = {}
    checks_by_id: dict[str, Mapping[str, Any]] = {}
    for source, rows, target in (
        ("coverage", coverage_rows, coverage_by_id),
        ("check", check_rows, checks_by_id),
    ):
        for row in rows:
            clause_id = str(row.get("clause_id", "")).strip()
            if not clause_id:
                issues.append(ProofContractIssue(
                    "proof_clause_id_missing", "", f"{source} row has no clause_id",
                ))
                continue
            if clause_id in target:
                issues.append(ProofContractIssue(
                    "proof_clause_id_duplicate", clause_id, f"duplicate {source} row",
                ))
                continue
            target[clause_id] = row
    for clause_id in sorted(set(coverage_by_id) ^ set(checks_by_id)):
        issues.append(ProofContractIssue(
            "proof_clause_route_missing", clause_id,
            "clause coverage and verifier route IDs must match exactly",
        ))

    certified: list[CertifiedProofClause] = []
    for clause_id in sorted(set(coverage_by_id) & set(checks_by_id)):
        coverage = coverage_by_id[clause_id]
        check = checks_by_id[clause_id]
        route = str(check.get("inspection_route", "")).strip()
        kind = _route_kind(route)
        required = str(check.get("required_evidence_class", "")).strip()
        ceiling = ROUTE_EVIDENCE_CEILINGS.get(kind, "")
        if required not in EVIDENCE_STRENGTH:
            issues.append(ProofContractIssue(
                "proof_evidence_class_unknown", clause_id,
                f"unknown required evidence class: {required!r}",
            ))
            continue
        if not ceiling:
            issues.append(ProofContractIssue(
                "proof_route_unavailable", clause_id,
                f"unsupported verifier route kind: {kind!r}",
            ))
            continue
        if _strength(ceiling) < _strength(required):
            issues.append(ProofContractIssue(
                "proof_route_strength_insufficient", clause_id,
                f"route {route!r} ceiling={ceiling} cannot establish required={required}",
            ))
            continue
        falsification = str(check.get("falsification_check", "")).strip()
        if not falsification:
            issues.append(ProofContractIssue(
                "proof_falsification_missing", clause_id,
                "critical clause has no falsification check",
            ))
            continue
        certified.append(CertifiedProofClause(
            clause_id=clause_id,
            requirement=str(coverage.get("verifier_check", "")).strip(),
            solver_handling=str(coverage.get("solver_handling", "")).strip(),
            verifier_route=route,
            fallback_route=str(check.get("fallback_route", "") or "").strip(),
            falsification_check=falsification,
            required_evidence_class=required,
            route_kind=kind,
            route_evidence_ceiling=ceiling,
            requires_independent_evidence=kind in INDEPENDENT_ROUTE_KINDS,
        ))
    return tuple(certified), tuple(issues)


def record_clause_evidence(
    ledger: ExecutionLedger,
    *,
    receipt_id: str,
    step: int,
    clause_id: str,
    route: str,
    evidence_class: str,
    provenance: str,
    supports_clause: bool,
    observation: str,
    state_generation: str = "",
    task_state_generation: int | None = None,
    inspection_ids: Sequence[str] = (),
) -> Receipt:
    """Record one current proof/disproof observation for a clause."""
    current_generation = (
        ledger.task_state_generation()
        if task_state_generation is None else int(task_state_generation)
    )
    receipt = Receipt(
        receipt_id=receipt_id,
        step=step,
        kind="proof_evidence",
        success=bool(supports_clause),
        summary=observation,
        failure_class="" if supports_clause else "clause_disproved",
        payload={
            "clause_id": clause_id,
            "route": route,
            "route_kind": _route_kind(route),
            "evidence_class": evidence_class,
            "provenance": provenance,
            "supports_clause": bool(supports_clause),
            "observation": observation,
            "state_generation": state_generation or str(current_generation),
            "task_state_generation": current_generation,
            "inspection_ids": [str(item) for item in inspection_ids if str(item).strip()],
        },
    )
    ledger.record(receipt)
    return receipt


def evaluate_proof_contract(
    clauses: Sequence[Mapping[str, Any] | CertifiedProofClause],
    ledger: ExecutionLedger,
) -> tuple[ClauseEvidenceDecision, ...]:
    """Evaluate current receipted proof without interpreting task semantics."""
    decisions: list[ClauseEvidenceDecision] = []
    current_generation = ledger.task_state_generation()
    proof_receipts = [receipt for receipt in ledger.all_receipts() if receipt.kind == "proof_evidence"]
    for raw_clause in clauses:
        clause = raw_clause if isinstance(raw_clause, CertifiedProofClause) else CertifiedProofClause(
            clause_id=str(raw_clause.get("clause_id", "")),
            requirement=str(raw_clause.get("requirement", "")),
            solver_handling=str(raw_clause.get("solver_handling", "")),
            verifier_route=str(raw_clause.get("verifier_route", "")),
            fallback_route=str(raw_clause.get("fallback_route", "")),
            falsification_check=str(raw_clause.get("falsification_check", "")),
            required_evidence_class=str(raw_clause.get("required_evidence_class", "")),
            route_kind=str(raw_clause.get("route_kind", "")),
            route_evidence_ceiling=str(raw_clause.get("route_evidence_ceiling", "")),
            requires_independent_evidence=bool(raw_clause.get("requires_independent_evidence", True)),
        )
        matching = [
            receipt for receipt in proof_receipts
            if str(receipt.payload.get("clause_id", "")).strip() == clause.clause_id
        ]
        if not matching:
            decisions.append(ClauseEvidenceDecision(
                clause.clause_id, False, "missing_clause_evidence",
                f"no proof evidence recorded for {clause.clause_id}",
            ))
            continue
        latest_disproof = next((receipt for receipt in reversed(matching) if not receipt.success), None)
        latest_support = next((receipt for receipt in reversed(matching) if receipt.success), None)
        if latest_disproof is not None and (
            latest_support is None or latest_disproof.step >= latest_support.step
        ):
            decisions.append(ClauseEvidenceDecision(
                clause.clause_id, False, "clause_disproved",
                latest_disproof.summary, (latest_disproof.receipt_id,),
            ))
            continue
        candidates: list[Receipt] = []
        stale_candidates: list[Receipt] = []
        allowed_routes = {clause.verifier_route}
        if clause.fallback_route:
            allowed_routes.add(clause.fallback_route)
        for receipt in matching:
            if not receipt.success:
                continue
            payload = receipt.payload
            if str(payload.get("route", "")).strip() not in allowed_routes:
                continue
            evidence_class = str(payload.get("evidence_class", "")).strip()
            if _strength(evidence_class) < _strength(clause.required_evidence_class):
                continue
            if clause.requires_independent_evidence:
                provenance = str(payload.get("provenance", "")).strip()
                if provenance not in INDEPENDENT_PROVENANCE:
                    continue
            try:
                evidence_generation = int(payload.get("task_state_generation"))
            except (TypeError, ValueError):
                evidence_generation = -1
            if evidence_generation != current_generation:
                stale_candidates.append(receipt)
                continue
            candidates.append(receipt)
        if not candidates:
            if stale_candidates:
                generations = sorted({
                    int(item.payload.get("task_state_generation", -1))
                    for item in stale_candidates
                })
                decisions.append(ClauseEvidenceDecision(
                    clause.clause_id,
                    False,
                    "stale_clause_evidence",
                    f"proof observed task generation(s) {generations}; current generation={current_generation}",
                    tuple(item.receipt_id for item in stale_candidates),
                ))
                continue
            strongest = max(
                (_strength(str(receipt.payload.get("evidence_class", ""))) for receipt in matching if receipt.success),
                default=-1,
            )
            decisions.append(ClauseEvidenceDecision(
                clause.clause_id, False, "insufficient_clause_evidence",
                (
                    f"required route={clause.verifier_route!r}, class={clause.required_evidence_class}; "
                    f"strongest observed rank={strongest}"
                ),
                tuple(receipt.receipt_id for receipt in matching),
            ))
            continue
        accepted = candidates[-1]
        decisions.append(ClauseEvidenceDecision(
            clause.clause_id, True, "clause_proved", accepted.summary,
            (accepted.receipt_id,),
        ))
    return tuple(decisions)


def _registered_inspection_support(
    ledger: ExecutionLedger,
    *,
    inspection_ids: Sequence[str],
    allowed_routes: set[str],
    evidence_class: str,
) -> Receipt | None:
    """Strongest successful registered inspection matching a proof route."""
    from .inspection_registry import inspection_records_by_id

    registry = inspection_records_by_id(ledger)
    candidates: list[Receipt] = []
    for inspection_id in inspection_ids:
        receipt = registry.get(str(inspection_id))
        if receipt is None or not receipt.success:
            continue
        payload = receipt.payload
        if not bool(payload.get("eligible_for_proof", False)):
            continue
        if str(payload.get("route", "")).strip() not in allowed_routes:
            continue
        if _strength(str(payload.get("evidence_ceiling", ""))) < _strength(evidence_class):
            continue
        candidates.append(receipt)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: _strength(str(item.payload.get("evidence_ceiling", ""))),
    )


def record_verifier_result_evidence(
    ledger: ExecutionLedger,
    *,
    result: Any,
    compiled: Any,
    step: int,
) -> tuple[Receipt, ...]:
    """Project validated Verifier clause evidence into durable proof receipts.

    ``verify_with_inspector`` already validates completion inspection refs
    before this point. This bridge does not reinterpret the Verifier's
    reasoning; it binds clause IDs and evidence classes to the compiler-owned
    route so the completion gate can enforce the typed contract.
    """
    contract_by_id = {
        str(row.get("clause_id", "")).strip(): row
        for row in getattr(compiled, "proof_contract", ())
        if isinstance(row, Mapping) and str(row.get("clause_id", "")).strip()
    }
    if not contract_by_id:
        return ()
    recorded: list[Receipt] = []

    if getattr(result, "verdict", "") == "completed":
        for entry_index, entry in enumerate(getattr(result, "completion_evidence", ())):
            inspection_ids = tuple(
                str(item).strip()
                for item in getattr(entry, "inspection_refs", ())
                if str(item).strip()
            )
            evidence_class = str(getattr(entry, "evidence_class", "")).strip()
            for clause_id in getattr(entry, "clause_ids", ()):
                clause_id = str(clause_id).strip()
                contract = contract_by_id.get(clause_id)
                if contract is None:
                    continue
                allowed_routes = {
                    str(contract.get("verifier_route", "")).strip(),
                    str(contract.get("fallback_route", "")).strip(),
                } - {""}
                chosen = _registered_inspection_support(
                    ledger,
                    inspection_ids=inspection_ids,
                    allowed_routes=allowed_routes,
                    evidence_class=evidence_class,
                )
                if chosen is None:
                    rejected = record_clause_evidence(
                        ledger,
                        receipt_id=f"step-{step}:proof:{clause_id}:rejected:{entry_index}",
                        step=step,
                        clause_id=clause_id,
                        route="unregistered_inspection",
                        evidence_class="model_claim",
                        provenance="verifier_inspection",
                        supports_clause=False,
                        observation=(
                            f"completion evidence for {clause_id} did not cite a successful "
                            "registered inspection on an allowed route"
                        ),
                        inspection_ids=inspection_ids,
                    )
                    rejected.payload.update({
                        "source": "model_verifier_completion_evidence_rejected",
                        "allowed_routes": sorted(allowed_routes),
                    })
                    recorded.append(rejected)
                    continue
                actual = chosen.payload
                receipt = record_clause_evidence(
                    ledger,
                    receipt_id=f"step-{step}:proof:{clause_id}:completed:{entry_index}",
                    step=step,
                    clause_id=clause_id,
                    route=str(actual.get("route", "")),
                    evidence_class=evidence_class,
                    provenance="verifier_inspection",
                    supports_clause=True,
                    observation=str(getattr(entry, "observed", "") or getattr(entry, "requirement", "")),
                    state_generation=str(actual.get("target_generation", "")),
                    task_state_generation=int(actual.get("task_state_generation", -1)),
                    inspection_ids=inspection_ids,
                )
                receipt.payload.update({
                    "inspection_refs": list(inspection_ids),
                    "selected_inspection_id": str(actual.get("inspection_id", chosen.receipt_id)),
                    "target_identity": str(actual.get("target_identity", "")),
                    "target_generation": str(actual.get("target_generation", "")),
                    "result_hash": str(actual.get("result_hash", "")),
                    "tool_identity": str(actual.get("tool_identity", "")),
                    "actual_evidence_ceiling": str(actual.get("evidence_ceiling", "")),
                    "falsification_check": str(getattr(entry, "falsification_check", "")),
                    "source": "model_verifier_completion_evidence",
                })
                recorded.append(receipt)
        return tuple(recorded)

    for finding_index, finding in enumerate(getattr(result, "findings", ())):
        targets = {
            str(target).strip()
            for target in getattr(finding, "applies_to", ())
            if str(target).strip()
        }
        for clause_id in sorted(targets & set(contract_by_id)):
            contract = contract_by_id[clause_id]
            observation_parts = [str(getattr(finding, "summary", "")).strip()]
            observation_parts.extend(str(item).strip() for item in getattr(finding, "evidence", ()) if str(item).strip())
            observation = "; ".join(part for part in observation_parts if part)
            receipt = record_clause_evidence(
                ledger,
                receipt_id=f"step-{step}:proof:{clause_id}:finding:{finding_index}",
                step=step,
                clause_id=clause_id,
                route=str(contract.get("verifier_route", "")),
                evidence_class=str(contract.get("required_evidence_class", "")),
                provenance="verifier_inspection",
                supports_clause=False,
                observation=observation or f"Verifier finding for {clause_id}",
            )
            receipt.payload.update({
                "finding_id": str(getattr(finding, "finding_id", "")),
                "repair_instruction": str(getattr(finding, "repair_instruction", "")),
                "source": "model_verifier_finding",
            })
            recorded.append(receipt)
    return tuple(recorded)
