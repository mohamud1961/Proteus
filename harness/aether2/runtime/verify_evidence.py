"""Evidence classification helpers for fresh-context verification.

Extracted from verify.py to keep that module under 500 LOC.
These are internal helpers; callers should import from verify.py.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


def _stringify_source(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _first_catalog_key(source_catalog: Mapping[str, Any], *, prefix: str) -> str | None:
    matches = sorted(key for key in source_catalog if key.startswith(prefix))
    if not matches:
        return None
    return matches[0]


def _normalize_evidence_refs(raw_refs: Any) -> tuple[str, ...]:
    if not isinstance(raw_refs, list):
        return ()
    seen: set[str] = set()
    refs: list[str] = []
    for raw_ref in raw_refs:
        ref = str(raw_ref).strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
    return tuple(refs)


def _normalize_provenance_labels(raw_labels: Any) -> tuple[str, ...]:
    if not isinstance(raw_labels, list):
        return ()
    seen: set[str] = set()
    labels: list[str] = []
    for raw_label in raw_labels:
        label = str(raw_label).strip().lower()
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return tuple(labels)


def _extract_inline_refs(evidence: str, source_catalog: Mapping[str, Any]) -> tuple[str, ...]:
    refs = [ref for ref in source_catalog if ref and ref in evidence]
    return tuple(refs)


def _infer_evidence_refs(evidence: str, source_catalog: Mapping[str, Any]) -> tuple[str, ...]:
    lower = evidence.lower()
    inferred: list[str] = []
    if any(token in lower for token in ("check", "command", "pytest", "test", "exit code", "returned")) and "checks_results" in source_catalog:
        inferred.append("checks_results")
    if any(token in lower for token in ("workspace", "artifact", "file", "diff", "content")) and "workspace_diff" in source_catalog:
        inferred.append("workspace_diff")
    if any(token in lower for token in ("inspect", "inspection", "session", "job", "port", "service")):
        inspection_ref = _first_catalog_key(source_catalog, prefix="inspection.")
        if inspection_ref is not None:
            inferred.append(inspection_ref)
    if any(token in lower for token in ("tool", "action", "ran", "executed")) and "action_digest.tool_calls" in source_catalog:
        inferred.append("action_digest.tool_calls")
    if not inferred and "claim" in source_catalog:
        inferred.append("claim")
    return tuple(dict.fromkeys(inferred))


def _finalize_evidence_refs(raw_refs: Any, evidence: str, source_catalog: Mapping[str, Any]) -> tuple[str, ...]:
    refs = _normalize_evidence_refs(raw_refs)
    if not refs:
        refs = _extract_inline_refs(evidence, source_catalog)
    if not refs:
        refs = _infer_evidence_refs(evidence, source_catalog)
    return refs


def _looks_like_service_or_persistence_claim(corpus: str) -> bool:
    return _contains_any(
        corpus,
        (
            "service",
            "server",
            "daemon",
            "port",
            "socket",
            "listen",
            "listening",
            "startup probe",
            "health endpoint",
            "healthcheck",
            "pid",
            "process alive",
            "remained up",
            "stayed up",
            "survived",
            "restart",
            "replacement",
            "state persisted",
            "session still alive",
        ),
    )


def _has_bounded_survival_signal(corpus: str) -> bool:
    if _contains_any(
        corpus,
        (
            "survived for",
            "remained up for",
            "stayed up for",
            "rechecked after",
            "still running after",
            "still listening after",
            "across 2 probes",
            "across two probes",
            "across 3 probes",
            "across three probes",
            "over a 30s window",
            "over a 60s window",
            "bounded window",
            "second probe",
            "third probe",
            "later probe",
        ),
    ):
        return True
    return bool(re.search(r"\b(after|over)\s+\d+\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)\b", corpus))


def _has_correct_environment_probe(corpus: str) -> bool:
    return _contains_any(
        corpus,
        (
            "same workspace",
            "same working tree",
            "same cwd",
            "same container",
            "same environment",
            "same virtualenv",
            "same venv",
            "inside the project env",
            "inside the app env",
            "from the project env",
            "using the repo client",
            "using the project client",
            "using the app client",
            "from the workspace root",
        ),
    )


def _has_response_or_state_validation(corpus: str) -> bool:
    return _contains_any(
        corpus,
        (
            "response matched expected",
            "response body matched",
            "returned expected payload",
            "returned expected json",
            "validated response body",
            "validated service state",
            "state persisted",
            "wrote then read",
            "created then fetched",
            "same record",
            "same counter",
            "same value after restart check",
            "health response contained",
            "state endpoint matched",
        ),
    )


def _has_crash_or_replacement_signal(corpus: str) -> bool:
    return _contains_any(
        corpus,
        (
            "pid changed",
            "new pid",
            "different pid",
            "replacement pid",
            "replacement process",
            "restarted",
            "restart detected",
            "crashed",
            "died",
            "respawned",
            "replaced",
            "exit code",
            "restart count",
        ),
    )


def _service_monitoring_signals(corpus: str) -> dict[str, tuple[str, ...]]:
    weak: list[str] = []
    strong: list[str] = []
    if not _looks_like_service_or_persistence_claim(corpus):
        return {"weak": (), "strong": ()}
    if _contains_any(corpus, ("curl", "http", "request", "response", "client", "probe")) and not _has_bounded_survival_signal(corpus) and not _has_response_or_state_validation(corpus):
        weak.append("service_probe_without_survival_window")
    if _has_bounded_survival_signal(corpus):
        strong.append("bounded_survival_window")
    if _has_correct_environment_probe(corpus):
        strong.append("correct_environment_client_probe")
    if _has_response_or_state_validation(corpus):
        strong.append("response_or_state_validation")
    if _has_crash_or_replacement_signal(corpus):
        strong.append("crash_or_replacement_detected")
    return {"weak": tuple(weak), "strong": tuple(strong)}


def _service_positive_bundle(strong_reasons: list[str]) -> bool:
    strong_reason_set = set(strong_reasons)
    return "bounded_survival_window" in strong_reason_set and bool(
        strong_reason_set
        & {
            "correct_environment_client_probe",
            "response_or_state_validation",
            "client_interaction",
        }
    )


def _evidence_strength_confidence(strength: str, reasons: list[str], evidence_refs: tuple[str, ...]) -> str:
    high_signal_reasons = {
        "help_or_version_only",
        "command_presence_only",
        "import_only",
        "environment_or_path_mutation",
        "partial_test_selection_only",
        "swallowed_failure",
        "clean_execution",
        "independent_value_or_invariant_comparison",
        "artifact_parse_and_use",
        "client_interaction",
        "provided_checks_without_environment_hacks",
        "parse_or_schema_failure",
    }
    if any(reason in high_signal_reasons for reason in reasons):
        return "high"
    if strength == "mixed" or len(reasons) >= 2 or evidence_refs:
        return "medium"
    return "low"


def _classify_evidence_strength(
    *,
    requirement: str,
    verdict: str,
    evidence: str,
    evidence_refs: tuple[str, ...],
    source_catalog: Mapping[str, Any],
    report_reason_codes: list[str],
) -> dict[str, Any]:
    ref_texts = [_stringify_source(source_catalog.get(ref)) for ref in evidence_refs if ref in source_catalog]
    corpus = "\n".join(part for part in [requirement, evidence, *ref_texts] if part).lower()
    weak_reasons: list[str] = []
    strong_reasons: list[str] = []

    if "verifier_parse_failed" in report_reason_codes:
        weak_reasons.append("parse_or_schema_failure")
    if _contains_any(corpus, ("--help", "--version", "usage:", "version output")):
        weak_reasons.append("help_or_version_only")
    if _contains_any(corpus, ("command -v", "which ", "type -p", "type -a")):
        weak_reasons.append("command_presence_only")
    if _contains_any(corpus, ('python -c "import', "python3 -c \"import", "import-only", "import pass", "imports successfully")):
        weak_reasons.append("import_only")
    if _contains_any(corpus, ("schema", "shape", "count", "row count", "column count", "field count", "line count", "regex match", "matched pattern")):
        weak_reasons.append("shape_count_or_schema_only")
    if _contains_any(corpus, ("process alive", "pgrep", "pid", "port open", "listening on", "netstat", "lsof -i", "ss -l", "service alive")):
        weak_reasons.append("process_or_port_open_only")
    if _contains_any(corpus, ("startup probe", "startup-only", "first probe", "initial probe", "ready once", "boot probe", "initial readiness")):
        weak_reasons.append("startup_probe_only")
    if _contains_any(corpus, ("pythonpath", "path=", "ld_library_path", "virtual_env", "source venv", "sys.path", "export path", "export pythonpath")):
        weak_reasons.append("environment_or_path_mutation")
    if _contains_any(corpus, ("pytest -k", "::test", "::", "partial test", "selected test", "single test")):
        weak_reasons.append("partial_test_selection_only")
    if _contains_any(corpus, ("|| true", "|| :", "set +e", "ignored failure", "ignored exit code", "swallowed failure")):
        weak_reasons.append("swallowed_failure")
    if _contains_any(corpus, ("exists", "present", "contains", "found", "read-only", "read file", "ls ", "cat ", "head ", "tail ", "grep ")):
        weak_reasons.append("existence_or_read_only_observation")

    if _contains_any(corpus, ("exit code 0", "exited 0", "returned 0", "completed successfully", "passed cleanly", "no errors")):
        strong_reasons.append("clean_execution")
    if _contains_any(corpus, ("input", "output", "stdout", "stderr", "response body", "round-trip", "produced", "returned value")):
        strong_reasons.append("representative_io")
    if _contains_any(corpus, ("expected", "actual", "matches expected", "compared", "diff", "checksum", "hash", "invariant", "equal to", "validated against")):
        strong_reasons.append("independent_value_or_invariant_comparison")
    if _contains_any(corpus, ("parsed", "loaded", "decoded", "rendered", "opened and used", "deserialized", "compiled")):
        strong_reasons.append("artifact_parse_and_use")
    if _contains_any(corpus, ("curl", "http", "request", "response", "client", "connected to", "queried", "handshake")):
        strong_reasons.append("client_interaction")
    if _contains_any(corpus, ("session_read", "screen", "ui", "pane", "prompt appeared", "rendered page", "visible in session")):
        strong_reasons.append("observable_ui_or_session_behavior")
    if _contains_any(corpus, ("pytest", "cargo test", "go test", "npm test", "make test", "declared check", "provided check", "verification command")) and "environment_or_path_mutation" not in weak_reasons:
        strong_reasons.append("provided_checks_without_environment_hacks")

    service_signals = _service_monitoring_signals(corpus)
    weak_reasons.extend(service_signals["weak"])
    strong_reasons.extend(service_signals["strong"])

    weak_reasons = _dedupe(weak_reasons)
    strong_reasons = _dedupe(strong_reasons)
    dominant_weak_reasons = {
        "help_or_version_only",
        "command_presence_only",
        "import_only",
        "shape_count_or_schema_only",
        "process_or_port_open_only",
        "startup_probe_only",
        "service_probe_without_survival_window",
        "environment_or_path_mutation",
        "partial_test_selection_only",
        "swallowed_failure",
        "existence_or_read_only_observation",
    }
    lightweight_strong_reasons = {"clean_execution", "representative_io", "client_interaction"}
    service_positive_bundle = _service_positive_bundle(strong_reasons)
    instability_detected = "crash_or_replacement_detected" in strong_reasons
    if verdict in {"unsatisfied", "unverifiable"} and instability_detected:
        strength = "strong"
    elif service_positive_bundle and not instability_detected:
        strength = "strong"
    elif strong_reasons and weak_reasons and (
        set(weak_reasons) & dominant_weak_reasons
    ) and set(strong_reasons).issubset(lightweight_strong_reasons):
        strength = "weak"
    elif strong_reasons and weak_reasons:
        strength = "mixed"
    elif strong_reasons:
        strength = "strong"
    else:
        strength = "weak"

    reasons = strong_reasons + weak_reasons
    if not reasons:
        reasons = ["generic_assertion_only" if verdict == "satisfied" else "unresolved_without_decisive_evidence"]

    return {
        "strength": strength,
        "reasons": tuple(reasons),
        "confidence": _evidence_strength_confidence(strength, reasons, evidence_refs),
    }


def _classify_evidence_provenance(
    *,
    requirement: str,
    verdict: str,
    evidence: str,
    evidence_refs: tuple[str, ...],
    source_catalog: Mapping[str, Any],
    report_reason_codes: list[str],
    assessment: Mapping[str, Any],
) -> tuple[str, ...]:
    corpus = "\n".join(
        part for part in [requirement, evidence, *(_stringify_source(source_catalog.get(ref)) for ref in evidence_refs if ref in source_catalog)] if part
    ).lower()
    labels: list[str] = []

    if not evidence_refs or all(ref.startswith(("claim", "action_digest")) for ref in evidence_refs):
        labels.append("model_authored")
    if _contains_any(
        corpus,
        (
            "same method",
            "same heuristic",
            "same raw-byte",
            "same raw byte",
            "self-check",
            "self check",
            "self-authored",
            "self authored",
            "circular",
            "replayed",
            "same client",
        ),
    ):
        labels.append("same_method")
    if _contains_any(
        corpus,
        (
            "read back",
            "readback",
            "cat ",
            "head ",
            "tail ",
            "ls ",
            "exists",
            "present",
            "read-only observation",
            "read only observation",
            "file exists",
        ),
    ):
        labels.append("readback")
    if _contains_any(
        corpus,
        (
            "shape",
            "schema",
            "count",
            "row count",
            "column count",
            "field count",
            "tuple",
            "matrix",
            "dimensions",
        ),
    ):
        labels.append("shape")
    if _contains_any(
        corpus,
        (
            "command -v",
            "which ",
            "type -p",
            "type -a",
            "--help",
            "--version",
            "import ",
            "imports successfully",
            "startup probe",
            "first probe",
            "partial test",
            "selected test",
            "|| true",
        ),
    ):
        labels.append("proxy")

    strong_reasons = {str(reason) for reason in (assessment.get("reasons", ()) or ())}
    if strong_reasons & {
        "independent_value_or_invariant_comparison",
        "client_interaction",
        "provided_checks_without_environment_hacks",
        "bounded_survival_window",
        "response_or_state_validation",
        "crash_or_replacement_detected",
    }:
        labels.append("independent")

    if not labels:
        if verdict == "satisfied":
            labels.append("model_authored")
        else:
            labels.append("proxy")

    return tuple(_dedupe(labels))


def _has_clean_support(
    *,
    requirement: str,
    verdict: str,
    strength: str,
    evidence_provenance: tuple[str, ...],
    evidence_strength_reasons: tuple[str, ...],
) -> bool:
    lowered_requirement = requirement.strip().lower()
    if lowered_requirement.startswith("[inferred]") or lowered_requirement.startswith("[watchpoint]"):
        return True
    if verdict != "satisfied":
        return False
    if strength == "strong":
        return True
    if strength == "mixed":
        return "independent" in evidence_provenance
    high_signal_reasons = {
        "clean_execution",
        "independent_value_or_invariant_comparison",
        "artifact_parse_and_use",
        "client_interaction",
        "provided_checks_without_environment_hacks",
        "bounded_survival_window",
        "response_or_state_validation",
        "crash_or_replacement_detected",
    }
    if "independent" in evidence_provenance and high_signal_reasons.intersection(evidence_strength_reasons):
        return True
    return False
