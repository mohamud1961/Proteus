"""Trusted verifier recovery and semantic evidence gates.

This module is deliberately small and canonical: it operates on the existing
``ModelVerifierResult``/``VerifierInspectionRequest`` protocol and does not
implement a second runtime.  The kernel owns route execution; this module
only supplies deterministic ownership, retry, and evidence decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from .verifier import ModelVerifierResult


class VerifierRecoveryAction(str, Enum):
    RETRY_VERIFIER = "retry_verifier"
    RECONFIGURE = "reconfigure"
    TERMINAL_INFRASTRUCTURE = "terminal_infrastructure"
    RETURN_TO_SOLVER = "return_to_solver"
    TERMINATE_SUCCESS = "terminate_success"


class EvidenceClass(str, Enum):
    """Ordered evidence strengths used by compiled semantic gates."""

    SHAPE = "shape"
    METADATA_PROXY = "metadata_proxy"
    SOLVER_AUTHORED_TEST = "solver_authored_test"
    SAME_METHOD = "same_method"
    BEHAVIORAL = "behavioral"
    EXACT_CONTRACT = "exact_contract"
    INDEPENDENT_SEMANTIC = "independent_semantic"


_EVIDENCE_RANK = {item: index for index, item in enumerate(EvidenceClass)}


@dataclass(frozen=True)
class CompiledEvidenceRequirement:
    clause_id: str
    minimum_class: EvidenceClass


@dataclass(frozen=True)
class EvidenceValidationError:
    code: str
    message: str


@dataclass(frozen=True)
class RouteAttempt:
    route: str
    success: bool
    inspection_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class RecoveryResult:
    attempts: tuple[RouteAttempt, ...]
    action: VerifierRecoveryAction


def execute_primary_then_fallback(
    *,
    primary_route: str,
    fallback_route: str | None,
    executor: Callable[[str], Any],
    inspection_id_factory: Callable[[str], str] | None = None,
) -> tuple[RouteAttempt, ...]:
    """Execute exactly one compiled fallback after a primary failure.

    A failed primary is never converted into Solver feedback.  The returned
    attempts are receipts for the Verifier lane and the caller decides whether
    a packet retry is still available.
    """
    if not str(primary_route).strip():
        raise ValueError("primary verifier route must be non-empty")
    attempts: list[RouteAttempt] = []
    routes = [str(primary_route)]
    if fallback_route and str(fallback_route).strip() and str(fallback_route) != str(primary_route):
        routes.append(str(fallback_route))
    for route in routes:
        inspection_id = inspection_id_factory(route) if inspection_id_factory else ""
        try:
            result = executor(route)
        except Exception as exc:  # route failure stays verifier-owned
            attempts.append(RouteAttempt(route, False, inspection_id, f"{type(exc).__name__}: {exc}"))
            continue
        # ``None`` is the historical executor success sentinel, so only an
        # explicit false status denotes a failed route.  Do not report
        # fallback success when a boolean executor rejects the route.
        if result is False:
            attempts.append(RouteAttempt(route, False, inspection_id, "route returned failure status"))
            continue
        attempts.append(RouteAttempt(route, True, inspection_id))
        break
    return tuple(attempts)


@dataclass
class VerifierRecoveryRouter:
    """Bounded routing keyed by packet signature and failure owner."""

    max_packet_retries: int = 1
    _retry_counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_packet_retries < 0:
            raise ValueError("max_packet_retries must be non-negative")

    def route(
        self,
        result: ModelVerifierResult,
        *,
        packet_signature: str,
        blocker_owner: str = "",
        blocker_verified: bool = False,
        allowed_reconfigure_owners: Iterable[str] = (),
    ) -> VerifierRecoveryAction:
        """Return the only legal next owner for a verifier outcome.

        ``needs_repair`` is the sole outcome that can return to Solver.  Tool,
        protocol, provider, and harness failures remain outside Solver.  A
        reconfiguration is legal only when the owner is explicitly allowed
        *and* the blocker has a verified receipt/evidence marker.
        """
        verdict = str(result.verdict)
        if verdict == "completed":
            return VerifierRecoveryAction.TERMINATE_SUCCESS
        if verdict in {"needs_repair", "incomplete_state_wrong", "incomplete_missing_required_artifact", "incomplete_semantic_mismatch"}:
            return VerifierRecoveryAction.RETURN_TO_SOLVER
        owner = blocker_owner.strip() or (
            "verifier_tooling" if verdict in {"blocked_by_tooling", "reviewer_tool_execution_failed", "reviewer_capability_missing"}
            else "harness_config" if verdict == "blocked_by_harness_config" else "protocol"
        )
        key = (str(packet_signature), owner)
        used = self._retry_counts.get(key, 0)
        if used < self.max_packet_retries:
            self._retry_counts[key] = used + 1
            return VerifierRecoveryAction.RETRY_VERIFIER
        if blocker_verified and owner in {str(item).strip() for item in allowed_reconfigure_owners}:
            return VerifierRecoveryAction.RECONFIGURE
        return VerifierRecoveryAction.TERMINAL_INFRASTRUCTURE


def validate_compiled_evidence(
    evidence: Iterable[Any],
    *,
    requirements: Iterable[CompiledEvidenceRequirement],
    known_inspection_ids: Iterable[str],
    inspection_ceilings: Mapping[str, EvidenceClass | str] | None = None,
) -> tuple[EvidenceValidationError, ...]:
    """Validate completed evidence against compiled clause thresholds.

    The gate is content-blind about the observation text, but it is strict on
    provenance, clause coverage, evidence class, and tool ceilings.  Shape,
    metadata proxies, and solver-authored self-tests cannot satisfy stronger
    semantic clauses merely by being present.
    """
    known = {str(item).strip() for item in known_inspection_ids if str(item).strip()}
    ceilings = dict(inspection_ceilings or {})
    requirement_rows = tuple(requirements)
    known_clause_ids = {str(item.clause_id).strip() for item in requirement_rows if str(item.clause_id).strip()}
    errors: list[EvidenceValidationError] = []
    by_clause: dict[str, list[tuple[EvidenceClass, Any]]] = {}
    for index, item in enumerate(evidence):
        refs = tuple(str(ref).strip() for ref in (getattr(item, "inspection_refs", ()) or ()) if str(ref).strip())
        clause_ids = tuple(str(value).strip() for value in (getattr(item, "clause_ids", ()) or ()) if str(value).strip())
        evidence_class_raw = str(getattr(item, "evidence_class", "") or "").strip()
        if not refs:
            errors.append(EvidenceValidationError("missing_inspection_id", f"evidence[{index}] requires inspection_refs"))
        unknown = sorted(set(refs) - known)
        if unknown:
            errors.append(EvidenceValidationError("unknown_inspection_id", f"evidence[{index}] cites unknown inspection IDs: {unknown}"))
        if not str(getattr(item, "falsification_check", "") or "").strip():
            errors.append(EvidenceValidationError("missing_falsification", f"evidence[{index}] requires falsification_check"))
        try:
            claimed = EvidenceClass(evidence_class_raw)
        except ValueError:
            errors.append(EvidenceValidationError("unknown_evidence_class", f"evidence[{index}] has unknown class: {evidence_class_raw}"))
            continue
        for ref in refs:
            ceiling_raw = ceilings.get(ref)
            if ceiling_raw is None or not str(ceiling_raw).strip():
                errors.append(EvidenceValidationError(
                    "missing_inspection_ceiling",
                    f"inspection {ref} has no registered evidence ceiling",
                ))
                continue
            try:
                ceiling = ceiling_raw if isinstance(ceiling_raw, EvidenceClass) else EvidenceClass(str(ceiling_raw))
            except ValueError:
                errors.append(EvidenceValidationError("unknown_ceiling", f"inspection {ref} has unknown evidence ceiling: {ceiling_raw}"))
                continue
            if _EVIDENCE_RANK[claimed] > _EVIDENCE_RANK[ceiling]:
                errors.append(EvidenceValidationError("evidence_ceiling_exceeded", f"evidence class {claimed.value} exceeds inspection {ref} ceiling {ceiling.value}"))
        for clause_id in clause_ids:
            if known_clause_ids and clause_id not in known_clause_ids:
                errors.append(EvidenceValidationError(
                    "unknown_clause_id",
                    f"evidence[{index}] cites unknown clause ID: {clause_id}",
                ))
            by_clause.setdefault(clause_id, []).append((claimed, item))
    for requirement in requirement_rows:
        candidates = by_clause.get(requirement.clause_id, ())
        if not candidates:
            errors.append(EvidenceValidationError("missing_clause_evidence", f"completed lacks evidence for clause {requirement.clause_id}"))
            continue
        strongest = max(_EVIDENCE_RANK[claimed] for claimed, _item in candidates)
        if strongest < _EVIDENCE_RANK[requirement.minimum_class]:
            errors.append(EvidenceValidationError("weak_evidence", f"clause {requirement.clause_id} requires {requirement.minimum_class.value}; weaker/proxy evidence supplied"))
    return tuple(errors)


def findings_for_solver_context(result: ModelVerifierResult) -> list[dict[str, Any]]:
    """Project only conclusive Solver-state findings into the next context."""
    if result.verdict == "completed":
        return []
    if result.verdict not in {"needs_repair", "incomplete_state_wrong", "incomplete_missing_required_artifact", "incomplete_semantic_mismatch"}:
        return []
    return [
        {
            "finding_id": finding.finding_id,
            "summary": finding.summary,
            "evidence": list(finding.evidence),
            "repair_instruction": finding.repair_instruction,
            "applies_to": list(finding.applies_to),
        }
        for finding in result.findings
    ]
