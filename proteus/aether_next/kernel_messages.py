"""Message-building helpers extracted from kernel.py to stay under 500 LOC."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Mapping

from .compiler import ConfigCompiler
from .runtime_ir import CompiledRuntime, EnvMap, normalize_relpath
from .runtime_manual import build_runtime_manual


def build_architect_request(
    envmap: EnvMap,
    compiler: ConfigCompiler,
) -> dict[str, Any]:
    """Build the request payload sent to the architect hook."""
    objective_graph, eval_index = compiler.analyze_envmap(envmap)
    return {
        "task_prompt": envmap.task_prompt,
        "envmap_digest": envmap.digest(),
        "envmap": {
            "workspace_root": envmap.workspace_root,
            "visible_files": [
                normalize_relpath(path, envmap.workspace_root)
                for path in envmap.visible_files
            ],
            "visible_dirs": [
                normalize_relpath(path, envmap.workspace_root)
                for path in envmap.visible_dirs
            ],
            "file_tree": envmap.file_tree,
            "file_map_summary": dict(envmap.file_map_summary),
            "services": dict(envmap.services),
            "resource_limits": dict(envmap.resource_limits),
            "permissions": dict(envmap.permissions),
            "interactive_features": dict(envmap.interactive_features),
            "task_metadata": _model_facing_task_metadata(envmap.task_metadata),
            "visible_task_materials": _visible_task_materials(envmap.task_metadata),
            "available_action_affordances": list(envmap.task_metadata.get("available_action_affordances", ()) or ()),
            "observed_environment_support": dict(envmap.task_metadata.get("observed_environment_support", {}) or {}),
            "reviewer_probe_support": dict(envmap.task_metadata.get("reviewer_probe_support", {}) or {}),
            "environment_probe": dict(envmap.task_metadata.get("environment_probe", {}) or {}),
            "network_scope": envmap.network_scope,
        },
        "capability_index": list(compiler.registry.metadata_view()),
        "runtime_manual": build_runtime_manual(),
        "objective_graph": asdict(objective_graph),
        "eval_index": {
            "checks": [asdict(check) for check in eval_index.checks],
            "authoritative_check_ids": [
                check.check_id for check in eval_index.authoritative_checks()
            ],
        },
        "required_ir_fields": [
            "architect_summary",
            "solver_identity_prompt",
            "selected_capabilities",
            "process_policy",
            "bootstrap_policy",
            "completion_policy",
            "refusal_policy",
            "reconfigure_policy",
            "inspection_plan",
            "proof_plan",
            "check_plan",
            "forbidden_paths",
        ],
    }


def _model_facing_task_metadata(task_metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return only model-facing runtime facts, not benchmark-shaped metadata."""
    allowed = {
        "resource_budget",
        "model_facing_resource_budget",
        "agent_timeout_sec",
        "verifier_timeout_sec",
        "instruction_path_references",
        "network_policy",
        "env_fact_policy",
        "visible_validation_surfaces",
    }
    blocked = {
        "public_task_metadata",
        "internal_task_metadata",
        "category",
        "difficulty",
        "tags",
        "expert_time_estimate_min",
        "junior_time_estimate_min",
        "docker_image",
        "task_slug",
        "task_name",
    }
    result: dict[str, Any] = {}
    for key, value in task_metadata.items():
        if key in blocked or key == "environment_probe":
            continue
        if key in allowed:
            if key == "resource_budget" and isinstance(value, Mapping):
                result[key] = {
                    budget_key: budget_value
                    for budget_key, budget_value in value.items()
                    if budget_key in {"agent_timeout_sec", "verifier_timeout_sec", "build_timeout_sec", "cpus", "memory", "storage"}
                }
            else:
                result[key] = value
    return result


def _visible_task_materials(task_metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "visible_validation_surfaces": list(task_metadata.get("visible_validation_surfaces", ()) or ()),
        "declared_assets": list(task_metadata.get("declared_assets", ()) or ()),
        "visible_examples": list(task_metadata.get("visible_examples", ()) or ()),
        "visible_material_summary": dict(task_metadata.get("visible_material_summary", {}) or {}),
    }


def build_solver_messages(
    compiled: CompiledRuntime,
    context_packet: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build the full message list sent to the solver hook."""
    messages = compiled.prefix_messages()
    messages.append(
        {
            "role": "system",
            "content": "[context_packet]\n"
            + json.dumps(
                context_packet,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        }
    )
    return messages
