from copy import deepcopy
import random

import pytest

from aether_next import (
    ConfigCompileError,
    FIXED_KERNEL_TOOLS,
    ProcessMode,
    compile_workbench_config,
)
from aether_next.config import assert_all_fields_realised, describe_config


def test_interactive_config_compiles(contract, config_factory):
    compiled = compile_workbench_config(config_factory(), contract)
    assert compiled.process_mode is ProcessMode.INTERACTIVE
    assert compiled.fixed_tools == FIXED_KERNEL_TOOLS
    assert compiled.legacy_warnings == ()
    assert_all_fields_realised(compiled)


def test_service_config_compiles_and_changes_process_policy(contract, config_factory):
    selectors = [
        {"kind": "task_contract", "representation": "full", "required": True},
        {"kind": "service_state", "target": "web", "representation": "structured_summary", "required": True},
        {"kind": "active_findings", "representation": "full"},
    ]
    compiled = compile_workbench_config(config_factory(mode="service", selectors=selectors), contract)
    assert compiled.process_mode is ProcessMode.SERVICE
    assert compiled.config.process_policy.readiness == ("wait_for_port", "probe_http")


def test_batch_config_compiles_with_larger_budget(contract, config_factory):
    selectors = [
        {"kind": "task_contract", "representation": "full", "required": True},
        {"kind": "job_state", "target": "trainer", "representation": "structured_summary", "required": True},
        {"kind": "receipt", "target": "receipt:000001", "representation": "handle_only", "required": False},
    ]
    compiled = compile_workbench_config(config_factory(mode="batch_job", selectors=selectors), contract)
    assert compiled.process_mode is ProcessMode.BATCH_JOB
    assert compiled.config.resource_policy.max_steps == 120
    assert "wait_for_process_state" in compiled.config.process_policy.readiness


@pytest.mark.parametrize("key", ["selected_capabilities", "enabled_tools", "tool_policy", "tools", "capabilities"])
def test_new_config_rejects_architect_tool_surface_keys(contract, config_factory, key):
    raw = config_factory()
    raw[key] = ["read_file"]
    with pytest.raises(ConfigCompileError, match="outside Architect scope"):
        compile_workbench_config(raw, contract)


def test_legacy_tool_policy_can_be_ignored_only_in_compatibility_mode(contract, config_factory):
    raw = config_factory()
    raw["tool_policy"] = {"enabled_tools": ["read_file"]}
    compiled = compile_workbench_config(raw, contract, compatibility_mode=True)
    assert compiled.fixed_tools == FIXED_KERNEL_TOOLS
    assert compiled.legacy_warnings == ("legacy_tool_selection_ignored: fixed kernel tool surface retained",)


def test_unknown_selector_fails_before_solver(contract, config_factory):
    raw = config_factory()
    raw["context_policy"]["selectors"][0]["kind"] = "free_form_context_request"
    with pytest.raises(ConfigCompileError, match="must be one of"):
        compile_workbench_config(raw, contract)


def test_unknown_representation_fails_before_solver(contract, config_factory):
    raw = config_factory()
    raw["context_policy"]["selectors"][0]["representation"] = "smart_summary"
    with pytest.raises(ConfigCompileError, match="must be one of"):
        compile_workbench_config(raw, contract)


def test_targeted_excerpt_requires_pattern(contract, config_factory):
    raw = config_factory(selectors=[
        {"kind": "file", "target": "/app/large.log", "representation": "targeted_excerpt", "required": True}
    ])
    with pytest.raises(ConfigCompileError, match="requires pattern"):
        compile_workbench_config(raw, contract)


def test_interactive_mode_rejects_readiness_probes(contract, config_factory):
    raw = config_factory()
    raw["process_policy"]["readiness"] = ["wait_for_port"]
    with pytest.raises(ConfigCompileError, match="interactive mode"):
        compile_workbench_config(raw, contract)


def test_resource_timeout_invariants(contract, config_factory):
    raw = config_factory()
    raw["resource_policy"]["command_timeout_s"] = 1000
    with pytest.raises(ConfigCompileError, match="cannot exceed"):
        compile_workbench_config(raw, contract)


def test_every_clause_must_be_covered(contract, config_factory):
    raw = config_factory()
    raw["clause_coverage"] = raw["clause_coverage"][:1]
    with pytest.raises(ConfigCompileError, match="uncovered_task_clauses:c_value"):
        compile_workbench_config(raw, contract)


def test_every_clause_must_have_verifier_check(contract, config_factory):
    raw = config_factory()
    raw["verifier_strategy"]["clause_checks"] = raw["verifier_strategy"]["clause_checks"][:1]
    with pytest.raises(ConfigCompileError, match="unverified_task_clauses:c_value"):
        compile_workbench_config(raw, contract)


def test_duplicate_clause_entries_fail(contract, config_factory):
    raw = config_factory()
    raw["clause_coverage"].append(deepcopy(raw["clause_coverage"][0]))
    with pytest.raises(ConfigCompileError, match="duplicate_clause_coverage"):
        compile_workbench_config(raw, contract)


def test_different_configs_have_different_hashes_but_same_tools(contract, config_factory):
    file_config = compile_workbench_config(config_factory(), contract)
    service_config = compile_workbench_config(config_factory(mode="service", selectors=[
        {"kind": "task_contract", "representation": "full", "required": True},
        {"kind": "service_state", "target": "web", "representation": "full", "required": True},
    ]), contract)
    assert file_config.stable_config_sha256 != service_config.stable_config_sha256
    assert file_config.fixed_tools == service_config.fixed_tools


def test_realisation_report_accounts_for_every_top_level_field(contract, config_factory):
    compiled = compile_workbench_config(config_factory(), contract)
    payload = describe_config(compiled)
    paths = {item["path"] for item in payload["dispositions"]}
    expected = {
        "task_understanding", "success_definition", "clause_coverage",
        "solver_system_prompt", "verifier_system_prompt", "verifier_strategy",
        "context_policy", "memory_policy", "process_policy", "resource_policy",
        "reconfigure_policy", "local_verification_limits", "kernel.fixed_tools",
    }
    assert expected <= paths
    assert all(item["status"] in {"compiled", "kernel_owned"} for item in payload["dispositions"])


def test_config_compilation_is_deterministic(contract, config_factory):
    raw = config_factory()
    one = compile_workbench_config(raw, contract)
    two = compile_workbench_config(deepcopy(raw), contract)
    assert one.stable_config_json == two.stable_config_json
    assert one.stable_config_sha256 == two.stable_config_sha256
    assert one.dispositions == two.dispositions


def test_random_supported_selector_combinations_compile_deterministically(contract, config_factory):
    random.seed(7)
    pool = [
        {"kind": "task_contract", "representation": "full", "required": True},
        {"kind": "env_fact", "target": "python", "representation": "structured_summary"},
        {"kind": "file", "target": "/app/out.txt", "representation": "head_tail", "max_chars": 100},
        {"kind": "artifact", "target": "/app/frame.png", "representation": "handle_only"},
        {"kind": "service_state", "target": "web", "representation": "structured_summary"},
        {"kind": "job_state", "target": "trainer", "representation": "structured_summary"},
        {"kind": "active_findings", "representation": "full"},
        {"kind": "latest_result", "representation": "structured_summary"},
        {"kind": "named_section", "target": "plan", "representation": "full"},
    ]
    hashes = set()
    for _ in range(30):
        selected = [pool[0]] + random.sample(pool[1:], k=random.randint(1, 5))
        compiled = compile_workbench_config(config_factory(selectors=deepcopy(selected)), contract)
        assert compiled.fixed_tools == FIXED_KERNEL_TOOLS
        hashes.add(compiled.stable_config_sha256)
    assert len(hashes) >= 10


def test_nested_unknown_config_field_fails_closed(contract, config_factory):
    raw = config_factory()
    raw["context_policy"]["mystery_knob"] = True
    with pytest.raises(ConfigCompileError, match="unsupported fields in context_policy"):
        compile_workbench_config(raw, contract)


def test_service_and_batch_modes_require_readiness(contract, config_factory):
    for mode in ("service", "batch_job"):
        raw = config_factory(mode=mode)
        raw["process_policy"]["readiness"] = []
        with pytest.raises(ConfigCompileError, match="requires deterministic readiness"):
            compile_workbench_config(raw, contract)


def test_solver_state_cannot_be_reconfiguration_owner(contract, config_factory):
    raw = config_factory()
    raw["reconfigure_policy"]["allowed_owners"] = ["solver_state"]
    with pytest.raises(ConfigCompileError):
        compile_workbench_config(raw, contract)
