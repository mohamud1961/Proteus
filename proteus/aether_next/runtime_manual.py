"""Runtime manual surfaced to the architect before the first model call.

This is deliberately data, not prose-only prompt text: the architect should
know which harness knobs are real and which fields are advisory.
"""
from __future__ import annotations

from typing import Any

from .runtime_ir import ACTION_SCHEMA, ALWAYS_AVAILABLE_ACTION_KINDS


SUPPORTED_CONTEXT_POLICIES = (
    "default_bounded",
    "latest_tool_result_only",
    "rolling_recent",
    "retrieval_augmented",
    "failure_focused",
)

ALLOWED_VISIBLE_SMOKE_TEST_TYPES = (
    "syntax_check",
    "run_deliverable_on_fixture",
    "content_assertion",
    "file_exists",
    "file_size",
)


def build_runtime_manual() -> dict[str, Any]:
    """Return a stable architect-facing description of configurable runtime facts."""
    action_schema = {name: list(args) for name, args in ACTION_SCHEMA}
    return {
        "architect_role": (
            "Configure the task-specific harness/workbench for solver success. "
            "Do not solve the task, hallucinate file contents, or invent hidden grader logic. "
            "Design the task-specific solver system prompt and runtime policy."
        ),
        "role_contract": {
            "architect_does": [
                "designs the task-specific solver system prompt",
                "selects supported hard runtime policies",
                "specifies verification-first workflow guidance",
                "states local verification limits",
            ],
            "architect_does_not": [
                "solve the task",
                "claim unseen file contents",
                "invent hidden grader behavior",
                "turn arbitrary shell into trusted gate authority",
            ],
        },
        "architect_skill_spec": {
            "purpose": "Compile task prompt plus live EnvMap into an executable task operating recipe.",
            "required_inputs": [
                "task_prompt",
                "envmap.file_tree",
                "envmap.file_map_summary",
                "envmap.environment_probe",
                "capability_index",
                "runtime_manual",
                "objective_graph",
                "eval_index",
            ],
            "self_audit_questions": [
                "Did I use live environment facts instead of assuming command/package availability?",
                "Did every local check include concrete path or argv fields so it can compile?",
                "Did every required deliverable have a write target and verification plan?",
                "Did I write an elite task-specific solver prompt with exact deliverables, workflow, validation, submit gates, and do-not-submit gates?",
                "Did I write an elite task-specific verifier prompt with completed evidence, false-positive traps, verdict rules, and feedback style?",
                "Did I emit evidence_requirements, false_positive_risks, and minimum_completion_evidence that match the prompts and checks?",
                "Did I explain what local checks cannot prove?",
                "Did I avoid selecting or gating the kernel-owned fixed tool surface?",
            ],
            "repair_rule": "If a proposed check cannot compile, rewrite it as a supported typed visible smoke test or move it to local_verification_limits.",
        },
        "environment_awareness": {
            "probe_source": "envmap.environment_probe",
            "rule": "Prefer probed commands/interpreters/modules over assumptions. If python is absent and python3 is present, use python3 in checks and solver guidance.",
            "dependency_rule": "Do not design deliverable scripts around unavailable imports unless the solver must install and verify them.",
        },
        "schema_humility": {
            "placeholder_notation_rule": (
                "Do not turn placeholder notation such as [integer], <value>, {field}, or ... "
                "into a hard type/shape contract unless the task explicitly states that the "
                "literal brackets, list/array, object, or placeholder delimiters are required."
            ),
            "ambiguous_output_examples": (
                "When an output example is ambiguous, instruct the solver/verifier to confirm "
                "parsed types from task prose and executable/parser evidence instead of asserting "
                "the placeholder shape."
            ),
            "visible_smoke_limit": (
                "Visible smoke content_assertion checks may confirm stable literal keys/tokens, "
                "but must not harden ambiguous placeholder delimiters into required output syntax."
            ),
        },
        "hard_configurable": [
            "context_policy",
            "memory_policy",
            "verification_policy",
            "helper_script_policy",
            "auto_success_candidate_policy",
        ],
        "soft_configurable": [
            "solver_system_prompt",
            "task_workflow_guidance",
            "self_verification_guidance",
            "failure_hypotheses",
        ],
        "config_authority": {
            "hard_config": "Compiler-realized fields must either change runtime behavior, be rejected, or be recorded as advisory/partial in config_realization.",
            "soft_guidance": "Prompt/workflow guidance shapes the solver but is not trusted completion evidence.",
        },
        "tools": {
            "action_schema": action_schema,
            "always_available": sorted(ALWAYS_AVAILABLE_ACTION_KINDS),
            "rule": "The solver sees the kernel-owned fixed generic surface subject only to real environment and safety availability; Architect output does not select tools.",
            "solver_callable_verification_tools": ["inspect_checks", "run_check"],
            "stable_core_solver_tools": [
                "read_file",
                "write_file",
                "run_command",
                "query_memory",
                "query_artifact_history",
                "inspect_diff",
                "record_observation",
                "inspect_checks",
                "run_check",
            ],
            "architect_does_not_choose_tools": True,
            "audit_separately": ["run_experiment", "register_candidate", "reconfigure"],
            "capability_selection_guidance": [
                "Architect output must not select, enable, disable, rename, or gate core actions.",
                "Mention run_command as likely useful when success likely requires executing a program, running a validator, invoking a CLI, generating artifacts with a tool, compiling, testing, probing a service, or running scripts.",
                "Filesystem-only guidance is appropriate only when the task can be solved and usefully checked by reading/writing files without executing anything.",
                "If an execution-required task cannot use run_command because the environment forbids it, explain that limit in local_verification_limits.",
            ],
        },
        "memory": {
            "store": "ExecutionLedger receipts/events",
            "query_tool": "query_memory",
            "query_memory_always_available": True,
            "automatic_repeat_interception": True,
            "automatic_repeat_modes": ["off", "advisory", "require_justification", "soft_block_exact_repeat"],
            "automatic_repeat_mode_guidance": [
                "Use advisory as the default when repeated reads/checks may be legitimate but should be surfaced.",
                "Use require_justification when repeated expensive/destructive commands should be blocked unless the solver explains why the repeat is necessary.",
                "Use soft_block_exact_repeat when exact repeated reads/checks/commands are likely loops and should be blocked without justification.",
                "Use off only for diagnostics or tasks where automatic repeat intervention is known to be harmful.",
            ],
            "architect_prompt_rule": "Do not tell the solver to call query_memory. Memory should surface automatically when a proposed read/check/command/write collides with prior evidence; solver guidance should say to use prior evidence, narrow the action, justify the repeat, or change strategy.",
            "query_use_cases": [
                "check whether a file was already read",
                "retrieve prior file hash/excerpt/summary",
                "retrieve failed checks and completion findings",
                "query artifact history and diffs",
                "record explicit learned observations",
                "compare what changed since a finding",
                "avoid wasteful repeat reads or checks",
            ],
            "recorded_event_kinds": [
                "architect_config",
                "compiled_config",
                "solver_prompt",
                "context_packet",
                "tool_call",
                "tool_result",
                "file_read",
                "file_write",
                "command_run",
                "check_result",
                "model_verifier_result",
                "active_finding",
                "artifact_snapshot",
                "artifact_diff",
                "observation",
            ],
        },
        "context": {
            "source": "compiled policy over structured ledger/memory state",
            "supported_policies": list(SUPPORTED_CONTEXT_POLICIES),
            "policy_goal": "Choose the smallest context that keeps task contract, latest evidence, active findings, pending checks, and useful memory query affordances visible.",
            "recipe_selectors": [
                "active_completion_findings", "pending_checks", "latest_failure", "failed_checks",
                "artifact_history", "memory_events", "observations", "file_reads", "file_writes",
                "command_results", "check_results", "verifier_results", "repeated_actions",
                "files_already_read", "stuck"
            ],
            "recipe_safety_rule": "active_completion_findings and pending_checks are safety sections and cannot be silently dropped when present.",
            "compression": {
                "implemented": True,
                "planned_threshold": "60_percent_of_model_context_window",
                "instruction": "Context compression triggers when rendered context reaches 60% of the model context window.",
            },
        },
        "verification": {
            "deterministic_checks_are_evidence": True,
            "model_verifier_planned_as_internal_gate": True,
            "architect_authored_verifier_prompt": True,
            "official_grader_outside_agent": True,
            "allowed_visible_smoke_test_types": list(ALLOWED_VISIBLE_SMOKE_TEST_TYPES),
            "success_candidate_flow": [
                "solver builds/modifies artifact",
                "solver self-verifies visible evidence",
                "solver may inspect_checks/run_check for harness-owned checks",
                "solver submit requests internal verifier review",
                "model verifier may perform bounded read-only inspection when packet evidence is insufficient",
                "model verifier decides internal completed or repair-needed when configured",
                "official grader remains external benchmark authority",
            ],
            "verifier_inspector": {
                "implemented": True,
                "trigger": "solver_submit only in canonical workbench flow",
                "allowed_read_only_requests": [
                    "read_file",
                    "rerun_check",
                    "inspect_artifact_history",
                    "inspect_recent_receipts",
                ],
                "bounded_rounds": 3,
            },
            "compilable_checks": [
                "file_exists",
                "file_size",
                "syntax_check",
                "content_assertion",
                "run_deliverable_on_fixture",
                "compiler_generated_visible_smoke_check_for_supported_typed_specs",
            ],
            "forbidden_checks": [
                "hidden_grader_calls",
                "placeholder_commands",
                "unknown_files",
                "arbitrary_model_authored_shell_as_gate_authority",
            ],
            "visible_smoke_test_rule": (
                "visible_smoke_tests entries must use only the allowed typed smoke-test types. "
                "If no safe typed smoke test applies, emit [] and describe the limitation in "
                "local_verification_limits or solver_system_prompt.self_verification."
            ),
            "check_plan_guidance": [
                "For tasks with local validation pressure, provide safe typed visible smoke tests when possible.",
                "If a safe local check cannot be specified, local_verification_limits must explain exactly what local checks cannot prove.",
                "Unsupported or raw-command smoke tests are quarantined and must not be treated as authoritative evidence.",
                "Completion must not be provable only by source-text or syntax checks when the task requires semantic, executable, visual, service, cryptographic, data, or artifact behavior.",
            ],
        },
        "perception_and_artifact_extraction": {
            "metadata_is_not_semantics": True,
            "inspect_artifact_metadata_fields": [
                "existence",
                "file_type",
                "size",
                "sha256",
                "permissions",
                "owner",
                "image_dimensions_when_tooling_exists",
            ],
            "semantic_extraction_rule": (
                "For image, OCR, video, chart, screenshot, or code-from-image tasks, "
                "architect guidance must name a real extraction route grounded in "
                "envmap.environment_probe or state that semantic extraction is unavailable."
            ),
            "not_completion_evidence": [
                "file dimensions",
                "MIME type",
                "file size",
                "metadata-only inspect_artifact success",
                "same-method recomputation",
            ],
            "solver_guidance_requirement": (
                "Require raw semantic content extraction, independent validation of the "
                "transcribed/derived artifact, and preservation of extraction evidence."
            ),
            "verifier_guidance_requirement": (
                "Before completed on visual/OCR/media tasks, inspect current state for "
                "fresh extraction evidence and do not accept metadata-only probes as "
                "semantic proof."
            ),
        },
        "solver_prompt_requirements": {
            "quality_target": "Elite task-specific prompt, typically 600-1200 words for non-trivial tasks.",
            "verification_first_style": [
                "what to inspect first",
                "what evidence to gather before building",
                "what artifact/content checks to perform before submit",
                "which local or harness checks to run",
                "how to respond when automatic memory indicates a repeated read/check/command/write",
                "how to respond when local checks fail or completion findings arrive",
                "when not to repeat reads/checks/commands/writes",
                "when to call inspect_checks or run_check",
                "what local verification cannot prove",
                "what ready to submit means for this task",
                "that submit_outcome is a true completion claim, not a fishing call",
            ],
            "not_a_global_progress_contract": True,
        },
        "verifier_prompt_requirements": {
            "quality_target": "Elite task-specific verifier prompt, typically 400-900 words for non-trivial tasks.",
            "must_include": [
                "task-specific success criteria",
                "required evidence before completed",
                "false-positive traps",
                "verdict meanings for completed/needs_repair/uncertain_missing_evidence/blocked_by_tooling/blocked_by_harness_config",
                "actionable feedback style",
                "how to use success_definition and local_verification_limits",
                "read-only current-state inspection role",
            ],
        },
        "harness_config_quality": {
            "must_include": [
                "success_definition",
                "evidence_requirements",
                "false_positive_risks",
                "minimum_completion_evidence",
                "local_verification_limits",
                "typed visible smoke tests when supported",
                "model_verifier_policy enabled unless unsafe or impossible",
            ],
            "reject_patterns": [
                "source-text-only completion for behavioral tasks",
                "syntax-only completion for semantic tasks",
                "unsupported smoke tests as authority",
                "tool hiding by architect omission",
                "generic prompts that do not name task deliverables or failure modes",
            ],
        },
        "prompt_assembly": {
            "cached_static_prefix": [
                "immutable_harness_protocol",
                "stable_tool_semantics",
                "automatic_memory_manual",
                "verification_submit_manual",
            ],
            "dynamic_task_prefix": [
                "task_prompt",
                "envmap_file_tree",
                "architect_designed_solver_prompt",
                "configured_tool_list",
                "configured_verification_policy",
                "configured_context_policy",
            ],
            "per_step_context": [
                "ledger_derived_current_state",
                "active_findings",
                "solver_authored_evidence_audit_trail",
                "pending_checks",
                "memory_summaries_or_query_results",
            ],
        },
    }
