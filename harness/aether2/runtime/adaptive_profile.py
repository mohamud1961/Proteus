"""Standalone adaptive harness profile generator for the AHP variant."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from harness.aether2.runtime.adaptive_profile_helpers import (
    GRADER_LEAK_KEYS,
    attempt_json_repair as _attempt_json_repair_impl,
    compact_tool_catalogue as _compact_tool_catalogue,
    parse_profile_response,
    solver_visible_orientation as _solver_visible_orientation,
    strip_grader_keys as _strip_grader_keys,
)

PROFILE_VERSION = "ahp_v0"


class AgentInitializationFailure(RuntimeError):
    """Architect/workbench configuration failed before a task attempt existed."""

    def __init__(self, message: str, *, reason_code: str = "agent_initialization_failure") -> None:
        super().__init__(message)
        self.reason_code = reason_code

REQUIRED_PROFILE_FIELDS = frozenset({
    "task_understanding",
    "solver_system_prompt",
    "context_configuration",
    "context_pack_policy",
    "tool_configuration",
    "success_definition",
    "hard_visible_requirements",
    "inferred_success_requirements",
    "verification_watchpoints",
    "verification_configuration",
    "repeat_action_guidance",
    "approach_risks",
    "pivot_signals",
    "initial_plan",
    "compaction_recommendation",
})

# Fields that are new authority-level additions; their absence downgrades
# to a warning (not an error) so that older model responses degrade gracefully.
_AUTHORITY_LEVEL_FIELDS = frozenset({
    "hard_visible_requirements",
    "inferred_success_requirements",
    "verification_watchpoints",
    "uncertain_or_exploratory_risks",
    "do_not_assume",
    "initial_plan",
})

ALLOWED_PROFILE_FIELDS = REQUIRED_PROFILE_FIELDS | _AUTHORITY_LEVEL_FIELDS | frozenset({"profile_version"})
SUPPORTED_TOOL_CONFIGURATION_FIELDS = frozenset({"primary_tools", "reserve_capabilities"})
SUPPORTED_CONTEXT_CONFIGURATION_FIELDS = frozenset({"preserve", "deprioritise"})
SUPPORTED_COMPACTION_RECOMMENDATION_FIELDS = frozenset({"preserve", "deprioritise"})
SUPPORTED_VERIFICATION_CONFIGURATION_FIELDS = frozenset({
    "model_verifier_focus",
    "required_final_evidence",
    "immediate_feedback_rounds",
    "final_rounds",
    "verifier_system_prompt",
    "model_verifier_prompt",
})

TOOL_CONFIGURATION_REQUIRED = frozenset({"primary_tools"})

KNOWN_NATIVE_TOOLS = frozenset({
    "run_command",
    "start_job",
    "job_status",
    "session_start",
    "session_send",
    "session_read",
    "read_file",
    "write_file",
    "wait",
    "task_done",
    "task_blocked",
    "query_evidence",
    "query_history",
})

# Tools that the solver must always have access to.
MANDATORY_SOLVER_TOOLS = frozenset({"run_command", "read_file", "write_file", "task_done", "task_blocked"})

# Negation prefixes that flip the meaning of the following verb.
# When these appear immediately before a flagged verb phrase, the match is
# a *safe* instruction (e.g. "do not skip verification") and must not fire.
_NEGATION_PREFIX = r"(?:do\s+not|don'?t|never|must\s+not|should\s+not|shouldn'?t|cannot|can'?t|avoid)\s+"

_UNSAFE_PROMPT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("weakens_no_fake_work", re.compile(
        r"(?:ignore|skip|bypass|disable)\s+(?:no.fake.work|evidence|verification|proof)",
        re.IGNORECASE,
    )),
    ("tells_solver_skip_verification", re.compile(
        r"(?:skip|omit|bypass|disable|do\s+not\s+run)\s+(?:verif|check|test|valid)",
        re.IGNORECASE,
    )),
    ("disables_cleanup", re.compile(
        r"(?:skip|ignore|disable|do\s+not)\s+(?:clean.?up|teardown|shutdown)",
        re.IGNORECASE,
    )),
    ("ignores_tool_errors", re.compile(
        r"(?:ignore|suppress|swallow|discard)\s+(?:error|failure|exit.code|stderr)",
        re.IGNORECASE,
    )),
    ("fakes_checks", re.compile(
        r"(?:fake|fabricate|forge|invent|stub)\s+(?:check|test|result|evidence|output|proof)",
        re.IGNORECASE,
    )),
    ("encourages_premature_done", re.compile(
        r"(?:finish|complete|done|submit)\s+(?:as\s+soon\s+as|immediately|without\s+evidence|before\s+check)",
        re.IGNORECASE,
    )),
]

_STARTUP_SYSTEM_PROMPT = """\
You are a run configuration assistant. Your job is to analyze the visible \
task and environment, then produce a structured run configuration for an \
executor agent. You do NOT solve the task. You configure how the solver \
should approach it.

You will receive:
1. The visible task instruction.
2. A factual environment orientation snapshot.
3. The available tool catalogue.

Produce a single JSON object matching the profile schema described below. \
Do not solve the task. Do not execute any actions. Only configure the run.

COMPACTNESS RULE: every field must be task-specific and operational. \
Generic boilerplate (e.g. "follow instructions carefully", "be thorough") \
adds no signal and must be omitted. Hard requirement items must name \
concrete deliverables or acceptance criteria from the visible task, not \
restate the instruction. solver_system_prompt must give the solver \
actionable orientation for THIS task, not generic agent advice.

CATEGORY COVERAGE: when defining success criteria and watchpoints, \
explicitly consider each of these categories where relevant to the task: \
(a) dependencies/importability/setup — are required packages, modules, or \
tools installed and importable? (b) exact output format/types/ordering/ \
whitespace/trailing-newline — does the task specify a format contract? \
(c) exact I/O behaviour — specific inputs that must produce specific outputs. \
(d) error and edge-case handling — what happens with bad input or boundary \
conditions? (e) service readiness/real-client-interaction/survival/cleanup — \
if a service must run, can a real client connect? Does it survive and shut \
down cleanly? (f) persistence/durability — must state survive restarts or \
be recoverable? (g) command fidelity — where exact invocation or command \
strings matter, name them.

DEPENDENCY AWARENESS: use the env map for available runtimes and package \
managers, but do NOT assume project dependencies are installed just because \
a package manager exists. Treat dependency readiness/importability as a \
watchpoint unless the env map proves it satisfied.

AUTHORITY SEPARATION: separate direct visible requirements from inferred \
watchpoints. Do not present guesses as hard requirements. If a value, \
path, expected output, or hidden state is not visible in the task or \
workspace files, state the PROPERTY to verify rather than inventing the \
specific value. Place invented specifics in inferred or watchpoint \
sections, never in hard_visible_requirements.

TOOL SELECTION: primary_tools must list only tool names that appear in the \
provided tool catalogue. Any capability you want the solver to have that \
is not in the catalogue (e.g. domain-specific abilities, external APIs) \
should go in reserve_capabilities as descriptive strings, not in primary_tools.

Output ONLY valid JSON. No markdown fences, no explanation text outside \
the JSON object."""

_STARTUP_USER_TEMPLATE = """\
## Visible task instruction
{task_instruction}

## Environment orientation (solver-visible fields only)
{orientation_json}

## Available tool catalogue
{tool_catalogue_json}

## Profile schema
Produce a JSON object with these required fields:
- "task_understanding": {{"summary": str, "important_properties": [str], "initial_working_theory": str}}
- "solver_system_prompt": full system prompt for the solver; must encourage evidence-based work, \
allow pivots, include no-blind-repeat principle, not solve the task, not disable verification/cleanup
- "context_configuration": {{"preserve": [str], "deprioritise": [str]}}
- "context_pack_policy": {{"include_sections": [str], "always_include": [str], "exclude_sections": [str], \
"full_previous_steps": int, "receipt_event_budget": int, "failure_event_budget": int, \
"tool_result_budget": int, "verifier_feedback_budget": int, "artifact_observation_budget": int}}. \
Allowed sections: success_contract, current_plan, open_requirements, recent_steps, recent_failures, \
verifier_feedback, task_local_tools, artifact_observations, evidence_refs, active_jobs. Never request \
hidden_grader_refs, external_history, private_reasoning, raw_unrestricted_transcript, or raw_full_transcript.
- "tool_configuration": {{"primary_tools": [tool names from catalogue], "reserve_capabilities": [optional]}}
- "success_definition": [str] concrete success criteria (kept for back-compat; union of hard + inferred below)
- "hard_visible_requirements": [str] directly stated in the task text or visible workspace files — \
only include criteria you can point to a specific line or file for
- "inferred_success_requirements": [str] likely required but not directly guaranteed by visible text — \
reasonable inferences about what success probably requires
- "verification_watchpoints": [str] things the solver should inspect or monitor, NOT hard pass/fail — \
includes dependency readiness, format edge cases, service liveness, cleanup
- "uncertain_or_exploratory_risks": [str] journey risks, unknowns, things that might go wrong
- "do_not_assume": [str] values, paths, expected outputs, or hidden state the configurator \
is NOT confident about — name the property, not an invented value
- "verification_configuration": {{"model_verifier_focus": [str], "required_final_evidence": [str], \
"immediate_feedback_rounds": int optional, "final_rounds": int optional}}
- "repeat_action_guidance": str (when to avoid blind repeats)
- "approach_risks": [str] things about the chosen approach that could go wrong or need adjustment
- "pivot_signals": [str] (evidence that should trigger strategy change)
- "initial_plan": [{{"step": str, "status": "pending", "evidence_needed": str}}] short starting \
checklist (max 5 steps) the solver can follow and revise — a guide, not a rigid script
- "compaction_recommendation": {{"preserve": [str], "deprioritise": [str]}}"""


_SCHEMA_REPAIR_SYSTEM_PROMPT = """\
You repair run-configuration JSON for an executor agent. Return ONLY a single \
valid JSON object. Do not add markdown fences or commentary. Preserve the \
original task-specific intent, but add or correct fields needed to satisfy the \
schema. Do not solve the task."""


def _schema_repair_user_prompt(
    *,
    profile: dict[str, Any],
    validation_errors: list[str],
    task_instruction: str,
    orientation_dict: dict[str, Any],
    tool_catalogue: list[dict[str, Any]],
) -> str:
    """Build a bounded schema-repair prompt for parsed-but-invalid AHP JSON."""
    visible_orientation = _solver_visible_orientation(orientation_dict)
    compact_catalogue = _compact_tool_catalogue(tool_catalogue)
    return (
        "The previous JSON parsed successfully but failed schema validation.\n"
        "Repair it so every required field is present with the correct shape.\n"
        "Use only tool names from the available tool catalogue in "
        "tool_configuration.primary_tools.\n"
        "Missing/invalid fields:\n"
        + json.dumps(validation_errors, indent=2, ensure_ascii=True)
        + "\n\nVisible task instruction:\n"
        + task_instruction[:4000]
        + "\n\nEnvironment orientation (solver-visible fields only):\n"
        + json.dumps(visible_orientation, indent=2, sort_keys=True, ensure_ascii=True)[:4000]
        + "\n\nAvailable tool catalogue:\n"
        + json.dumps(compact_catalogue, indent=2, sort_keys=True, ensure_ascii=True)
        + "\n\nProfile schema reminder:\n"
        + _STARTUP_USER_TEMPLATE.split("## Profile schema\n", 1)[1]
        + "\n\nOriginal parsed JSON to repair:\n"
        + json.dumps(profile, indent=2, sort_keys=True, ensure_ascii=True)[:12000]
    )


def _attempt_schema_repair(
    *,
    profile: dict[str, Any],
    validation_errors: list[str],
    task_instruction: str,
    orientation_dict: dict[str, Any],
    tool_catalogue: list[dict[str, Any]],
    model_client: Any,
    available_tools: frozenset[str],
) -> tuple[dict[str, Any] | None, ProfileValidationResult, dict[str, int], float]:
    """Ask the model once to repair schema-invalid AHP JSON.

    This is not a fallback: if the repaired profile is still invalid, the
    caller surfaces the validation failure. The repair is bounded to converting
    parsed-but-incomplete JSON into the declared schema.
    """
    messages = [
        {"role": "system", "content": _SCHEMA_REPAIR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _schema_repair_user_prompt(
                profile=profile,
                validation_errors=validation_errors,
                task_instruction=task_instruction,
                orientation_dict=orientation_dict,
                tool_catalogue=tool_catalogue,
            ),
        },
    ]
    t0 = time.monotonic()
    response = model_client.call(messages, [], cache_prefix_len=0)
    duration = time.monotonic() - t0
    raw_text = response.text if hasattr(response, "text") else str(response)
    usage = dict(response.usage) if hasattr(response, "usage") else {}
    repaired = parse_profile_response(raw_text)
    if repaired is None:
        return None, ProfileValidationResult(valid=False, errors=["schema repair response was not valid JSON"]), usage, duration
    validation = validate_profile(repaired, available_tools)
    return repaired, validation, usage, duration


def build_startup_messages(
    task_instruction: str,
    orientation_dict: dict[str, Any],
    tool_catalogue: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the messages list for the startup model call."""
    # Filter orientation to solver-visible fields only
    solver_visible_orientation = _solver_visible_orientation(orientation_dict)
    # Build compact tool catalogue (names + descriptions only)
    compact_catalogue = _compact_tool_catalogue(tool_catalogue)

    user_content = _STARTUP_USER_TEMPLATE.format(
        task_instruction=task_instruction,
        orientation_json=json.dumps(
            solver_visible_orientation,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        ),
        tool_catalogue_json=json.dumps(
            compact_catalogue,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        ),
    )
    return [
        {"role": "system", "content": _STARTUP_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


@dataclass(frozen=True)
class ProfileValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_fallback: bool = False


def validate_profile(
    profile: dict[str, Any],
    available_tools: frozenset[str] | None = None,
) -> ProfileValidationResult:
    """Validate a parsed profile against the schema and safety rules."""
    errors: list[str] = []
    warnings: list[str] = []

    if available_tools is None:
        available_tools = KNOWN_NATIVE_TOOLS

    for field_name in sorted(set(profile) - ALLOWED_PROFILE_FIELDS):
        errors.append(f"unsupported profile field: {field_name}")

    # Check required fields — authority-level fields downgrade to warnings
    for required_field in REQUIRED_PROFILE_FIELDS:
        if required_field not in profile:
            if required_field in _AUTHORITY_LEVEL_FIELDS:
                warnings.append(f"missing authority-level field: {required_field}")
            else:
                errors.append(f"missing required field: {required_field}")

    # Validate tool_configuration
    tool_config = profile.get("tool_configuration", {})
    if isinstance(tool_config, dict):
        for field_name in sorted(set(tool_config) - SUPPORTED_TOOL_CONFIGURATION_FIELDS):
            errors.append(f"unsupported tool_configuration field: {field_name}")
        primary_tools = tool_config.get("primary_tools", [])
        if not isinstance(primary_tools, list):
            errors.append("tool_configuration.primary_tools must be a list")
        elif not primary_tools:
            errors.append("tool_configuration.primary_tools must not be empty")
        else:
            for tool_name in primary_tools:
                if tool_name not in available_tools:
                    warnings.append(
                        f"tool_configuration.primary_tools contains "
                        f"unknown tool '{tool_name}' — move to "
                        f"reserve_capabilities"
                    )
            for mandatory in MANDATORY_SOLVER_TOOLS:
                if mandatory not in primary_tools:
                    warnings.append(
                        f"tool_configuration.primary_tools should include "
                        f"mandatory tool: {mandatory}"
                    )
    else:
        errors.append("tool_configuration must be an object")

    for object_name, supported_fields in (
        ("context_configuration", SUPPORTED_CONTEXT_CONFIGURATION_FIELDS),
        ("compaction_recommendation", SUPPORTED_COMPACTION_RECOMMENDATION_FIELDS),
        ("verification_configuration", SUPPORTED_VERIFICATION_CONFIGURATION_FIELDS),
    ):
        value = profile.get(object_name, {})
        if isinstance(value, dict):
            for field_name in sorted(set(value) - supported_fields):
                errors.append(f"unsupported {object_name} field: {field_name}")
        elif value is not None:
            errors.append(f"{object_name} must be an object")

    # Validate solver_system_prompt is a non-empty string
    prompt = profile.get("solver_system_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        errors.append("solver_system_prompt must be a non-empty string")

    # Validate success_definition is a non-empty list
    success_def = profile.get("success_definition")
    if not isinstance(success_def, list) or not success_def:
        errors.append("success_definition must be a non-empty list")

    # Validate task_understanding has required subfields
    task_understanding = profile.get("task_understanding")
    if isinstance(task_understanding, dict):
        if not task_understanding.get("summary"):
            warnings.append("task_understanding.summary is empty")
    elif task_understanding is not None:
        errors.append("task_understanding must be an object")

    # Validate approach_risks is a list
    approach_risks = profile.get("approach_risks")
    if approach_risks is not None and not isinstance(approach_risks, list):
        errors.append("approach_risks must be a list")

    return ProfileValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


@dataclass(frozen=True)
class PromptLintFinding:
    code: str
    pattern: str
    matched_text: str


def lint_solver_prompt(prompt: str) -> list[PromptLintFinding]:
    """Check the generated solver prompt for unsafe patterns.

    Negation-aware: phrases like "do not skip verification" or
    "do not disable, bypass, or skip verification" are safe instructions
    and are excluded from the findings.
    """
    negation_re = re.compile(_NEGATION_PREFIX, re.IGNORECASE)
    # Conjunction filler: commas, "and", "or" + words between negation
    # and the flagged verb, within the same sentence.
    _conj_filler = re.compile(
        r"^[\w,\s]*(?:,\s*(?:or|and)\s+)?$", re.IGNORECASE,
    )
    findings: list[PromptLintFinding] = []
    for code, pattern in _UNSAFE_PROMPT_PATTERNS:
        for match in pattern.finditer(prompt):
            # Walk back to the last sentence boundary (. ! ? or start).
            clause_start = max(0, match.start() - 120)
            for j in range(match.start() - 1, clause_start - 1, -1):
                if j >= 0 and prompt[j] in ".!?\n":
                    clause_start = j + 1
                    break
            clause_prefix = prompt[clause_start : match.start()]
            # Check if a negation appears anywhere in this clause prefix
            neg_match = negation_re.search(clause_prefix)
            if neg_match:
                # Verify the text between negation and match is only
                # conjunction filler (commas, "or", "and", other verbs)
                gap = clause_prefix[neg_match.end():]
                if _conj_filler.match(gap):
                    continue
            findings.append(PromptLintFinding(
                code=code,
                pattern=pattern.pattern,
                matched_text=match.group(0),
            ))
    return findings


@dataclass
class ProfileGenerationResult:
    profile: dict[str, Any]
    profile_raw: str
    validation: ProfileValidationResult
    lint_findings: list[PromptLintFinding]
    used_fallback: bool
    parse_succeeded: bool
    model_call_duration_sec: float
    usage: dict[str, int]
    error: str | None = None

    def to_artifacts(self) -> dict[str, Any]:
        """Return a dict suitable for JSON serialization of all artifacts."""
        return {
            "profile_raw": self.profile_raw,
            "profile": self.profile,
            "validation": {
                "valid": self.validation.valid,
                "errors": self.validation.errors,
                "warnings": self.validation.warnings,
                "used_fallback": self.validation.used_fallback,
            },
            "lint_findings": [
                {
                    "code": f.code,
                    "pattern": f.pattern,
                    "matched_text": f.matched_text,
                }
                for f in self.lint_findings
            ],
            "used_fallback": self.used_fallback,
            "parse_succeeded": self.parse_succeeded,
            "model_call_duration_sec": self.model_call_duration_sec,
            "usage": self.usage,
            "error": self.error,
        }


def generate_profile(
    task_instruction: str,
    orientation_dict: dict[str, Any],
    tool_catalogue: list[dict[str, Any]],
    model_client: Any,
    available_tools: frozenset[str] | None = None,
) -> ProfileGenerationResult:
    """Run the full profile generation pipeline: prompt -> model -> parse -> validate -> lint."""
    if available_tools is None:
        available_tools = KNOWN_NATIVE_TOOLS

    messages = build_startup_messages(
        task_instruction, orientation_dict, tool_catalogue,
    )

    t0 = time.monotonic()
    response = model_client.call(messages, [], cache_prefix_len=0)
    raw_text = response.text if hasattr(response, "text") else str(response)
    usage = dict(response.usage) if hasattr(response, "usage") else {}
    duration = time.monotonic() - t0

    # Parse
    parsed = parse_profile_response(raw_text)
    if parsed is None:
        # One repair attempt: ask the model to fix its JSON
        parsed, repair_usage, repair_dur = _attempt_json_repair_impl(
            raw_text, model_client,
        )
        duration += repair_dur
        for k, v in repair_usage.items():
            usage[k] = usage.get(k, 0) + v
        if parsed is None:
            raise AgentInitializationFailure(
                "AHP profile generation failed: model response was not valid JSON "
                "after one repair attempt.",
                reason_code="architect_config_json_invalid_after_retry",
            )

    # Validate
    validation = validate_profile(parsed, available_tools)

    if not validation.valid:
        repaired, repair_validation, repair_usage, repair_dur = _attempt_schema_repair(
            profile=parsed,
            validation_errors=validation.errors,
            task_instruction=task_instruction,
            orientation_dict=orientation_dict,
            tool_catalogue=tool_catalogue,
            model_client=model_client,
            available_tools=available_tools,
        )
        duration += repair_dur
        for k, v in repair_usage.items():
            usage[k] = usage.get(k, 0) + v
        if repaired is None or not repair_validation.valid:
            errors = repair_validation.errors if repaired is not None else validation.errors
            raise AgentInitializationFailure(
                "AHP profile generation failed validation: "
                + "; ".join(errors[:5]),
                reason_code="architect_config_schema_invalid_after_retry",
            )
        parsed = repaired
        validation = repair_validation

    # Stamp version
    parsed["profile_version"] = PROFILE_VERSION

    # Lint the generated solver prompt
    solver_prompt = parsed.get("solver_system_prompt", "")
    lint_findings = lint_solver_prompt(solver_prompt) if isinstance(solver_prompt, str) else []

    return ProfileGenerationResult(
        profile=parsed,
        profile_raw=raw_text,
        validation=validation,
        lint_findings=lint_findings,
        used_fallback=False,
        parse_succeeded=True,
        model_call_duration_sec=duration,
        usage=usage,
    )
