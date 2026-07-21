from copy import deepcopy

import pytest

from aether_next import ConfigCompileError, TaskClause, TaskContract, compile_workbench_config


@pytest.mark.parametrize(
    "path,value",
    [
        (("context_policy", "selectors", 0, "required"), "false"),
        (("process_policy", "allow_equivalent_overlap"), 1),
        (("reconfigure_policy", "enabled"), "true"),
        (("resource_policy", "max_steps"), True),
        (("resource_policy", "total_timeout_s"), "600"),
    ],
)
def test_scalar_types_fail_closed(contract, config_factory, path, value):
    raw = config_factory()
    cursor = raw
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value
    with pytest.raises(ConfigCompileError):
        compile_workbench_config(raw, contract)


def test_string_is_not_accepted_as_string_list(contract, config_factory):
    raw = config_factory()
    raw["memory_policy"]["index_by"] = "path"
    with pytest.raises(ConfigCompileError, match="list of strings"):
        compile_workbench_config(raw, contract)


def test_missing_nested_required_key_has_clean_compile_error(contract, config_factory):
    raw = config_factory()
    del raw["clause_coverage"][0]["clause_id"]
    with pytest.raises(ConfigCompileError, match=r"clause_coverage\[0\]\.clause_id is required"):
        compile_workbench_config(raw, contract)


def test_unknown_nested_tool_key_is_not_silently_migrated(contract, config_factory):
    raw = config_factory()
    raw["context_policy"]["selected_capabilities"] = ["shell"]
    with pytest.raises(ConfigCompileError, match="tool-selection"):
        compile_workbench_config(raw, contract, compatibility_mode=True)


def test_unknown_readiness_route_is_rejected(contract, config_factory):
    raw = config_factory(mode="service")
    raw["process_policy"]["readiness"] = ["wait_for_magic"]
    with pytest.raises(ConfigCompileError, match="unsupported service readiness"):
        compile_workbench_config(raw, contract)


def test_duplicate_readiness_routes_are_rejected(contract, config_factory):
    raw = config_factory(mode="service")
    raw["process_policy"]["readiness"] = ["wait_for_port", "wait_for_port"]
    with pytest.raises(ConfigCompileError, match="must be unique"):
        compile_workbench_config(raw, contract)


def test_return_all_findings_is_kernel_invariant(contract, config_factory):
    raw = config_factory()
    raw["verifier_strategy"]["return_all_findings"] = False
    with pytest.raises(ConfigCompileError, match="required kernel invariant"):
        compile_workbench_config(raw, contract)


def test_false_positive_traps_must_be_unique(contract, config_factory):
    raw = config_factory()
    raw["verifier_strategy"]["false_positive_traps"] = ["same", "same"]
    with pytest.raises(ConfigCompileError, match="must be unique"):
        compile_workbench_config(raw, contract)


def test_exact_task_atoms_are_validated():
    with pytest.raises(ValueError, match="non-empty strings"):
        TaskClause("c", "clause", ("",))
    with pytest.raises(ValueError, match="unique within"):
        TaskClause("c", "clause", ("alpha", "alpha"))


def test_exact_atom_may_support_multiple_clauses():
    contract = TaskContract.create(
        "task",
        (
            TaskClause("c1", "one", ("/app/out",)),
            TaskClause("c2", "two", ("/app/out",)),
        ),
    )
    assert contract.clause_ids == frozenset({"c1", "c2"})


def test_config_input_is_not_mutated(contract, config_factory):
    raw = config_factory()
    original = deepcopy(raw)
    compile_workbench_config(raw, contract)
    assert raw == original


def test_memory_index_fields_are_typed_and_must_include_action_kind(contract, config_factory):
    raw = config_factory()
    raw["memory_policy"]["index_by"] = ["path", "made_up"]
    with pytest.raises(ConfigCompileError, match="unsupported memory index"):
        compile_workbench_config(raw, contract)
    raw = config_factory()
    raw["memory_policy"]["index_by"] = ["path"]
    with pytest.raises(ConfigCompileError, match="must include action_kind"):
        compile_workbench_config(raw, contract)
