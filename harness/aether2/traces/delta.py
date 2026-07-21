"""Workspace delta snapshots and durable evidence ledger helpers for HarnessEng Aether-2.

This module is the **stable public surface** for the traces package.
All implementation has been extracted into focused sibling modules:

- ``snapshot_diff``    — FileDelta / StateSnapshot / DeltaReport / snapshot / diff
- ``evidence_ledger``  — build_evidence_ledger / compact_evidence_ledger / record_*
- ``blockers``         — register_verifier_blockers / mark_blockers_* lifecycle helpers
- ``terminal_claims``  — record_terminal_claim / normalization helpers
- ``_text_utils``      — _clean_text / _coerce_int / _normalize_string_list / …

Every name that previously lived in this file is still importable from
``harness.aether2.traces.delta``.  External call-sites require no changes.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Snapshot / diff surface
# ---------------------------------------------------------------------------
from harness.aether2.traces.snapshot_diff import (
    DeltaReport,
    FileDelta,
    StateSnapshot,
    diff,
    snapshot,
    with_evidence_ledger,
)

# ---------------------------------------------------------------------------
# Evidence-ledger surface
# ---------------------------------------------------------------------------
from harness.aether2.traces.evidence_ledger import (
    build_evidence_ledger,
    compact_evidence_ledger,
    ensure_stated_requirements,
    record_check_results,
    record_observation_evidence,
    serialize_evidence_ledger,
    # internal helpers also imported by the rest of the ecosystem
    _new_requirement_entry,
    _ensure_requirement_entry,
    _requirement_map,
    _requirement_id,
    _normalize_requirement_status,
    _normalize_evidence_strength,
    _record_failure_family,
    _normalize_failure_families,
    _build_observation_ref,
    _build_check_ref,
    _failure_family_from_check,
    _blocker_requirement_summaries,
)

# ---------------------------------------------------------------------------
# Blocker surface
# ---------------------------------------------------------------------------
from harness.aether2.traces.blockers import (
    compute_relevant_evidence_version,
    mark_blockers_candidate_resolved,
    mark_blockers_exhausted,
    # internal helpers used transitively
    _blocker_map,
    _blocker_sort_key,
    _normalize_blocker,
    _normalize_blocker_status,
    _build_blocker_id,
    _build_verifier_ref,
    _upsert_blocker,
    _resolve_requirement_blockers,
    _obsolete_unseen_blockers,
    _iter_target_blockers,
    _compute_age_steps,
    _choose_step,
    _coerce_step,
    _extract_step_from_text,
)
from harness.aether2.traces._blocker_relevance import (
    _current_blocker_evidence_version,
    _has_relevant_new_evidence,
    _evidence_overlaps_blocker,
    _token_set,
)

# ---------------------------------------------------------------------------
# Verifier surface (verifier-report processing + evidence-class inference)
# ---------------------------------------------------------------------------
from harness.aether2.traces.verifier import (
    record_verifier_report,
    register_verifier_blockers,
    _infer_evidence_provenance,
    _normalize_terminal_evidence_class,
    _infer_evidence_classes,
    _iter_verifier_structure_findings,
)
from harness.aether2.traces._blocker_builders import (
    _build_blocker_from_requirement_finding,
    _build_blocker_from_structure_finding,
    _default_next_evidence,
)

# ---------------------------------------------------------------------------
# Terminal-claims surface
# ---------------------------------------------------------------------------
from harness.aether2.traces.terminal_claims import (
    record_terminal_claim,
    _normalize_terminal_claims,
    _normalize_terminal_claim,
    _normalize_terminal_requirement_claims,
    _normalize_terminal_claim_kind,
    _build_terminal_claim_id,
    _terminal_claim_sort_key,
)

# ---------------------------------------------------------------------------
# Text utilities re-exported for any downstream code that reaches in
# ---------------------------------------------------------------------------
from harness.aether2.traces._text_utils import (
    _TOKEN_RE,
    _RELEVANCE_STOPWORDS,
    _clean_text,
    _read_attr,
    _coerce_int,
    _normalize_string_list,
    _append_capped,
)

# ---------------------------------------------------------------------------
# Constants that callers may reference directly
# ---------------------------------------------------------------------------
_EVIDENCE_LEDGER_VERSION = 1
_VALID_REQUIREMENT_STATUSES = {"unproven", "partial", "proven", "contradicted"}
_VALID_EVIDENCE_STRENGTHS = {"none", "weak", "moderate", "strong"}
_VALID_BLOCKER_STATUSES = {"active", "candidate_resolved", "resolved", "obsolete", "exhausted"}
_REQUIREMENT_LIST_LIMITS = {
    "evidence_refs": 6,
    "evidence_provenance": 6,
    "failed_checks": 4,
    "disproven_assumptions": 4,
    "open_risks": 4,
    "verifier_blockers": 4,
    "next_required_evidence": 4,
}
_MAX_REQUIREMENTS = 24
_MAX_FAILURE_FAMILIES = 8
_MAX_BLOCKERS = 48
_MAX_TERMINAL_CLAIMS = 24
_BLOCKER_LIST_LIMITS = {
    "reason_codes": 6,
    "rejected_evidence_refs": 6,
    "rejected_evidence_provenance": 6,
    "required_next_evidence": 4,
}
_TERMINAL_CLAIM_LIST_LIMITS = {
    "requirements": 8,
    "evidence_refs": 6,
    "evidence_provenance": 6,
    "known_limitations": 6,
    "attempts": 6,
    "missing_external_state": 6,
    "recommended_next_evidence": 6,
}
_VERIFIER_INTEGRITY_REQUIREMENT = "verifier report integrity"
_IGNORED_DIR_NAMES = {
    ".git",
    ".aether2",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
_REGISTRY_FILENAMES = {
    "service_registry": ("service_registry.json",),
    "process_registry": ("process_registry.json",),
    "job_registry": ("job_registry.json", "jobs.json"),
    "session_registry": ("session_registry.json", "sessions.json"),
}

__all__ = [
    # snapshot_diff
    "DeltaReport",
    "FileDelta",
    "StateSnapshot",
    "diff",
    "snapshot",
    "with_evidence_ledger",
    # evidence_ledger
    "build_evidence_ledger",
    "compact_evidence_ledger",
    "ensure_stated_requirements",
    "record_check_results",
    "record_observation_evidence",
    "serialize_evidence_ledger",
    # blockers
    "compute_relevant_evidence_version",
    "mark_blockers_candidate_resolved",
    "mark_blockers_exhausted",
    "record_verifier_report",
    "register_verifier_blockers",
    # terminal_claims
    "record_terminal_claim",
]
