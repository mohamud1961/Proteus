"""Compile vNext HarnessConfigIR into the legacy RuntimeConfigIR path."""
from __future__ import annotations

from dataclasses import asdict

from .smoke_compile import compile_visible_smoke_tests
from .runtime_ir import (
    ALWAYS_AVAILABLE_ACTION_KINDS,
    AutomaticMemoryPolicy,
    BootstrapPolicy,
    CompletionPolicy,
    ContextPolicy,
    ContextRecipe,
    ContextRecipeRecent,
    EnvMap,
    FIXED_KERNEL_TOOL_SURFACE,
    KERNEL_INTERNAL_ACTION_KINDS,
    HelperToolPolicy,
    ModelVerifierPolicy,
    RuntimeConfigIR,
    ReconfigurePolicy,
)
from .workbench_config import HarnessConfigIR, SUPPORTED_TOP_LEVEL_CONFIG_FIELDS

# The stable core is the FULL generic workbench: every kernel-implemented,
# capability-backed tool a task class could legitimately require.  The
# architect's tool_policy is recorded as guidance, never as a gate -- a
# missing tool must never be a hidden harness ceiling (caught 2026-07-05 by
# the Docker service-class smoke: launch_process was unreachable).
# Compatibility alias retained for callers/tests of the previous workbench
# compiler.  The inventory is kernel-owned and derived from ACTION_SCHEMA.
STABLE_CORE_SOLVER_TOOLS = FIXED_KERNEL_TOOL_SURFACE

AUDIT_SEPARATELY_TOOLS = (
    "run_experiment",
    "register_candidate",
    "reconfigure",
)

_DEFAULT_CAPABILITY_TOOLS = {
    "shell": ("run_command",),
    "filesystem": ("read_file", "write_file"),
    "managed_process": ("launch_process", "probe_service", "stop_process"),
    "service_probe": ("probe_service",),
    "artifact_inspection": ("inspect_artifact",),
    "output_handle_retrieval": ("read_output", "grep_output"),
    "network_fetch": ("bootstrap_acquire",),
}


TOP_LEVEL_CONFIG_FIELDS = (
    *sorted(SUPPORTED_TOP_LEVEL_CONFIG_FIELDS),
)


def harness_config_to_runtime_ir(config: HarnessConfigIR, envmap: EnvMap) -> RuntimeConfigIR:
    """Translate vNext workbench config into a runtime the current compiler can realize."""
    selected_caps = _stable_core_capabilities(envmap)
    helper_enabled = config.helper_script_policy.enabled and {"shell", "filesystem"}.issubset(selected_caps)
    smoke_result = compile_visible_smoke_tests(config, envmap)
    schema_humility_warnings = _schema_humility_warnings(config, envmap)
    advisory_notes = [
        "vNext HarnessConfigIR compiled into RuntimeConfigIR.",
        "tool_policy_mode=stable_core",
        "architect_tool_selection_applied=False",
        "fixed_kernel_tool_surface=" + str(list(FIXED_KERNEL_TOOL_SURFACE)),
        f"context_policy={config.context_policy.mode}",
        f"model_verifier_enabled={config.model_verifier_policy.enabled}",
        f"failure_feedback_persist_until={config.failure_feedback_policy.persist_until}",
    ]
    env_probe = envmap.task_metadata.get("environment_probe", {}) or {}
    if isinstance(env_probe, dict) and env_probe:
        preferred_python = (
            env_probe.get("validation_guidance", {}).get("preferred_python")
            if isinstance(env_probe.get("validation_guidance"), dict)
            else ""
        )
        advisory_notes.append("environment_probe_available=True")
        if preferred_python:
            advisory_notes.append(f"environment_preferred_python={preferred_python}")
    advisory_notes.extend(config.local_verification_limits)
    if config.evidence_requirements:
        advisory_notes.append("evidence_requirements=" + str(list(config.evidence_requirements)))
    if config.false_positive_risks:
        advisory_notes.append("false_positive_risks=" + str(list(config.false_positive_risks)))
    if config.minimum_completion_evidence:
        advisory_notes.append("minimum_completion_evidence=" + str(list(config.minimum_completion_evidence)))
    if config.re_derivable_claims:
        advisory_notes.append("re_derivable_claims=" + str(list(config.re_derivable_claims)))
    if config.verification_policy.visible_smoke_tests:
        advisory_notes.append("visible_smoke_tests=" + str([dict(item) for item in config.verification_policy.visible_smoke_tests]))
        if smoke_result.checks:
            advisory_notes.append("compiled_visible_smoke_checks=" + str([check.check_id for check in smoke_result.checks]))
        if smoke_result.rejected:
            advisory_notes.append("visible_smoke_compile_rejections=" + str([dict(item) for item in smoke_result.rejected]))
    advisory_notes.extend(schema_humility_warnings)
    if config.repair_warning_codes:
        advisory_notes.append("workbench_config_repairs=" + ",".join(config.repair_warning_codes))
    if config.legacy_tool_selection_warning:
        advisory_notes.append("legacy_tool_selection_warning=" + config.legacy_tool_selection_warning)
    return RuntimeConfigIR(
        architect_summary=config.task_understanding,
        solver_identity_prompt=config.solver_system_prompt.render(),
        verifier_identity_prompt=config.verifier_system_prompt.render(),
        selected_capabilities=selected_caps,
        context_policy=ContextPolicy(
            mode=config.context_policy.mode,
            include_sections=_context_sections(config),
            model_context_window_tokens=config.context_policy.model_context_window_tokens,
            recipe=_context_recipe(config),
        ),
        automatic_memory_policy=AutomaticMemoryPolicy(mode=config.memory_policy.automatic_repeat_mode),
        bootstrap_policy=BootstrapPolicy(allow_acquisition="shell" in selected_caps),
        helper_tool_policy=HelperToolPolicy(
            allow_creation=helper_enabled,
            trust_for_completion=False,
            task_local_dir=config.helper_script_policy.directory.replace("/app/", ""),
        ),
        completion_policy=CompletionPolicy(),
        reconfigure_policy=ReconfigurePolicy(
            max_reconfigurations=config.reconfigure_policy.max_versions,
            allowed_owners=config.reconfigure_policy.allowed_owners,
        ),
        model_verifier_policy=ModelVerifierPolicy(
            enabled=bool(config.model_verifier_policy.enabled),
            runs_on=tuple(config.model_verifier_policy.runs_on),
        ),
        inspection_plan=tuple(config.solver_system_prompt.workflow[:3]) or ("follow architect solver prompt",),
        proof_plan=tuple(config.solver_system_prompt.self_verification) or ("self-verify visible evidence",),
        check_plan=tuple(check.check_id for check in smoke_result.checks),
        compiler_injected_checks=smoke_result.checks,
        advisory_notes=tuple(advisory_notes),
        success_definition=config.success_definition,
        local_verification_limits=config.local_verification_limits,
        evidence_requirements=config.evidence_requirements,
        false_positive_risks=config.false_positive_risks,
        minimum_completion_evidence=config.minimum_completion_evidence,
        re_derivable_claims=config.re_derivable_claims,
        semantic_clause_coverage=tuple(asdict(item) for item in config.clause_coverage),
        semantic_verifier_checks=tuple(asdict(item) for item in config.verifier_strategy),
        semantic_false_positive_traps=config.verifier_false_positive_traps,
    )


def _capabilities_for_tools(tools: tuple[str, ...], envmap: EnvMap) -> tuple[str, ...]:
    wanted = set(tools) - set(ALWAYS_AVAILABLE_ACTION_KINDS)
    selected: list[str] = []
    for capability_id, cap in sorted(envmap.capabilities.items()):
        if wanted.intersection(set(cap.tool_names)):
            selected.append(capability_id)
    # If the registry lacks tool_names, fall back to common capability names.
    if "read_file" in wanted or "write_file" in wanted:
        selected.append("filesystem")
    if "run_command" in wanted:
        selected.append("shell")
    deduped = []
    for item in selected:
        if item in envmap.capabilities and item not in deduped:
            deduped.append(item)
    return tuple(deduped or tuple(envmap.capabilities)[:1])


def _stable_core_capabilities(envmap: EnvMap) -> tuple[str, ...]:
    return _capabilities_for_tools(STABLE_CORE_SOLVER_TOOLS, envmap)


def _context_sections(config: HarnessConfigIR) -> tuple[str, ...]:
    base = list(config.context_policy.always_include)
    base.extend(config.context_policy.include_on_failure)
    if not base:
        base = [
            "open_obligations", "obligation_status", "recent_progress", "failure_clusters",
            "artifacts_present", "planned_checks", "pending_checks", "command_results",
        ]
    allowed = {
        "open_obligations", "obligation_status", "monitor_alerts", "live_processes",
        "recent_progress", "failure_clusters", "artifacts_present", "candidate_leaderboard",
        "installed_capabilities", "planned_checks", "pending_checks", "command_results",
    }
    return tuple(item for item in dict.fromkeys(base) if item in allowed)


def _context_recipe(config: HarnessConfigIR) -> ContextRecipe | None:
    recipe = config.context_policy.recipe
    if recipe is None:
        return None
    return ContextRecipe(
        always_include=tuple(dict.fromkeys(recipe.always_include)),
        include_recent=tuple(
            ContextRecipeRecent(selector=item.selector, count=max(0, int(item.count)))
            for item in recipe.include_recent
        ),
        include_last_failure=max(0, int(recipe.include_last_failure)),
        preserve_exact=tuple(dict.fromkeys(recipe.preserve_exact)),
        make_queryable_not_inline=tuple(dict.fromkeys(recipe.make_queryable_not_inline)),
        unsupported_fields=tuple(dict.fromkeys(recipe.unsupported_fields)),
    )


def realization_preview(config: HarnessConfigIR, envmap: EnvMap) -> dict[str, object]:
    ir = harness_config_to_runtime_ir(config, envmap)
    return {
        "schema_version": config.schema_version,
        "selected_capabilities": list(ir.selected_capabilities),
        "context_policy_mode": ir.context_policy.mode,
        "solver_prompt_inserted": bool(ir.solver_identity_prompt.strip()),
        "verifier_prompt_inserted": bool(ir.verifier_identity_prompt.strip()),
        "helper_scripts_enabled": ir.helper_tool_policy.allow_creation,
        "tool_policy_mode": "stable_core",
        "architect_tool_selection_applied": False,
        "fixed_kernel_tool_surface": list(FIXED_KERNEL_TOOL_SURFACE),
        "advisory_notes": list(ir.advisory_notes),
        "repair_warning_codes": list(config.repair_warning_codes),
        "repair_warnings": list(config.repair_warnings),
        "rejected_config_items": [dict(item) for item in config.rejected_config_items],
        "legacy_tool_selection_paths": list(config.legacy_tool_selection_paths),
        "legacy_tool_selection_warning": config.legacy_tool_selection_warning,
        "realization_audit": config_realization_audit(config, envmap),
        "raw_config": asdict(config),
    }


def config_realization_audit(config: HarnessConfigIR, envmap: EnvMap) -> dict[str, object]:
    """Explain how each HarnessConfigIR field is handled.

    This is deliberately conservative: a field is marked realized only when it
    changes current runtime architecture, not when it is merely parsed.
    """
    ir = harness_config_to_runtime_ir(config, envmap)
    # Stable-core Workbench always exposes the fixed generic solver surface;
    # EnvMap capability gaps are runtime availability facts, not a reason to
    # shrink the solver contract.  Keep the filtered view only for the legacy
    # IR compatibility path.
    realized_tools = list(FIXED_KERNEL_TOOL_SURFACE)
    smoke_count = len(config.verification_policy.visible_smoke_tests)
    smoke_compile = compile_visible_smoke_tests(config, envmap)
    structural_count = len(config.verification_policy.structural_checks)
    schema_humility_warnings = _schema_humility_warnings(config, envmap)
    dispositions = {
        "schema_version": {
            "status": "validated",
            "evidence": "parse_harness_config_ir rejects non-harness_config.v1",
        },
        "task_understanding": {
            "status": "realized",
            "realized_as": "RuntimeConfigIR.architect_summary",
        },
        "success_definition": {
            "status": "realized_advisory",
            "realized_as": "RuntimeConfigIR.success_definition and verifier packet metadata",
        },
        "solver_system_prompt": {
            "status": "realized",
            "realized_as": "RuntimeConfigIR.solver_identity_prompt",
            "inserted": bool(ir.solver_identity_prompt.strip()),
        },
        "verifier_system_prompt": {
            "status": "realized",
            "realized_as": "RuntimeConfigIR.verifier_identity_prompt and verifier packet/prompt metadata",
            "inserted": bool(ir.verifier_identity_prompt.strip()),
        },
        "clause_coverage": {
            "status": "compiled" if config.clause_coverage else "uncompiled_legacy_missing",
            "count": len(config.clause_coverage),
            "realized_as": "RuntimeConfigIR.semantic_clause_coverage and semantic evidence realization metadata",
        },
        "verifier_strategy": {
            "status": "compiled" if config.verifier_strategy else "uncompiled_legacy_missing",
            "count": len(config.verifier_strategy),
            "false_positive_trap_count": len(config.verifier_false_positive_traps),
            "realized_as": "RuntimeConfigIR.semantic_verifier_checks and verifier packet compiled evidence metadata",
        },
        "evidence_requirements": {
            "status": "realized_advisory",
            "count": len(config.evidence_requirements),
            "realized_as": "RuntimeConfigIR.evidence_requirements, config_realization, and verifier packet metadata",
        },
        "false_positive_risks": {
            "status": "realized_advisory",
            "count": len(config.false_positive_risks),
            "realized_as": "RuntimeConfigIR.false_positive_risks, config_realization, and verifier packet metadata",
        },
        "minimum_completion_evidence": {
            "status": "realized_advisory",
            "count": len(config.minimum_completion_evidence),
            "realized_as": "RuntimeConfigIR.minimum_completion_evidence, config_realization, and verifier packet metadata",
        },
        "tool_policy": {
            "status": "legacy_input_ignored_if_present",
            "canonical_schema_field": False,
            "tool_policy_mode": "fixed_kernel_surface",
            "architect_tool_selection_applied": False,
            "legacy_enabled_tools_declared": list(config.tool_policy.enabled_tools),
            "legacy_disabled_tools_declared": list(config.tool_policy.disabled_tools),
            "legacy_tool_selection_paths": list(config.legacy_tool_selection_paths),
            "legacy_tool_selection_warning_code": config.legacy_tool_selection_warning,
            "fixed_kernel_tool_surface": list(FIXED_KERNEL_TOOL_SURFACE),
            "kernel_internal_action_kinds": sorted(KERNEL_INTERNAL_ACTION_KINDS),
            "selected_capabilities": list(ir.selected_capabilities),
            "runtime_allowed_tools_expected": realized_tools,
            "always_available_tools": sorted(ALWAYS_AVAILABLE_ACTION_KINDS),
            "note": "Canonical Architect output has no tool-selection field. Legacy input is recorded and ignored.",
        },
        "context_policy": {
            "status": "realized_partial",
            "mode": ir.context_policy.mode,
            "sections": list(ir.context_policy.include_sections),
            "recipe_declared": ir.context_policy.recipe is not None,
            "recipe": (
                {
                    "always_include": list(ir.context_policy.recipe.always_include),
                    "include_recent": [
                        {"selector": item.selector, "count": item.count}
                        for item in ir.context_policy.recipe.include_recent
                    ],
                    "include_last_failure": ir.context_policy.recipe.include_last_failure,
                    "preserve_exact": list(ir.context_policy.recipe.preserve_exact),
                    "make_queryable_not_inline": list(ir.context_policy.recipe.make_queryable_not_inline),
                    "unsupported_fields": list(ir.context_policy.recipe.unsupported_fields),
                }
                if ir.context_policy.recipe is not None
                else None
            ),
            "note": "supported modes are realized; custom sections are allowlisted; optional recipes are compiled into ContextPolicy and realized by ContextCompiler with explicit metadata",
        },
        "memory_policy": {
            "status": "realized_partial",
            "automatic_repeat_mode": config.memory_policy.automatic_repeat_mode,
            "runtime_automatic_memory_policy": asdict(ir.automatic_memory_policy),
            "index_by": list(config.memory_policy.index_by),
            "require_query_before_repeat": config.memory_policy.require_query_before_repeat,
            "require_query_before_overwrite": config.memory_policy.require_query_before_overwrite,
            "note": "automatic_repeat_mode is compiled into RuntimeConfigIR.automatic_memory_policy; legacy query-before fields remain advisory metadata while automatic memory replaces solver-called query rituals",
        },
        "verification_policy": {
            # ``structural_checks`` has no executor in the canonical
            # Workbench path.  The parser rejects non-empty values before the
            # Solver starts; keep this explicit in the audit as a defence
            # against callers constructing HarnessConfigIR directly.
            "status": "realized_partial" if structural_count == 0 else "unsupported_rejected_before_solver",
            "visible_smoke_tests": smoke_count,
            "compiled_visible_smoke_checks": [check.check_id for check in smoke_compile.checks],
            "smoke_compile_rejections": [dict(item) for item in smoke_compile.rejected],
            "structural_checks": structural_count,
            "structural_checks_realized": structural_count == 0,
            "structural_checks_note": (
                "empty; no structural checks requested"
                if structural_count == 0
                else "not executable; parser must reject before Solver start"
            ),
            "repair_warning_codes": list(config.repair_warning_codes),
            "repair_warnings": list(config.repair_warnings),
            "rejected_config_items": [dict(item) for item in config.rejected_config_items],
            "note": "safe typed visible smoke specs compile into internal CheckSpec evidence; unsupported or under-specified specs are rejected/quarantined and never become official grader authority",
        },
        "reconfigure_policy": {
            "status": "realized_policy_gated",
            "enabled": config.reconfigure_policy.enabled,
            "max_versions": config.reconfigure_policy.max_versions,
            "allowed_owners": list(config.reconfigure_policy.allowed_owners),
            "realized_as": "RuntimeConfigIR.reconfigure_policy; certified production kernels suspend model-authored reconfiguration",
        },
        "model_verifier_policy": {
            "status": "realized",
            "enabled": config.model_verifier_policy.enabled,
            "runs_on": list(config.model_verifier_policy.runs_on),
            "realized_as": "RuntimeConfigIR.model_verifier_policy and kernel_verifier reason filtering",
        },
        "failure_feedback_policy": {
            "status": "advisory_partial",
            "persist_until": config.failure_feedback_policy.persist_until,
            "note": "active findings persist via ActiveFindingStore; policy variants are not compiled yet",
        },
        "helper_script_policy": {
            "status": "realized",
            "enabled": ir.helper_tool_policy.allow_creation,
            "directory": ir.helper_tool_policy.task_local_dir,
        },
        "local_verification_limits": {
            "status": "realized_advisory",
            "count": len(config.local_verification_limits),
            "realized_as": "RuntimeConfigIR.local_verification_limits, advisory_notes, and verifier packet metadata",
        },
        "re_derivable_claims": {
            "status": "realized",
            "count": len(config.re_derivable_claims),
            "realized_as": "RuntimeConfigIR.re_derivable_claims and verifier packet metadata",
        },
        "expected_steps": {
            "status": "realized_advisory",
            "value": int(getattr(config, "expected_steps", 0) or 0),
            "realized_as": "config_realization.expected_steps and result-row step_efficiency (advisory metric, never a gate)",
        },
    }
    missing = [field for field in TOP_LEVEL_CONFIG_FIELDS if field not in dispositions]
    if structural_count:
        missing.append("structural_checks_not_realized")
    return {
        "top_level_fields": list(TOP_LEVEL_CONFIG_FIELDS),
        "dispositions": dispositions,
        "guardrails": {
            "schema_humility": {
                "status": "realized_advisory",
                "warnings": list(schema_humility_warnings),
                "realized_as": "architect prompt instruction, runtime manual guidance, advisory notes, and config_realization_audit guardrail",
                "note": "Placeholder notation is reported when it appears to be hardened into list/array contracts; the harness does not rewrite the task or decide the correct semantic type.",
            },
        },
        "missing_dispositions": missing,
        "has_silent_ignored_fields": bool(missing),
    }


def _realized_tools(selected_capabilities: tuple[str, ...], envmap: EnvMap) -> list[str]:
    tools = set(ALWAYS_AVAILABLE_ACTION_KINDS)
    for capability_id in selected_capabilities:
        cap = envmap.capabilities.get(capability_id)
        if cap is not None:
            tools.update(cap.tool_names or _DEFAULT_CAPABILITY_TOOLS.get(cap.capability_id, ()))
    return sorted(tools)


def _schema_humility_warnings(config: HarnessConfigIR, envmap: EnvMap) -> tuple[str, ...]:
    task_prompt = str(envmap.task_prompt or "").lower()
    placeholder_markers = ("[integer]", "[number]", "[string]", "<value>", "{field}", "...")
    if not any(marker in task_prompt for marker in placeholder_markers):
        return ()

    visible_smoke_text = str([dict(item) for item in config.verification_policy.visible_smoke_tests])
    config_text = "\n".join(
        [
            str(config.task_understanding),
            str(config.success_definition),
            config.solver_system_prompt.render(),
            config.verifier_system_prompt.render(),
            str(list(config.evidence_requirements)),
            str(list(config.minimum_completion_evidence)),
            str(list(config.false_positive_risks)),
            visible_smoke_text,
        ]
    ).lower()
    hardening_terms = (
        "one-element array",
        "one element array",
        "single-element array",
        "single element array",
        "one-element list",
        "single-element list",
        "mapped to a list",
        "mapped to an array",
        "value is a list",
        "value is an array",
    )
    if any(term in config_text for term in hardening_terms):
        return (
            "schema_humility_warning=placeholder_notation_may_have_been_hardened_into_array_or_list_contract",
        )
    return ()
