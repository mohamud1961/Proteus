"""AHP artifact writing: persists per-run adaptive profile data to disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.aether2.runtime.adaptive_context import ValidatedRunConfig


REALIZED_CONFIG_FIELDS = {
    "solver_system_prompt": "system_prompt/task_block",
    "context_configuration.preserve": "context.preserve",
    "context_configuration.deprioritise": "context.deprioritise",
    "context_pack_policy": "context_pack with invariant evidence floor",
    "tool_configuration.primary_tools": "selected_tool_names filtered to available mandatory tools",
    "tool_configuration.reserve_capabilities": "tools.reserve_capabilities metadata",
    "hard_visible_requirements": "completion.hard_requirements and verifier.hard_requirements",
    "inferred_success_requirements": "verifier.inferred_requirements",
    "verification_watchpoints": "verifier.focus",
    "verification_configuration.model_verifier_focus": "verifier.focus",
    "verification_configuration.required_final_evidence": "completion/verifier.required_final_evidence",
    "verification_configuration.immediate_feedback_rounds": "verifier.immediate_feedback_rounds",
    "verification_configuration.final_rounds": "verifier.final_rounds",
    "verification_configuration.verifier_system_prompt": "verifier.system_prompt",
    "verification_configuration.model_verifier_prompt": "verifier.system_prompt",
    "repeat_action_guidance": "repeat.guidance",
    "initial_plan": "initial_plan and solver-visible checklist",
    "compaction_recommendation.preserve": "compaction.preserve",
    "compaction_recommendation.deprioritise": "compaction.deprioritise",
}


def _write_json(path: Path, payload: Any) -> str:
    """Write a JSON file and return the path as a string."""
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, default=str),
        encoding="utf-8",
    )
    return str(path)


def write_ahp_artifacts(
    artifacts_dir: Path,
    run_config: ValidatedRunConfig,
) -> dict[str, str]:
    """Write AHP artifacts to disk. Returns dict of artifact_name -> path."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    # Profile JSON + raw text + validation
    if run_config.profile_result is not None:
        paths["adaptation_contract"] = _write_json(
            artifacts_dir / "adaptation_contract.json",
            run_config.profile_result.profile,
        )
        raw_path = artifacts_dir / "adaptation_contract_raw.txt"
        raw_path.write_text(run_config.profile_result.profile_raw, encoding="utf-8")
        paths["adaptation_contract_raw"] = str(raw_path)

        paths["validation"] = _write_json(
            artifacts_dir / "validation.json",
            run_config.profile_result.to_artifacts().get("validation", {}),
        )

    # Generated task block
    task_block_path = artifacts_dir / "generated_task_block.txt"
    task_block_path.write_text(run_config.task_block, encoding="utf-8")
    paths["generated_task_block"] = str(task_block_path)

    # Selected tools
    paths["selected_tools"] = _write_json(
        artifacts_dir / "selected_tools.json",
        {
            "selected": run_config.selected_tool_names,
            "all_available": run_config.all_tool_names,
            "hidden": sorted(
                set(run_config.all_tool_names) - set(run_config.selected_tool_names)
            ),
        },
    )

    # Completion contract
    paths["completion_contract"] = _write_json(
        artifacts_dir / "completion_contract.json",
        {
            "hard_visible_requirements": run_config.completion_contract_items,
            "frozen_success_contract": run_config.frozen_success_contract,
        },
    )

    # Verifier payload preview
    paths["verifier_payload_preview"] = _write_json(
        artifacts_dir / "verifier_payload_preview.json",
        {
            "verifier_stated_requirements": run_config.verifier_stated_requirements,
            "verifier_focus": run_config.verifier_focus,
            "verifier_do_not_assume": run_config.verifier_do_not_assume,
        },
    )

    # Authority mapping (shows which profile field feeds which harness knob)
    paths["authority_mapping"] = _write_json(
        artifacts_dir / "authority_mapping.json",
        {
            "hard_visible_requirements -> completion_contract": run_config.completion_contract_items,
            "inferred_success_requirements -> verifier_stated_requirements (tagged)": [
                r for r in run_config.verifier_stated_requirements if r.startswith("[inferred]")
            ],
            "verification_watchpoints -> verifier_focus": run_config.verifier_focus,
            "do_not_assume -> verifier_do_not_assume": run_config.verifier_do_not_assume,
            "selected_tools -> active_tool_schemas": run_config.selected_tool_names,
            "initial_plan -> solver_checklist": run_config.initial_plan,
        },
    )

    # Run config summary
    paths["validated_run_config"] = _write_json(
        artifacts_dir / "validated_run_config.json",
        {
            "used_fallback": run_config.used_fallback,
            "fallback_reason": run_config.fallback_reason,
            "tool_count_selected": len(run_config.selected_tool_names),
            "tool_count_total": len(run_config.all_tool_names),
            "completion_contract_items_count": len(run_config.completion_contract_items),
            "verifier_requirements_count": len(run_config.verifier_stated_requirements),
            "has_task_block": bool(run_config.task_block),
            "has_frozen_success_contract": run_config.frozen_success_contract is not None,
            "has_initial_plan": bool(run_config.initial_plan),
            "initial_plan_step_count": len(run_config.initial_plan),
        },
    )

    paths["config_realization_audit"] = _write_json(
        artifacts_dir / "config_realization_audit.json",
        build_config_realization_audit(run_config),
    )

    return paths


def build_config_realization_audit(run_config: ValidatedRunConfig) -> dict[str, Any]:
    validation_errors: list[str] = []
    validation_warnings: list[str] = []
    profile_fields: list[str] = []
    if run_config.profile_result is not None:
        validation_errors = list(run_config.profile_result.validation.errors)
        validation_warnings = list(run_config.profile_result.validation.warnings)
        profile_fields = sorted(str(key) for key in run_config.profile_result.profile)
    rejected = [
        item
        for item in [*validation_errors, *validation_warnings]
        if "unsupported" in str(item) or "unknown tool" in str(item)
    ]
    return {
        "status": "realized",
        "realized_fields": dict(sorted(REALIZED_CONFIG_FIELDS.items())),
        "profile_fields_seen": profile_fields,
        "rejected_or_unsupported": rejected,
        "selected_tool_names": run_config.selected_tool_names,
        "reserve_capabilities": list(run_config.tools.reserve_capabilities),
        "verifier_focus": run_config.verifier_focus,
        "context_pack_invariants": [
            "current_plan",
            "recent_steps",
            "recent_failures",
            "verifier_feedback",
            "artifact_observations",
            "evidence_refs",
        ],
        "notes": [
            "Solver tools remain harness-owned stable capabilities after architect selection/filtering.",
            "Verifier capability focus is guidance within the generic read-only verifier tool set.",
        ],
    }
