"""Data-driven adapter: AdaptationContract -> ValidatedRunConfig.

Maps profile configurator output to existing harness knobs by authority level.
NO task-specific branches. NO second harness. Pure data mapping.

Authority-level wiring:
    hard_visible_requirements  -> completion_contract (success definition)
    inferred_success_requirements -> verifier stated_requirements (lower authority)
    verification_watchpoints   -> verifier focus / inspection guidance
    do_not_assume              -> verifier anti-invention guidance
    selected_tools             -> exposed tool schema subset
    solver_system_prompt       -> composed with stable kernel for receipt-driven runs,
                                  otherwise frozen context pack (extra_prefix_messages)

Prompt layering:
    default system_prompt = stable invariant kernel ONLY (cacheable across tasks)
    receipt-driven composed system_prompt = stable kernel + task block
    frozen context pack (extra_prefix_messages) = adaptation summary + plan CONTENT
        contract summary + initial plan CONTENT + watchpoints + do_not_assume
        + selected-tools summary
    dynamic tail = plan STATUS/checkmarks (rendered per-turn)
"""

from __future__ import annotations

import json
from typing import Any

from harness.aether2.runtime.run_config import (
    HarnessRunConfig,
    make_harness_run_config,
)

from harness.aether2.runtime.adaptive_profile import (
    MANDATORY_SOLVER_TOOLS,
    ProfileGenerationResult,
    generate_profile,
    validate_profile,
)
from harness.aether2.runtime.adaptive_profile_helpers import (
    solver_visible_orientation,
)


ValidatedRunConfig = HarnessRunConfig

AHP_EVIDENCE_CONTRACT = (
    "[ahp_evidence_contract]\n"
    "AHP guidance does not weaken the stable evidence rules. When using visible checks or verifier files, "
    "run them with the command that actually executes their assertions. If a Python file defines pytest-style "
    "tests, invoking it as `python test_file.py` is weak evidence unless you observed the tests being collected "
    "and run. Prefer the discovered runner or an equivalent direct behavior check."
)


def _filter_tool_schemas(
    all_schemas: list[dict[str, Any]],
    selected_names: list[str],
) -> list[dict[str, Any]]:
    """Filter tool schemas to only those whose function.name is in selected_names."""
    selected_set = set(selected_names)
    # Always include mandatory tools
    selected_set |= MANDATORY_SOLVER_TOOLS
    filtered: list[dict[str, Any]] = []
    for schema in all_schemas:
        func = schema.get("function", schema)
        name = func.get("name", "")
        if name in selected_set:
            filtered.append(schema)
    return filtered


def _build_task_block(profile: dict[str, Any]) -> str:
    """Build the task block from the profile's solver_system_prompt.

    This is the model-authored task-specific guidance that gets appended
    AFTER the stable invariant kernel in the prefix.
    """
    return profile.get("solver_system_prompt", "")


def _compose_system_prompt(base_system_prompt: str, task_block: str) -> str:
    """Compose the mechanical harness frame with architect-owned solver guidance."""
    task_block_text = str(task_block or "").strip()
    if not task_block_text:
        return base_system_prompt
    return base_system_prompt.rstrip() + "\n\n[architect_solver_prompt]\n" + task_block_text


def _build_verifier_system_prompt(profile: dict[str, Any]) -> str:
    verification_config = profile.get("verification_configuration", {})
    if isinstance(verification_config, dict):
        for key in ("verifier_system_prompt", "model_verifier_prompt"):
            value = verification_config.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())
    return ""


def _build_verifier_stated_requirements(
    profile: dict[str, Any],
    base_stated_requirements: list[str],
) -> list[str]:
    """Build authority-tagged stated_requirements for the verifier.

    Hard requirements come first (highest authority), then inferred
    (lower authority). Each item is tagged with its authority level
    so the verifier can weight them appropriately.
    """
    requirements: list[str] = list(base_stated_requirements)

    hard = profile.get("hard_visible_requirements", [])
    if isinstance(hard, list):
        for item in hard:
            text = str(item).strip()
            if text and text not in requirements:
                requirements.append(text)

    inferred = profile.get("inferred_success_requirements", [])
    if isinstance(inferred, list):
        for item in inferred:
            text = str(item).strip()
            if text and text not in requirements:
                requirements.append(f"[inferred] {text}")

    return requirements


def _build_completion_contract_items(
    profile: dict[str, Any],
) -> list[str]:
    """Extract hard_visible_requirements for the completion contract."""
    hard = profile.get("hard_visible_requirements", [])
    if isinstance(hard, list):
        return [str(item).strip() for item in hard if str(item).strip()]
    return []


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _extract_initial_plan(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract and normalize initial_plan from the profile.

    Each step is {step, status, evidence_needed}. Capped at 5 steps.
    """
    raw = profile.get("initial_plan", [])
    if not isinstance(raw, list):
        return []
    plan: list[dict[str, Any]] = []
    for item in raw[:5]:
        if isinstance(item, dict) and item.get("step"):
            plan.append({
                "step": str(item["step"]),
                "status": str(item.get("status", "pending")),
                "evidence_needed": str(item.get("evidence_needed", "")),
            })
        elif isinstance(item, str) and item.strip():
            plan.append({
                "step": item.strip(),
                "status": "pending",
                "evidence_needed": "",
            })
    return plan


def _render_initial_plan_checklist(plan: list[dict[str, Any]]) -> str:
    """Render initial_plan as a solver-visible checklist string."""
    if not plan:
        return ""
    lines = [
        "[initial_plan] A starting guide, not a script "
        "-- update it if evidence shows the path is wrong.",
    ]
    for i, step in enumerate(plan, 1):
        status = step.get("status", "pending")
        marker = "[ ]" if status == "pending" else "[x]"
        line = f"  {marker} {i}. {step['step']}"
        evidence = step.get("evidence_needed", "")
        if evidence:
            line += f"  (evidence: {evidence})"
        lines.append(line)
    return "\n".join(lines)


def _build_extra_prefix_messages(
    profile: dict[str, Any],
    selected_tool_names: list[str],
    initial_plan: list[dict[str, Any]],
    task_block: str = "",
    include_task_block: bool = True,
) -> list[dict[str, Any]]:
    """Build the FROZEN CONTEXT PACK for ContextManager.build_prefix().

    This pack is injected as extra_prefix_messages and contains all
    task-specific generated content that must NOT be in the system prompt:
      - Model-authored task block (solver_system_prompt)
      - Adaptation contract summary (watchpoints, do_not_assume, selected tools)
      - Initial plan CONTENT (frozen; plan STATUS/checkmarks are dynamic tail)

    The system prompt stays the stable invariant kernel, cacheable across tasks.
    """
    messages: list[dict[str, Any]] = []

    # 1. Task block: the model-authored task-specific guidance
    if include_task_block and task_block:
        messages.append({
            "role": "system",
            "content": "[ahp_task_block]\n" + task_block,
        })

    messages.append({"role": "system", "content": AHP_EVIDENCE_CONTRACT})

    # 2. Profile summary with adaptation contract fields
    task_understanding = profile.get("task_understanding", {})
    summary = task_understanding.get("summary", "") if isinstance(task_understanding, dict) else ""
    important_properties = task_understanding.get("important_properties", []) if isinstance(task_understanding, dict) else []

    watchpoints = profile.get("verification_watchpoints", [])
    do_not_assume = profile.get("do_not_assume", [])
    approach_risks = profile.get("approach_risks", [])
    pivot_signals = profile.get("pivot_signals", [])
    success_definition = profile.get("success_definition", [])
    uncertain_risks = profile.get("uncertain_or_exploratory_risks", [])
    context_config = profile.get("context_configuration", {})
    compaction_config = profile.get("compaction_recommendation", {})
    tool_config = profile.get("tool_configuration", {})

    profile_summary: dict[str, Any] = {
        "task_understanding": summary,
        "important_properties": important_properties[:6],
        "selected_tools": selected_tool_names,
        "reserve_capabilities": (
            tool_config.get("reserve_capabilities", [])[:6]
            if isinstance(tool_config, dict) and isinstance(tool_config.get("reserve_capabilities", []), list)
            else []
        ),
        "success_definition": success_definition[:6] if isinstance(success_definition, list) else [],
        "verification_watchpoints": watchpoints[:6] if isinstance(watchpoints, list) else [],
        "do_not_assume": do_not_assume[:6] if isinstance(do_not_assume, list) else [],
        "approach_risks": approach_risks[:4] if isinstance(approach_risks, list) else [],
        "pivot_signals": pivot_signals[:4] if isinstance(pivot_signals, list) else [],
        "uncertain_or_exploratory_risks": uncertain_risks[:4] if isinstance(uncertain_risks, list) else [],
        "context_preserve": (
            context_config.get("preserve", [])[:6]
            if isinstance(context_config, dict) and isinstance(context_config.get("preserve", []), list)
            else []
        ),
        "context_deprioritise": (
            context_config.get("deprioritise", [])[:6]
            if isinstance(context_config, dict) and isinstance(context_config.get("deprioritise", []), list)
            else []
        ),
        "compaction_preserve": (
            compaction_config.get("preserve", [])[:6]
            if isinstance(compaction_config, dict) and isinstance(compaction_config.get("preserve", []), list)
            else []
        ),
        "compaction_deprioritise": (
            compaction_config.get("deprioritise", [])[:6]
            if isinstance(compaction_config, dict) and isinstance(compaction_config.get("deprioritise", []), list)
            else []
        ),
    }

    content = "[ahp_profile_summary]\n" + json.dumps(
        profile_summary,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    messages.append({"role": "system", "content": content})

    # 3. Initial plan CONTENT (frozen in context pack)
    #    Plan STATUS/checkmarks render only in the dynamic tail path.
    plan_text = _render_initial_plan_checklist(initial_plan)
    if plan_text:
        messages.append({"role": "system", "content": plan_text})

    return messages


def apply_adaptation_contract(
    profile_result: ProfileGenerationResult,
    *,
    base_system_prompt: str,
    base_tool_schemas: list[dict[str, Any]],
    base_stated_requirements: list[str],
    task_instruction: str,
    use_full_generated_prompt: bool = False,
) -> ValidatedRunConfig:
    """Map a validated profile to existing harness knobs.

    This is the single data-driven adapter. No task-specific if/branches.

    Args:
        profile_result: The validated profile from generate_profile().
        base_system_prompt: The stable invariant kernel (SYSTEM_PROMPT).
        base_tool_schemas: All available tool schemas from the registry.
        base_stated_requirements: Requirements extracted from the task instruction.
        task_instruction: The raw task instruction text.
        use_full_generated_prompt: If True, compose the mechanical harness frame
            with the generated solver_system_prompt in the system prompt and
            omit the duplicate task-block prefix message. DEFAULT=False: the
            task block is appended after the kernel via frozen context pack.
    """
    profile = profile_result.profile
    used_fallback = profile_result.used_fallback

    # --- Tool selection ---
    tool_config = profile.get("tool_configuration", {})
    all_tool_names: list[str] = []
    for schema in base_tool_schemas:
        func = schema.get("function", schema)
        name = func.get("name", "")
        if name:
            all_tool_names.append(name)

    if isinstance(tool_config, dict):
        primary_tools = tool_config.get("primary_tools", [])
        if isinstance(primary_tools, list) and primary_tools:
            selected_tool_names = [
                ("query_evidence" if name == "query_history" else name)
                for name in primary_tools
                if ("query_evidence" if name == "query_history" else name) in set(all_tool_names)
                or ("query_evidence" if name == "query_history" else name) in MANDATORY_SOLVER_TOOLS
            ]
            # Ensure mandatory tools are always included
            for mandatory in sorted(MANDATORY_SOLVER_TOOLS):
                if mandatory not in selected_tool_names and mandatory in set(all_tool_names):
                    selected_tool_names.append(mandatory)
        else:
            selected_tool_names = list(all_tool_names)
    else:
        selected_tool_names = list(all_tool_names)

    active_tool_schemas = _filter_tool_schemas(base_tool_schemas, selected_tool_names)

    # --- System prompt ---
    task_block = _build_task_block(profile)

    if use_full_generated_prompt and task_block:
        # Architect-owned path: keep only the harness mechanical frame above
        # the generated solver prompt. The generated prompt is the substantive
        # solver system prompt; the static compatibility prompt does not
        # compete with it.
        system_prompt = _compose_system_prompt(base_system_prompt, task_block)
    else:
        # Compatibility path: system_prompt = supplied base prompt.
        # Task block is delivered via frozen context pack (extra_prefix_messages)
        system_prompt = base_system_prompt

    # --- Completion contract items (hard_visible_requirements) ---
    completion_contract_items = _build_completion_contract_items(profile)

    verifier_stated_requirements = _build_verifier_stated_requirements(
        profile, base_stated_requirements,
    )
    watchpoints = profile.get("verification_watchpoints", [])
    verifier_focus = [str(w).strip() for w in watchpoints if str(w).strip()] if isinstance(watchpoints, list) else []
    do_not_assume = profile.get("do_not_assume", [])
    verifier_do_not_assume = [str(d).strip() for d in do_not_assume if str(d).strip()] if isinstance(do_not_assume, list) else []
    verifier_system_prompt = _build_verifier_system_prompt(profile)

    # --- Initial plan (revisable checklist) ---
    initial_plan = _extract_initial_plan(profile)

    # --- Extra prefix messages (frozen context pack) ---
    extra_prefix_messages = _build_extra_prefix_messages(
        profile,
        selected_tool_names,
        initial_plan,
        task_block=task_block,
        include_task_block=not use_full_generated_prompt,
    )

    # --- Frozen success contract ---
    frozen_success_contract = None
    if completion_contract_items:
        frozen_success_contract = {
            "source": "ahp_hard_visible_requirements",
            "contract_text": task_instruction,
            "verbatim_lines": completion_contract_items,
        }

    fallback_reason = None
    if used_fallback:
        validation = profile_result.validation
        if validation.errors:
            fallback_reason = f"profile validation failed: {'; '.join(validation.errors[:3])}"
        elif profile_result.error:
            fallback_reason = f"model call failed: {profile_result.error}"
        else:
            fallback_reason = "profile generation failed (unknown reason)"

    verification_config = profile.get("verification_configuration", {})
    if not isinstance(verification_config, dict):
        verification_config = {}
    verifier_focus = [
        *verifier_focus,
        *_list_of_strings(verification_config.get("model_verifier_focus", [])),
    ]
    required_final_evidence = _list_of_strings(verification_config.get("required_final_evidence", []))
    immediate_feedback_rounds = verification_config.get("immediate_feedback_rounds", 2)
    final_rounds = verification_config.get("final_rounds", 2)

    context_config = profile.get("context_configuration", {})
    if not isinstance(context_config, dict):
        context_config = {}
    compaction_config = profile.get("compaction_recommendation", {})
    if not isinstance(compaction_config, dict):
        compaction_config = {}
    repeat_action_guidance = str(profile.get("repeat_action_guidance", "") or "")
    reserve_capabilities = []
    if isinstance(tool_config, dict):
        reserve_capabilities = _list_of_strings(tool_config.get("reserve_capabilities", []))

    inferred_requirements = _list_of_strings(profile.get("inferred_success_requirements", []))

    return make_harness_run_config(
        system_prompt=system_prompt,
        task_block=task_block,
        active_tool_schemas=active_tool_schemas,
        selected_tool_names=selected_tool_names,
        all_tool_names=all_tool_names,
        mandatory_tool_names=sorted(MANDATORY_SOLVER_TOOLS),
        reserve_capabilities=reserve_capabilities,
        base_requirements=list(base_stated_requirements),
        hard_requirements=completion_contract_items,
        inferred_requirements=inferred_requirements,
        verifier_system_prompt=verifier_system_prompt,
        verifier_focus=verifier_focus,
        verifier_do_not_assume=verifier_do_not_assume,
        required_final_evidence=required_final_evidence,
        verifier_max_rounds=2,
        verifier_immediate_feedback_rounds=immediate_feedback_rounds,
        verifier_final_rounds=final_rounds,
        extra_prefix_messages=extra_prefix_messages,
        context_pack_policy=profile.get("context_pack_policy", {}),
        context_preserve=_list_of_strings(context_config.get("preserve", [])),
        context_deprioritise=_list_of_strings(context_config.get("deprioritise", [])),
        repeat_action_guidance=repeat_action_guidance,
        compaction_preserve=_list_of_strings(compaction_config.get("preserve", [])),
        compaction_deprioritise=_list_of_strings(compaction_config.get("deprioritise", [])),
        initial_plan=initial_plan,
        frozen_success_contract=frozen_success_contract,
        used_fallback=used_fallback,
        profile_result=profile_result,
        fallback_reason=fallback_reason,
    )


def generate_and_apply(
    *,
    task_instruction: str,
    orientation_dict: dict[str, Any],
    tool_catalogue: list[dict[str, Any]],
    model_client: Any,
    available_tools: frozenset[str] | None = None,
    base_system_prompt: str,
    base_tool_schemas: list[dict[str, Any]],
    base_stated_requirements: list[str],
    use_full_generated_prompt: bool = False,
) -> ValidatedRunConfig:
    """One-call: generate profile then apply adaptation contract.

    Wraps generate_profile + apply_adaptation_contract. AHP is a variant; if
    profile generation fails, the caller should observe the failure instead of
    silently receiving a baseline-shaped config.
    """
    profile_result = generate_profile(
        task_instruction=task_instruction,
        orientation_dict=orientation_dict,
        tool_catalogue=tool_catalogue,
        model_client=model_client,
        available_tools=available_tools,
    )

    return apply_adaptation_contract(
        profile_result,
        base_system_prompt=base_system_prompt,
        base_tool_schemas=base_tool_schemas,
        base_stated_requirements=base_stated_requirements,
        task_instruction=task_instruction,
        use_full_generated_prompt=use_full_generated_prompt,
    )
