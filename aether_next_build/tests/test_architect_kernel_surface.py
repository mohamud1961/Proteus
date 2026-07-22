import json

import pytest

from aether_next.model_prompts import (
    ARCHITECT_SYSTEM_PROMPT,
    architect_prompt_has_no_tool_selection_language,
)
from aether_next.workbench_prompt import WORKBENCH_ARCHITECT_SYSTEM_PROMPT
from aether_next.runtime_ir import ACTION_SCHEMA, FIXED_KERNEL_TOOL_SURFACE, KERNEL_INTERNAL_ACTION_KINDS
from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.workbench_config import parse_harness_config_ir
from aether_next.workbench_compile import harness_config_to_runtime_ir, realization_preview
from aether_next.runtime_ir import CapabilityDescriptor, EnvMap


def test_architect_defers_to_fixed_generic_kernel_tool_surface() -> None:
    prompt = ARCHITECT_SYSTEM_PROMPT.lower()

    assert architect_prompt_has_no_tool_selection_language()
    assert "kernel owns a fixed trusted kernel tool surface" in prompt
    assert "do not" in prompt and "select" in prompt
    assert "every task clause" in prompt
    assert "direct verifier inspection route" in prompt

    for forbidden in ("selected_capabilities", "enabled_tools", "tool_policy"):
        assert forbidden not in prompt


def test_production_workbench_architect_route_uses_fixed_kernel_surface() -> None:
    """The certified WorkbenchArchitect prompt must carry the same authority contract."""
    prompt = WORKBENCH_ARCHITECT_SYSTEM_PROMPT.lower()
    assert "kernel owns one fixed generic action surface" in prompt
    assert "architect output must never select" in prompt
    assert "{fixed_kernel_tool_surface}" not in prompt
    assert all(tool in prompt for tool in FIXED_KERNEL_TOOL_SURFACE)
    for forbidden in ("\"tool_policy\"", "\"enabled_tools\"", "\"selected_capabilities\""):
        assert forbidden not in prompt


def _minimal_workbench_output() -> dict[str, object]:
    return {
        "schema_version": "harness_config.v1",
        "task_understanding": "create the requested artifact",
        "success_definition": "the artifact is correct",
        "solver_system_prompt": {"role": "artifact solver", "workflow": ["inspect", "act", "verify"]},
        "verifier_system_prompt": {
            "role": "state inspector",
            "success_criteria": ["artifact is correct"],
            "required_evidence": ["read the artifact"],
        },
        "evidence_requirements": ["artifact contents"],
        "minimum_completion_evidence": ["independent inspection"],
        "tool_policy": {"enabled_tools": ["read_file"]},
    }


def test_legacy_workbench_tool_selection_is_explicitly_non_authoritative() -> None:
    config = parse_harness_config_ir(json.dumps(_minimal_workbench_output()))
    assert config.legacy_tool_selection_paths == ("$.tool_policy",)
    assert config.legacy_tool_selection_warning
    env = EnvMap(
        task_prompt="create the requested artifact",
        workspace_root="/app",
        capabilities={
            "filesystem": CapabilityDescriptor("filesystem", "files", tool_names=("read_file", "write_file")),
            "shell": CapabilityDescriptor("shell", "shell", tool_names=("run_command",)),
        },
    )
    preview = realization_preview(config, env)
    assert preview["architect_tool_selection_applied"] is False
    assert preview["fixed_kernel_tool_surface"] == list(FIXED_KERNEL_TOOL_SURFACE)
    audit = preview["realization_audit"]["dispositions"]["tool_policy"]
    assert audit["architect_tool_selection_applied"] is False
    assert audit["legacy_tool_selection_warning_code"] == config.legacy_tool_selection_warning


def test_new_architect_selection_fields_fail_closed_before_solver() -> None:
    raw = _minimal_workbench_output()
    raw["selected_capabilities"] = ["filesystem"]
    with pytest.raises(Exception, match="unsupported top-level HarnessConfigIR fields"):
        parse_harness_config_ir(json.dumps(raw))


def test_workbench_filesystem_only_env_keeps_fixed_solver_surface() -> None:
    """Capability omission must not shrink the Workbench solver contract."""
    raw = _minimal_workbench_output()
    raw["tool_policy"] = {"enabled_tools": ["read_file"]}
    config = parse_harness_config_ir(json.dumps(raw))
    env = EnvMap(
        task_prompt="create the requested artifact",
        workspace_root="/app",
        capabilities={
            "filesystem": CapabilityDescriptor(
                "filesystem", "files", tool_names=("read_file", "write_file")
            ),
        },
    )
    ir = harness_config_to_runtime_ir(config, env)
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
    expected = tuple(name for name, _args in ACTION_SCHEMA if name not in KERNEL_INTERNAL_ACTION_KINDS)
    assert tuple(name for name, _args in compiled.action_schema) == expected
    assert compiled.config_realization["tools_visible_to_solver"] == sorted(expected)
    assert compiled.config_realization["tools_runtime_allowed"] == sorted(expected)
    assert set(KERNEL_INTERNAL_ACTION_KINDS).isdisjoint(compiled.config_realization["tools_visible_to_solver"])


@pytest.mark.parametrize(
    "path",
    [
        "solver_system_prompt", "verifier_system_prompt", "tool_policy",
        "context_policy", "memory_policy", "verification_policy",
        "model_verifier_policy", "failure_feedback_policy", "helper_script_policy",
    ],
)
def test_unknown_nested_config_key_fails_closed(path: str) -> None:
    raw = _minimal_workbench_output()
    raw.setdefault(path, {})["unknown_contract_key"] = True
    with pytest.raises(Exception, match=rf"unsupported fields in {path}"):
        parse_harness_config_ir(json.dumps(raw))


def test_unknown_context_recipe_key_fails_closed() -> None:
    raw = _minimal_workbench_output()
    raw["context_policy"] = {"recipe": {"unknown_contract_key": True}}
    with pytest.raises(Exception, match="unsupported fields in context_policy.recipe"):
        parse_harness_config_ir(json.dumps(raw))


@pytest.mark.parametrize(
    "path",
    [
        "context_policy", "memory_policy", "verification_policy",
        "model_verifier_policy", "failure_feedback_policy", "helper_script_policy",
    ],
)
def test_non_object_nested_policy_fails_closed(path: str) -> None:
    raw = _minimal_workbench_output()
    raw[path] = ["not", "an", "object"]
    with pytest.raises(Exception, match=rf"{path} must be an object"):
        parse_harness_config_ir(json.dumps(raw))


def test_unknown_visible_smoke_field_fails_closed() -> None:
    raw = _minimal_workbench_output()
    raw["verification_policy"] = {
        "visible_smoke_tests": [{"type": "file_exists", "path": "out.txt", "mystery": True}],
    }
    with pytest.raises(Exception, match="unsupported fields in verification_policy.visible_smoke_tests item"):
        parse_harness_config_ir(json.dumps(raw))
