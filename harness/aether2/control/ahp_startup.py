"""AHP startup phase: generate adaptive profile and map to run config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.aether2.runtime.adaptive_context import (
    ValidatedRunConfig,
    generate_and_apply,
)
from harness.aether2.runtime.adaptive_artifacts import write_ahp_artifacts
from harness.aether2.runtime.adaptive_profile_helpers import solver_visible_orientation
from harness.aether2.runtime.prompts import MECHANICAL_SYSTEM_PROMPT, SYSTEM_PROMPT
from harness.aether2.runtime.run_config import build_baseline_run_config


def _baseline_run_config(
    base_tool_schemas: list[dict[str, Any]],
    base_stated_requirements: list[str],
) -> ValidatedRunConfig:
    """Build a HarnessRunConfig that exactly equals baseline behavior."""
    return build_baseline_run_config(
        system_prompt=SYSTEM_PROMPT,
        base_tool_schemas=base_tool_schemas,
        base_stated_requirements=base_stated_requirements,
    )


def run_ahp_startup(
    *,
    task_instruction: str,
    orientation_dict: dict[str, Any],
    base_tool_schemas: list[dict[str, Any]],
    base_stated_requirements: list[str],
    model_client: Any,
    available_tools: frozenset[str] | None = None,
    artifacts_dir: Path | None = None,
    use_full_generated_prompt: bool = False,
) -> ValidatedRunConfig:
    """Run the AHP startup phase: profile generation + adaptation contract.

    AHP is a variant path: generation or validation failures propagate to the
    caller instead of silently returning a baseline-equivalent config.
    Writes artifacts to artifacts_dir/.aether2/ahp/ if provided.
    """
    solver_visible_orient = solver_visible_orientation(orientation_dict)
    tool_catalogue = list(base_tool_schemas)

    run_config = generate_and_apply(
        task_instruction=task_instruction,
        orientation_dict=solver_visible_orient,
        tool_catalogue=tool_catalogue,
        model_client=model_client,
        available_tools=available_tools,
        base_system_prompt=MECHANICAL_SYSTEM_PROMPT,
        base_tool_schemas=base_tool_schemas,
        base_stated_requirements=base_stated_requirements,
        use_full_generated_prompt=True,
    )

    if artifacts_dir is not None:
        ahp_dir = artifacts_dir / ".aether2" / "ahp"
        artifact_paths = write_ahp_artifacts(ahp_dir, run_config)

        # Write flag_off_baseline_diff.txt
        baseline = _baseline_run_config(base_tool_schemas, base_stated_requirements)
        diff_report = _compute_baseline_diff(baseline, run_config)
        diff_path = ahp_dir / "flag_off_baseline_diff.txt"
        diff_path.write_text(diff_report, encoding="utf-8")

    return run_config


def _compute_baseline_diff(
    baseline: ValidatedRunConfig,
    active: ValidatedRunConfig,
) -> str:
    """Compare baseline config vs active AHP config. Used for audit."""
    lines: list[str] = ["=== flag_off_baseline_diff ==="]

    if baseline.system_prompt == active.system_prompt:
        lines.append("system_prompt: IDENTICAL")
    else:
        lines.append(f"system_prompt: DIFFERS (baseline={len(baseline.system_prompt)} chars, active={len(active.system_prompt)} chars)")
        lines.append(f"  task_block appended: {bool(active.task_block)}")

    if baseline.active_tool_schemas == active.active_tool_schemas:
        lines.append("tool_schemas: IDENTICAL")
    else:
        baseline_names = set(baseline.selected_tool_names)
        active_names = set(active.selected_tool_names)
        hidden = baseline_names - active_names
        added = active_names - baseline_names
        lines.append(f"tool_schemas: DIFFERS (hidden={sorted(hidden)}, added={sorted(added)})")

    if baseline.completion_contract_items == active.completion_contract_items:
        lines.append("completion_contract: IDENTICAL")
    else:
        lines.append(f"completion_contract: DIFFERS ({len(active.completion_contract_items)} items)")

    if baseline.verifier_stated_requirements == active.verifier_stated_requirements:
        lines.append("verifier_stated_requirements: IDENTICAL")
    else:
        lines.append(f"verifier_stated_requirements: DIFFERS ({len(active.verifier_stated_requirements)} items)")

    if baseline.extra_prefix_messages == active.extra_prefix_messages:
        lines.append("extra_prefix_messages: IDENTICAL")
    else:
        lines.append(f"extra_prefix_messages: DIFFERS ({len(active.extra_prefix_messages)} messages)")

    if baseline.frozen_success_contract == active.frozen_success_contract:
        lines.append("frozen_success_contract: IDENTICAL")
    else:
        lines.append("frozen_success_contract: DIFFERS")

    if baseline.initial_plan == active.initial_plan:
        lines.append("initial_plan: IDENTICAL")
    else:
        lines.append(f"initial_plan: DIFFERS ({len(active.initial_plan)} steps)")

    lines.append(f"used_fallback: {active.used_fallback}")
    lines.append(f"fallback_reason: {active.fallback_reason}")

    return "\n".join(lines)
