"""Deterministic quality rubric for architect-only prompt/config evaluation."""
from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any

from .workbench_config import HarnessConfigIR


def _joined(values: list[str] | tuple[str, ...]) -> str:
    return "\n".join(str(item) for item in values if str(item).strip())


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _score(max_score: int, missing: list[str], warnings: list[str] | None = None) -> dict[str, Any]:
    warnings = warnings or []
    return {
        "score": max(0, max_score - len(_dedupe(missing))),
        "max_score": max_score,
        "missing": _dedupe(missing),
        "warnings": _dedupe(warnings),
    }


def score_solver_prompt(config: HarnessConfigIR | None) -> dict[str, Any]:
    if config is None:
        return {"score": 0, "max_score": 10, "missing": ["parseable HarnessConfigIR"], "warnings": []}
    prompt = config.solver_system_prompt
    rendered = prompt.render()
    lower = rendered.lower()
    missing: list[str] = []
    warnings: list[str] = []
    if _words(rendered) < 450:
        missing.append("solver_prompt_too_short_for_elite_contract")
    for field_name, values in (
        ("workflow", prompt.workflow),
        ("self_verification", prompt.self_verification),
        ("stop_conditions", prompt.stop_conditions),
        ("avoid", prompt.avoid),
    ):
        if not values:
            missing.append(field_name)
    if not config.success_definition.strip():
        missing.append("success_definition")
    if not config.evidence_requirements:
        missing.append("evidence_requirements")
    if not config.minimum_completion_evidence:
        missing.append("minimum_completion_evidence")
    if not any(marker in lower for marker in ("deliverable", "artifact", "/app/", "exact path", "output path")):
        missing.append("solver_prompt_names_artifact_or_path")
    if not any(marker in lower for marker in ("validate", "verify", "check", "run_check", "execute")):
        missing.append("solver_prompt_has_validation_language")
    if not prompt.stop_conditions or not any(marker in _joined(prompt.stop_conditions).lower() for marker in ("stop", "submit", "complete", "ready", "done")):
        missing.append("solver_prompt_has_completion_gate")
    if any(marker in lower for marker in ("verifier", "reviewer", "judge", "hidden test", "grader")):
        missing.append("solver_prompt_leaks_external_judgement_language")
    if not any(marker in lower for marker in ("completion finding", "completion issue", "state defect", "failed check", "repair and resubmit", "resubmit only after", "fresh evidence")):
        missing.append("solver_prompt_handles_completion_findings_or_failed_checks")
    avoid_lower = _joined(prompt.avoid).lower()
    if not prompt.avoid or not any(marker in avoid_lower for marker in ("do not", "don't", "avoid", "never")):
        missing.append("solver_prompt_has_do_not_submit_or_avoid_gate")
    manual_query_patterns = ("call query_memory", "use query_memory", "query_memory before")
    if any(pattern in lower for pattern in manual_query_patterns):
        missing.append("manual_query_memory_ritual_present")
    if "automatic memory" not in lower and "memory" not in lower:
        warnings.append("no_automatic_memory_guidance")
    return _score(10, missing, warnings)


def score_verifier_prompt(config: HarnessConfigIR | None) -> dict[str, Any]:
    if config is None:
        return {"score": 0, "max_score": 10, "missing": ["parseable HarnessConfigIR"], "warnings": []}
    prompt = config.verifier_system_prompt
    rendered = prompt.render()
    lower = rendered.lower()
    missing: list[str] = []
    if _words(rendered) < 300:
        missing.append("verifier_prompt_too_short_for_elite_contract")
    for field_name, values in (
        ("success_criteria", prompt.success_criteria),
        ("required_evidence", prompt.required_evidence),
        ("false_positive_traps", prompt.false_positive_traps),
        ("verdict_guidance", prompt.verdict_guidance),
        ("feedback_guidance", prompt.feedback_guidance),
    ):
        if not values:
            missing.append(field_name)
    for needle in (
        "completed",
        "needs_repair",
        "uncertain",
        "blocked_by_tooling",
        "blocked_by_harness_config",
        "evidence",
        "feedback",
        "read-only",
        "state",
        "inspect",
    ):
        if needle not in lower:
            missing.append(f"verifier_prompt_mentions_{needle}")
    if not any(marker in lower for marker in ("solver-authored", "self-report", "validation method", "method matches", "audit")):
        missing.append("verifier_prompt_audits_solver_authored_validation")
    if not config.false_positive_risks:
        missing.append("false_positive_risks")
    if not config.local_verification_limits:
        missing.append("local_verification_limits")
    return _score(10, missing)


def score_config_contract(config: HarnessConfigIR | None, realization_preview: dict[str, Any] | None = None) -> dict[str, Any]:
    if config is None:
        return {"score": 0, "max_score": 10, "missing": ["parseable HarnessConfigIR"], "warnings": []}
    missing: list[str] = []
    warnings: list[str] = []
    if not config.evidence_requirements:
        missing.append("evidence_requirements")
    if not config.false_positive_risks:
        missing.append("false_positive_risks")
    if not config.minimum_completion_evidence:
        missing.append("minimum_completion_evidence")
    if not config.verification_policy.solver_callable_checks:
        missing.append("solver_callable_checks")
    if not config.model_verifier_policy.enabled:
        missing.append("model_verifier_enabled")
    if tuple(config.model_verifier_policy.runs_on) != ("solver_submit",):
        missing.append("model_verifier_runs_on_solver_submit_only")
    if not config.local_verification_limits:
        missing.append("local_verification_limits")
    if realization_preview:
        audit = realization_preview.get("realization_audit", {})
        if isinstance(audit, dict) and audit.get("has_silent_ignored_fields"):
            missing.append("silent_ignored_config_fields")
        dispositions = audit.get("dispositions", {}) if isinstance(audit, dict) else {}
        verification = dispositions.get("verification_policy", {}) if isinstance(dispositions, dict) else {}
        if isinstance(verification, dict) and verification.get("smoke_compile_rejections"):
            missing.append("visible_smoke_compile_rejections")
        if not realization_preview.get("verifier_prompt_inserted"):
            missing.append("verifier_prompt_not_realized")
    smoke_tests = config.verification_policy.visible_smoke_tests
    text = _joined(config.evidence_requirements + config.minimum_completion_evidence).lower()
    if smoke_tests and all(str(item.get("type", "")) in {"content_assertion", "syntax_check"} for item in smoke_tests):
        if any(word in text for word in ("run", "execute", "semantic", "behavior", "browser", "service", "certificate", "query")):
            missing.append("behavioral_task_has_only_source_or_syntax_smoke")
    all_contract_text = "\n".join([
        config.task_understanding,
        config.success_definition,
        config.solver_system_prompt.render(),
        config.verifier_system_prompt.render(),
        _joined(config.evidence_requirements),
        _joined(config.false_positive_risks),
        _joined(config.minimum_completion_evidence),
        _joined(config.local_verification_limits),
    ]).lower()
    visual_terms = ("image", "ocr", "screenshot", "pixel", "visual", "png", "jpg", "jpeg", "video", "frame")
    if any(term in all_contract_text for term in visual_terms):
        if not any(term in all_contract_text for term in ("metadata-only", "metadata only", "semantic extraction", "tesseract", "ffmpeg", "pdftotext", "ocr", "vision")):
            missing.append("visual_task_missing_semantic_extraction_or_metadata_limit")
        if not any(term in all_contract_text for term in ("extract", "transcribe", "ocr", "semantic", "pixel", "frame")):
            missing.append("visual_task_missing_extraction_workflow")
    return _score(10, missing, warnings)


def score_architect_config(config: HarnessConfigIR | None, realization_preview: dict[str, Any] | None = None) -> dict[str, Any]:
    solver = score_solver_prompt(config)
    verifier = score_verifier_prompt(config)
    config_score = score_config_contract(config, realization_preview)
    score = round((solver["score"] + verifier["score"] + config_score["score"]) / 3, 2)
    return {
        "overall_score": score,
        "max_score": 10,
        "solver_prompt": solver,
        "verifier_prompt": verifier,
        "config_contract": config_score,
        "config_snapshot": None if config is None else {
            "solver_prompt_words": _words(config.solver_system_prompt.render()),
            "verifier_prompt_words": _words(config.verifier_system_prompt.render()),
            "evidence_requirement_count": len(config.evidence_requirements),
            "false_positive_risk_count": len(config.false_positive_risks),
            "minimum_completion_evidence_count": len(config.minimum_completion_evidence),
            "visible_smoke_test_count": len(config.verification_policy.visible_smoke_tests),
            "model_verifier_enabled": config.model_verifier_policy.enabled,
            "as_dict": asdict(config),
        },
    }
