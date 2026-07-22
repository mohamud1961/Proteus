from __future__ import annotations

import json
from pathlib import Path
import shlex
from typing import Any, Mapping

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.envmap_builder import build_envmap_from_task
from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.environment_probe import _command_probe_names, _expanded_command_names, probe_environment
from aether_next.kernel import AetherNextKernel
from aether_next.kernel_messages import build_architect_request
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.model_hooks import ModelHooks
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    BootstrapPolicy,
    CompletionPolicy,
    EnvMap,
    HelperToolPolicy,
    RuntimeConfigIR,
    SolverTurn,
)
from aether_next.workbench_config import (
    UNSUPPORTED_VISIBLE_SMOKE_TEST_TYPE_CODE,
    parse_harness_config_ir,
    parse_workbench_architect_output,
)
from aether_next.workbench_compile import harness_config_to_runtime_ir


_CAPS = {
    "shell": CapabilityDescriptor(
        capability_id="shell", summary="Run commands", tool_names=("run_command",)
    ),
    "filesystem": CapabilityDescriptor(
        capability_id="filesystem", summary="Files", tool_names=("read_file", "write_file")
    ),
}


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Write /app/out.txt",
        workspace_root="/app",
        capabilities=dict(_CAPS),
    )


def _env_with_verify_commands(*commands: str) -> EnvMap:
    return EnvMap(
        task_prompt="Write /app/out.txt",
        workspace_root="/app",
        capabilities=dict(_CAPS),
        grader_hints={"verify_commands": tuple(commands)},
    )


def _ir(selected=("filesystem",)) -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="summary",
        solver_identity_prompt="task-specific solver prompt",
        selected_capabilities=tuple(selected),
        bootstrap_policy=BootstrapPolicy(allow_acquisition=False),
        helper_tool_policy=HelperToolPolicy(allow_creation=False),
        completion_policy=CompletionPolicy(
            require_authoritative_check=False,
            require_all_obligations=False,
            require_recent_progress=False,
        ),
        inspection_plan=("inspect",),
        proof_plan=("prove",),
    )


def _action(kind: str, args: Mapping[str, Any], cap: str = "filesystem") -> ActionRequest:
    return ActionRequest(
        action_id=f"a-{kind}",
        kind=kind,
        capability_id=cap,
        arguments=dict(args),
        intent="test",
        expected_observation="test",
        if_fail_next="fix",
    )


class Hooks:
    def __init__(self, ir: RuntimeConfigIR, turns: list[SolverTurn]) -> None:
        self.ir = ir
        self.turns = list(turns)

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        return self.ir

    def solve(self, messages: list[dict[str, str]], compiled) -> SolverTurn:
        if self.turns:
            return self.turns.pop(0)
        return SolverTurn(kind="submit_outcome", summary="submit")

    def reconfigure(self, request, compiled, ledger):
        return self.ir


def _workbench_config_json(*, tools: list[str] | None = None, context_mode: str = "retrieval_augmented") -> str:
    return json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "Write the output file.",
        "success_definition": "out.txt exists.",
        "solver_system_prompt": {
            "role": "Workbench configured file solver",
            "workflow": ["inspect files", "write output", "self-verify", "submit"],
            "self_verification": ["inspect configured checks before submitting"],
            "memory_use": ["query_memory before repeating reads or checks"],
            "stop_conditions": ["submit only after visible evidence is sufficient"],
        },
        "verifier_system_prompt": {
            "role": "Read-only current-state verifier for the file deliverable",
            "success_criteria": ["out.txt exists and satisfies the task request"],
            "required_evidence": ["current file state or check evidence supports completion"],
            "false_positive_traps": ["file existence alone may not prove correct content"],
            "verdict_guidance": ["completed requires current evidence; needs_repair names the gap"],
            "feedback_guidance": ["give a concrete repair or evidence request"],
        },
        "evidence_requirements": ["out.txt exists in the current workspace", "current file evidence supports the requested content"],
        "false_positive_risks": ["a file can exist but contain the wrong content"],
        "minimum_completion_evidence": ["current out.txt existence and content evidence"],
        "tool_policy": {"enabled_tools": tools or ["read_file", "write_file", "query_memory"]},
        "context_policy": {"mode": context_mode, "always_include": ["recent_progress", "pending_checks"]},
        "model_verifier_policy": {"enabled": True},
    })


class FakeWorkbenchArchitect:
    def __init__(self, raw_config: str) -> None:
        self.config = parse_harness_config_ir(raw_config)

    def configure(self, request: Mapping[str, Any]):
        return self.config, []


class FailingWorkbenchArchitect:
    """Simulates configure() returning no config (unparseable output twice)."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors

    def configure(self, request: Mapping[str, Any]):
        return None, list(self.errors)


class RepairingWorkbenchArchitect:
    def __init__(self, raw_config: str) -> None:
        repaired = parse_workbench_architect_output(raw_config)
        assert repaired.config is not None
        self.config = repaired.config
        self.errors = list(repaired.errors)

    def configure(self, request: Mapping[str, Any]):
        return self.config, list(self.errors)


class FlakyWorkbenchArchitect:
    def __init__(self, initial_raw_config: str, *, reconfigure_errors: list[str]) -> None:
        self.initial = parse_harness_config_ir(initial_raw_config)
        self.reconfigure_errors = list(reconfigure_errors)
        self.calls = 0

    def configure(self, request: Mapping[str, Any]):
        self.calls += 1
        if self.calls == 1:
            return self.initial, []
        return None, list(self.reconfigure_errors)


class CapturingHooks(Hooks):
    def __init__(self, ir: RuntimeConfigIR, turns: list[SolverTurn]) -> None:
        super().__init__(ir, turns)
        self.messages: list[dict[str, str]] = []
        self.compiled = None

    def solve(self, messages: list[dict[str, str]], compiled) -> SolverTurn:
        self.messages = list(messages)
        self.compiled = compiled
        return super().solve(messages, compiled)


class VerifyingHooks(CapturingHooks):
    def __init__(self, ir: RuntimeConfigIR, turns: list[SolverTurn]) -> None:
        super().__init__(ir, turns)
        self.all_messages: list[list[dict[str, str]]] = []
        self.verifier_packets: list[dict[str, Any]] = []

    def solve(self, messages: list[dict[str, str]], compiled) -> SolverTurn:
        self.all_messages.append(list(messages))
        return super().solve(messages, compiled)

    def verify(self, packet, compiled, ledger):
        self.verifier_packets.append(dict(packet))
        return json.dumps({
            "verdict": "needs_repair",
            "confidence": "high",
            "summary": "Repeated memory queries are not producing semantic progress.",
            "findings": [
                {
                    "severity": "high",
                    "summary": "No semantic progress after repeated memory queries.",
                    "evidence": ["no_progress_streak"],
                    "repair_instruction": "Stop querying memory and inspect or write a concrete artifact.",
                }
            ],
        })


class CountingVerifyingHooks(VerifyingHooks):
    def __init__(self, ir: RuntimeConfigIR, turns: list[SolverTurn]) -> None:
        super().__init__(ir, turns)
        self.reconfigure_calls = 0

    def reconfigure(self, request, compiled, ledger):
        self.reconfigure_calls += 1
        return super().reconfigure(request, compiled, ledger)


def test_envmap_builder_adds_file_tree_and_summary(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "input.csv").write_text("a,b\n1,2\n")
    (tmp_path / "check_result.py").write_text("print('ok')\n")

    envmap = build_envmap_from_task(str(tmp_path), "Summarise data")

    assert "/app" in envmap.file_tree
    assert "input.csv" in envmap.file_tree
    assert envmap.file_map_summary["extension_counts"][".csv"] == 1
    assert envmap.file_map_summary["extension_counts"][".py"] == 1
    assert "likely_inputs" not in envmap.file_map_summary
    assert "likely_tests_or_checkers" not in envmap.file_map_summary


def test_envmap_builder_surfaces_instruction_tool_and_output_hints(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")

    envmap = build_envmap_from_task(
        str(tmp_path),
        "Use python3 and openssl to write /app/out.txt from /app/input.txt.",
    )

    assert "instruction_tool_hints" not in envmap.file_map_summary
    assert "instruction_language_hints" not in envmap.file_map_summary
    assert envmap.file_map_summary["instruction_output_paths"] == ["/app/out.txt", "/app/input.txt"]
    assert envmap.task_metadata["instruction_path_references"]["output_paths"] == ["/app/out.txt", "/app/input.txt"]
    assert envmap.file_map_summary["instruction_referenced_paths"] == ["out.txt", "input.txt"]
    assert envmap.file_map_summary["instruction_referenced_missing_paths"] == ["out.txt", "input.txt"]
    assert envmap.file_map_summary["prompt_declared_output_paths"] == ["out.txt"]
    assert envmap.file_map_summary["prompt_declared_output_visible_paths"] == []
    assert envmap.file_map_summary["prompt_declared_output_missing_paths"] == ["out.txt"]
    assert "static_task_hints" not in envmap.task_metadata
    assert "required_tool_hints" not in envmap.task_metadata


def test_envmap_builder_tracks_alias_matches_for_referenced_paths(tmp_path: Path) -> None:
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "input.txt").write_text("hello\n")

    envmap = build_envmap_from_task(
        str(tmp_path),
        "Read /app/input.txt and write /app/out.txt.",
    )

    assert envmap.file_map_summary["instruction_referenced_paths"] == ["input.txt", "out.txt"]
    assert envmap.file_map_summary["instruction_referenced_visible_paths"] == ["input.txt"]
    assert envmap.file_map_summary["instruction_referenced_alias_matches"] == ["inputs/input.txt"]
    assert envmap.file_map_summary["instruction_referenced_missing_paths"] == ["out.txt"]
    assert envmap.file_map_summary["prompt_declared_output_missing_paths"] == ["out.txt"]


def test_architect_request_contains_runtime_manual_and_file_tree(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n")
    envmap = build_envmap_from_task(str(tmp_path), "Fix the app")
    compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))

    request = build_architect_request(envmap, compiler)

    assert request["envmap"]["file_tree"]
    assert request["envmap"]["file_map_summary"]["visible_file_count"] == 1
    assert request["runtime_manual"]["architect_role"].startswith("Configure")
    assert request["runtime_manual"]["memory"]["query_memory_always_available"] is True


def test_environment_probe_records_command_and_module_facts() -> None:
    executor = MemoryExecutor(workspace_root="/app")
    probe_names = _command_probe_names(("nginx", "r"))
    command_probe = (
        "for c in " + " ".join(shlex.quote(name) for name in probe_names) + "; do "
        "p=$(command -v \"$c\" 2>/dev/null || true); "
        "if [ -n \"$p\" ]; then printf '%s\\t%s\\n' \"$c\" \"$p\"; else printf '%s\\t\\n' \"$c\"; fi; "
        "done"
    )

    def commands_handler(_exec: MemoryExecutor, _command: str):
        return CommandResult(
            command=_command,
            exit_code=0,
            stdout="python\t\npython3\t/usr/bin/python3\nopenssl\t/usr/bin/openssl\nnginx\t/usr/sbin/nginx\nR\t/usr/bin/R\nRscript\t/usr/bin/Rscript\n",
        )

    def python_handler(_exec: MemoryExecutor, command: str):
        modules = {"pytest": True, "cryptography": False, "rdflib": False}
        return CommandResult(
            command=command,
            exit_code=0,
            stdout=json.dumps({"executable": "/usr/bin/python3", "version": "3.11.0", "modules": modules}),
        )

    executor.register_command(command_probe, commands_handler)
    executor.register_command(
        "python3 -c 'import importlib.util,json,sys; mods=[\"pytest\", \"cryptography\", \"rdflib\"]; print(json.dumps({'\"'\"'executable'\"'\"':sys.executable,'\"'\"'version'\"'\"':sys.version.split()[0],'\"'\"'modules'\"'\"':{m:bool(importlib.util.find_spec(m)) for m in mods}}))'",
        python_handler,
    )

    probe = probe_environment(executor, workspace_root="/app", extra_command_names=("nginx", "r"))

    assert probe["command_names"]["python"]["available"] is False
    assert probe["command_names"]["python3"]["available"] is True
    assert probe["command_names"]["nginx"]["available"] is True
    assert probe["command_names"]["R"]["available"] is True
    assert probe["task_hints"]["requested_command_names"] == ["nginx", "r"]
    assert probe["task_hints"]["missing_requested_commands"] == []
    assert probe["validation_guidance"]["preferred_python"] == "python3"
    assert any("python3" in note for note in probe["validation_guidance"]["notes"])


def test_environment_probe_expands_task_tool_hints() -> None:
    assert _expanded_command_names(("qemu", "r", "python3")) == (
        "qemu-system-i386",
        "qemu-system-x86_64",
        "qemu-img",
        "R",
        "Rscript",
        "python3",
        "python",
    )


def test_architect_request_contains_full_workbench_manual_contract(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n")
    envmap = build_envmap_from_task(
        str(tmp_path),
        "Fix the app and verify it",
        task_metadata={"environment_probe": {"schema_version": "environment_probe.v1"}},
    )
    compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))

    request = build_architect_request(envmap, compiler)
    manual = request["runtime_manual"]

    assert request["task_prompt"] == "Fix the app and verify it"
    assert request["envmap"]["file_tree"]
    assert request["envmap"]["file_map_summary"]["visible_file_count"] == 1
    assert request["envmap"]["environment_probe"]["schema_version"] == "environment_probe.v1"
    assert request["capability_index"]
    assert "tool_policy" not in manual["hard_configurable"]
    assert manual["tools"]["architect_does_not_choose_tools"] is True
    assert "run_command" in manual["tools"]["stable_core_solver_tools"]
    assert "solver_system_prompt" in manual["soft_configurable"]
    assert "hard_config" in manual["config_authority"]
    assert manual["role_contract"]["architect_does"][0] == "designs the task-specific solver system prompt"
    assert manual["memory"]["query_tool"] == "query_memory"
    assert "retrieve failed checks and completion findings" in manual["memory"]["query_use_cases"]
    assert "inspect_checks" in manual["tools"]["solver_callable_verification_tools"]
    assert "run_check" in manual["tools"]["solver_callable_verification_tools"]
    assert manual["verification"]["model_verifier_planned_as_internal_gate"] is True
    assert manual["verification"]["official_grader_outside_agent"] is True
    assert "arbitrary_model_authored_shell_as_gate_authority" in manual["verification"]["forbidden_checks"]
    assert "retrieval_augmented" in manual["context"]["supported_policies"]
    assert manual["context"]["compression"]["implemented"] is True
    assert manual["context"]["compression"]["planned_threshold"] == "60_percent_of_model_context_window"
    assert "architect_designed_solver_prompt" in manual["prompt_assembly"]["dynamic_task_prefix"]
    assert "what to inspect first" in manual["solver_prompt_requirements"]["verification_first_style"]
    assert manual["solver_prompt_requirements"]["not_a_global_progress_contract"] is True
    assert manual["environment_awareness"]["probe_source"] == "envmap.environment_probe"


def test_harness_config_ir_parser_accepts_vnext_shape() -> None:
    raw = json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "Write the output file.",
        "success_definition": "out.txt exists and contains result.",
        "solver_system_prompt": {
            "role": "Careful file writer",
            "workflow": ["inspect", "write", "verify"],
            "memory_use": ["query before repeating"],
        },
        "verifier_system_prompt": {
            "role": "Read-only verifier for out.txt",
            "success_criteria": ["out.txt exists and contains result"],
            "required_evidence": ["current out.txt content evidence"],
        },
        "evidence_requirements": ["out.txt exists", "out.txt contains result"],
        "minimum_completion_evidence": ["current out.txt content evidence"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "query_memory"]},
        "context_policy": {"mode": "retrieval_augmented"},
    })

    parsed = parse_harness_config_ir(raw)

    assert parsed.schema_version == "harness_config.v1"
    assert parsed.solver_system_prompt.render().startswith("Role: Careful file writer")
    assert parsed.context_policy.mode == "retrieval_augmented"


def test_harness_config_ir_parser_requires_architect_verifier_prompt() -> None:
    raw = json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "Write the output file.",
        "success_definition": "out.txt exists and contains result.",
        "solver_system_prompt": {"role": "Careful file writer"},
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "query_memory"]},
    })

    with pytest.raises(Exception, match="verifier_system_prompt.role is required"):
        parse_harness_config_ir(raw)


def test_harness_config_ir_parser_rejects_unknown_tool() -> None:
    raw = json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "x",
        "success_definition": "x",
        "solver_system_prompt": {"role": "x"},
        "verifier_system_prompt": {"role": "x", "success_criteria": ["x"], "required_evidence": ["x"]},
        "evidence_requirements": ["x"],
        "minimum_completion_evidence": ["x"],
        "tool_policy": {"enabled_tools": ["made_up_tool"]},
    })

    with pytest.raises(Exception, match="unknown enabled tools"):
        parse_harness_config_ir(raw)


def test_compiler_realizes_filtered_tool_schema_and_receipt() -> None:
    envmap = _env()
    compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
    compiled = compiler.compile(_ir(selected=("filesystem",)), envmap)

    tools = dict(compiled.action_schema)
    assert "read_file" in tools
    assert "write_file" in tools
    assert "query_memory" in tools  # always available
    assert "run_command" not in tools
    assert compiled.config_realization["tools_visible_to_solver"] == compiled.config_realization["tools_runtime_allowed"]


def test_runtime_rejects_tool_not_in_realized_schema() -> None:
    envmap = _env()
    executor = MemoryExecutor(workspace_root="/app")
    bad = _action("run_command", {"command": "echo hi"}, cap="shell")
    hooks = Hooks(_ir(selected=("filesystem",)), [SolverTurn(kind="act", summary="bad", actions=(bad,))])

    result = AetherNextKernel(max_steps=1).run(envmap, executor, hooks)

    validation = [r for r in result.receipts if r.kind == "turn_validation" or r.kind == "action_validation"]
    assert validation
    assert "unknown action kind: run_command" in validation[0].summary
    assert "echo hi" not in executor.command_history


def test_workbench_kernel_boot_path_uses_stable_core_tools_and_receipt() -> None:
    envmap = _env()
    executor = MemoryExecutor(workspace_root="/app")
    bad = _action("run_command", {"command": "echo hi"}, cap="shell")
    hooks = CapturingHooks(_ir(selected=("shell", "filesystem")), [
        SolverTurn(kind="act", summary="attempt disabled tool", actions=(bad,)),
    ])
    workbench = FakeWorkbenchArchitect(_workbench_config_json())

    result = AetherNextKernel(max_steps=1, workbench_architect=workbench).run(envmap, executor, hooks)

    assert result.status == "incomplete"
    assert hooks.compiled is not None
    tools = dict(hooks.compiled.action_schema)
    assert "read_file" in tools
    assert "write_file" in tools
    assert "query_memory" in tools
    assert "query_artifact_history" in tools
    assert "inspect_diff" in tools
    assert "record_observation" in tools
    assert "run_command" in tools
    assert "register_candidate" not in tools
    assert "run_experiment" not in tools

    sections = {message["content"].split("\n", 1)[0]: message["content"] for message in hooks.messages}
    assert "[solver_identity]" in sections
    assert "Workbench configured file solver" in sections["[solver_identity]"]
    assert "[tool_semantics]" in sections
    assert "[automatic_memory_manual]" in sections
    assert "[completion_submit_manual]" in sections
    assert "[envmap_file_tree]" in sections
    assert "[envmap_file_map_summary]" in sections
    assert "[configured_context_policy]" in sections
    assert "[configured_advisory_notes]" in sections
    assert "[action_schema]" in sections
    assert "query_memory" in sections["[action_schema]"]
    assert "run_command" in sections["[action_schema]"]
    assert "register_candidate" not in sections["[action_schema]"]
    assert hooks.messages[-1]["content"].startswith("[context_packet]\n")

    assert any(r.kind == "run_command" for r in result.receipts)
    assert "echo hi" in executor.command_history

    realizations = [r for r in result.receipts if r.kind == "config_realization"]
    assert realizations
    payload = realizations[0].payload["config_realization"]
    assert payload["architect_path"] == "workbench"
    assert payload["harness_config_schema_version"] == "harness_config.v1"
    assert payload["tools_visible_to_solver"] == payload["tools_runtime_allowed"]
    assert payload["tool_policy_mode"] == "stable_core"
    assert payload["architect_tool_selection_applied"] is False
    assert "run_command" in payload["tools_visible_to_solver"]
    assert payload["environment_probe_available"] is False
    assert "register_candidate" not in payload["tools_visible_to_solver"]
    assert payload["solver_prompt_hash"]
    assert "automatic_memory_manual" in payload["rendered_sections"]
    assert "configured_context_policy" in payload["rendered_sections"]
    assert "configured_advisory_notes" in payload["rendered_sections"]
    audit = payload["harness_config_realization_audit"]
    assert audit["has_silent_ignored_fields"] is False
    dispositions = audit["dispositions"]
    assert dispositions["tool_policy"]["tool_policy_mode"] == "fixed_kernel_surface"
    assert dispositions["tool_policy"]["canonical_schema_field"] is False
    assert dispositions["tool_policy"]["architect_tool_selection_applied"] is False
    assert dispositions["memory_policy"]["status"] == "realized_partial"
    assert dispositions["memory_policy"]["automatic_repeat_mode"] == "advisory"
    assert dispositions["verification_policy"]["status"] == "realized_partial"
    assert dispositions["model_verifier_policy"]["status"] == "realized"


def test_workbench_env_probe_reaches_config_realization_and_solver_prompt() -> None:
    envmap = EnvMap(
        task_prompt="Write /app/out.txt",
        workspace_root="/app",
        capabilities=dict(_CAPS),
        task_metadata={
            "environment_probe": {
                "schema_version": "environment_probe.v1",
                "validation_guidance": {"preferred_python": "python3"},
            }
        },
    )
    executor = MemoryExecutor(workspace_root="/app")
    hooks = CapturingHooks(_ir(selected=("shell", "filesystem")), [
        SolverTurn(kind="act", summary="write", actions=(_action("write_file", {"path": "out.txt", "content": "ok"}),)),
    ])
    workbench = FakeWorkbenchArchitect(_workbench_config_json())

    result = AetherNextKernel(max_steps=1, workbench_architect=workbench).run(envmap, executor, hooks)

    assert result.status == "incomplete"
    assert any("[environment_probe]" in msg["content"] and "python3" in msg["content"] for msg in hooks.messages)
    payload = [r for r in result.receipts if r.kind == "config_realization"][0].payload["config_realization"]
    assert payload["environment_probe_available"] is True
    assert payload["environment_probe"]["validation_guidance"]["preferred_python"] == "python3"


def test_no_progress_does_not_invoke_verifier_before_solver_submit() -> None:
    envmap = _env()
    executor = MemoryExecutor(workspace_root="/app")
    query = _action("query_memory", {"query": "nothing yet"}, cap="memory")
    turns = [
        SolverTurn(kind="act", summary="query", actions=(query,)),
        SolverTurn(kind="act", summary="query", actions=(query,)),
        SolverTurn(kind="act", summary="query", actions=(query,)),
        SolverTurn(kind="act", summary="query", actions=(query,)),
        SolverTurn(kind="act", summary="observe finding", actions=()),
    ]
    hooks = VerifyingHooks(_ir(selected=("shell", "filesystem")), turns)
    workbench = FakeWorkbenchArchitect(_workbench_config_json())

    result = AetherNextKernel(max_steps=5, workbench_architect=workbench).run(envmap, executor, hooks)

    assert hooks.verifier_packets == []
    assert not any(r.kind == "model_verifier_packet" for r in result.receipts)


def test_query_memory_is_available_in_workbench_kernel_path() -> None:
    envmap = _env()
    executor = MemoryExecutor(workspace_root="/app")
    query = _action("query_memory", {"query": "out txt"}, cap="memory")
    hooks = CapturingHooks(_ir(), [
        SolverTurn(kind="act", summary="query memory", actions=(query,)),
    ])
    workbench = FakeWorkbenchArchitect(_workbench_config_json(tools=["read_file", "write_file"]))

    result = AetherNextKernel(max_steps=1, workbench_architect=workbench).run(envmap, executor, hooks)

    assert hooks.compiled is not None
    assert "query_memory" in dict(hooks.compiled.action_schema)
    assert any(r.kind == "query_memory" and r.success for r in result.receipts)


def test_workbench_submit_requires_verifier_verdict_for_completion() -> None:
    envmap = _env()
    executor = MemoryExecutor(workspace_root="/app")
    hooks = CapturingHooks(_ir(), [
        SolverTurn(kind="act", summary="write", actions=(_action("write_file", {"path": "out.txt", "content": "ok"}),)),
        SolverTurn(kind="submit_outcome", summary="submit"),
    ])
    workbench = FakeWorkbenchArchitect(_workbench_config_json())

    result = AetherNextKernel(max_steps=2, workbench_architect=workbench).run(envmap, executor, hooks)

    assert result.status == "incomplete"
    assert executor.files["out.txt"] == "ok"
    assert not any(r.kind == "model_verifier_result" for r in result.receipts)
    assert any(r.kind == "verifier_required_for_completion" for r in result.receipts)


def test_workbench_planned_check_pass_does_not_auto_complete_without_solver_submit() -> None:
    envmap = _env()
    executor = MemoryExecutor(workspace_root="/app")
    hooks = CapturingHooks(_ir(selected=("shell", "filesystem")), [
        SolverTurn(kind="act", summary="write", actions=(_action("write_file", {"path": "out.txt", "content": "ok"}),)),
    ])
    raw_config = json.loads(_workbench_config_json(tools=["read_file", "write_file", "run_command", "query_memory"]))
    raw_config["verification_policy"] = {
        "visible_smoke_tests": [{"type": "file_exists", "path": "out.txt"}],
    }
    parsed_config = parse_harness_config_ir(json.dumps(raw_config))
    runtime_ir = harness_config_to_runtime_ir(parsed_config, envmap)
    assert runtime_ir.compiler_injected_checks
    for check in runtime_ir.compiler_injected_checks:
        executor.register_command(
            check.command,
            lambda _exec, command: CommandResult(command=command, exit_code=0, stdout="ok\n"),
        )
    workbench = FakeWorkbenchArchitect(json.dumps(raw_config))

    result = AetherNextKernel(max_steps=1, workbench_architect=workbench).run(envmap, executor, hooks)

    assert result.status == "incomplete"
    assert any(r.kind == "check_result" and r.success for r in result.receipts)
    assert not any(r.kind == "auto_submit" for r in result.receipts)
    assert not any(r.kind == "model_verifier_packet" for r in result.receipts)


def test_workbench_submit_does_not_auto_reconfigure_from_completion_gate_recommendation() -> None:
    envmap = _env_with_verify_commands("missing-tool --version")
    executor = MemoryExecutor(workspace_root="/app", files={"out.txt": "ok"})
    hooks = CountingVerifyingHooks(_ir(selected=("shell", "filesystem")), [
        SolverTurn(kind="submit_outcome", summary="submit"),
    ])
    workbench = FakeWorkbenchArchitect(_workbench_config_json())

    result = AetherNextKernel(max_steps=1, workbench_architect=workbench).run(envmap, executor, hooks)

    assert result.status == "incomplete"
    assert hooks.reconfigure_calls == 0
    assert hooks.verifier_packets and hooks.verifier_packets[0]["reason"] == "solver_submit"
    assert any(r.kind == "check_result" and r.failure_class == "missing_capability" for r in result.receipts)
    assert not any(r.kind == "reconfigure" for r in result.receipts)


def test_workbench_kernel_receipt_exposes_repair_warnings_and_rejected_items() -> None:
    envmap = _env()
    executor = MemoryExecutor(workspace_root="/app")
    hooks = CapturingHooks(_ir(), [
        SolverTurn(kind="submit_outcome", summary="submit"),
    ])
    raw = json.loads(_workbench_config_json())
    raw["verification_policy"] = {"visible_smoke_tests": [{"type": "grader_clone"}]}
    workbench = RepairingWorkbenchArchitect(json.dumps(raw))

    result = AetherNextKernel(max_steps=1, workbench_architect=workbench).run(envmap, executor, hooks)

    realizations = [r for r in result.receipts if r.kind == "config_realization"]
    assert realizations
    payload = realizations[0].payload["config_realization"]
    assert payload["workbench_repair_warning_codes"] == [UNSUPPORTED_VISIBLE_SMOKE_TEST_TYPE_CODE]
    assert payload["workbench_rejected_config_items"][0]["status"] == "quarantined"
    audit = payload["harness_config_realization_audit"]["dispositions"]["verification_policy"]
    assert audit["repair_warning_codes"] == [UNSUPPORTED_VISIBLE_SMOKE_TEST_TYPE_CODE]
    assert audit["rejected_config_items"][0]["status"] == "quarantined"


def test_workbench_architect_failure_is_agent_initialization_failure_end_to_end() -> None:
    """A failed workbench architect is not a task run.

    The old behavior silently ran with a baseline IR and emitted a fallback
    receipt. The canonical behavior is stricter: if the architect cannot build
    the workbench, the agent fails initialization with explicit blockers before
    the solver is called.
    """
    envmap = _env()
    executor = MemoryExecutor(workspace_root="/app")
    hooks = CapturingHooks(_ir(), [
        SolverTurn(kind="submit_outcome", summary="submit"),
    ])
    workbench = FailingWorkbenchArchitect(errors=["unterminated JSON string"])

    result = AetherNextKernel(max_steps=1, workbench_architect=workbench).run(envmap, executor, hooks)

    assert result.status == "config_invalid"
    assert result.step == 0
    assert "workbench_architect_configure_failed" in result.blockers
    assert "unterminated JSON string" in result.blockers
    assert result.receipts == ()
    assert hooks.messages == []
    assert hooks.compiled is None


def test_solver_sees_recent_command_stdout_in_live_kernel_loop() -> None:
    envmap = _env()
    executor = MemoryExecutor(workspace_root="/app")
    executor.register_command(
        "probe-proof",
        lambda _exec, _command: CommandResult("probe-proof", 0, stdout="PROOF_TOKEN=visible-now\n"),
    )
    workbench = FakeWorkbenchArchitect(_workbench_config_json())
    turns = [
        SolverTurn(
            kind="act",
            summary="run proof probe",
            actions=(
                _action("run_command", {"command": "probe-proof"}, cap="shell"),
            ),
        ),
        SolverTurn(kind="submit_outcome", summary="submit"),
    ]

    class MessageCaptureHooks(CapturingHooks):
        def __init__(self, ir: RuntimeConfigIR, turns: list[SolverTurn]) -> None:
            super().__init__(ir, turns)
            self.all_messages: list[list[dict[str, str]]] = []

        def solve(self, messages: list[dict[str, str]], compiled) -> SolverTurn:
            self.all_messages.append(list(messages))
            return super().solve(messages, compiled)

    hooks = MessageCaptureHooks(_ir(selected=("shell", "filesystem")), turns)

    result = AetherNextKernel(max_steps=3, workbench_architect=workbench).run(envmap, executor, hooks)

    assert result.status == "incomplete"
    assert len(hooks.all_messages) >= 2
    second_turn_context = hooks.all_messages[1][-1]["content"]
    assert second_turn_context.startswith("[context_packet]\n")
    assert "command_results" in second_turn_context
    assert "PROOF_TOKEN=visible-now" in second_turn_context


def test_model_hooks_final_solver_messages_include_mechanical_runtime_contract_only() -> None:
    envmap = _env()
    compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
    config = parse_harness_config_ir(_workbench_config_json())
    from aether_next.workbench_compile import harness_config_to_runtime_ir

    ir = harness_config_to_runtime_ir(config, envmap)
    compiled = compiler.compile(ir, envmap)
    messages = AetherNextKernel().build_solver_messages(compiled, {"pending_checks": []})
    captured: dict[str, Any] = {}

    def architect_model(_messages, *, max_output_tokens=8000):
        return "{}"

    def solver_model(final_messages, *, max_output_tokens=8000):
        captured["messages"] = final_messages
        return json.dumps({"kind": "submit_outcome", "summary": "submit"})

    hooks = ModelHooks(architect_model, solver_model)
    hooks.solve(messages, compiled)

    final_messages = captured["messages"]
    assert any("[automatic_memory_manual]" in msg["content"] for msg in final_messages)
    assert any("[completion_submit_manual]" in msg["content"] for msg in final_messages)
    assert any("[solver_turn_contract]" in msg["content"] for msg in final_messages)
    assert any("[solver_identity]" in msg["content"] and "Self-verification" in msg["content"] for msg in final_messages)
    assert any("active_completion_findings" in msg["content"] for msg in final_messages)
    assert not any("Available action kinds" in msg["content"] for msg in final_messages)


def test_query_memory_returns_structured_typed_results() -> None:
    ledger = ExecutionLedger()
    ledger.record(Receipt(
        receipt_id="r1",
        step=1,
        kind="read_file",
        success=True,
        summary="read university_graph.ttl",
        payload={"path": "university_graph.ttl", "content_hash": "abc", "excerpt": "Professor teaches Course"},
    ))
    ledger.record(Receipt(
        receipt_id="r2",
        step=2,
        kind="check_result",
        success=False,
        summary="check schema failed",
        failure_class="schema_mismatch",
        payload={"check_id": "schema-out", "detail": "missing key G", "passed": False},
    ))

    path_hits = ledger.query_memory("university graph ttl")
    check_hits = ledger.query_memory("schema mismatch G")

    assert path_hits[0]["receipt_id"] == "r1"
    assert path_hits[0]["path"] == "university_graph.ttl"
    assert path_hits[0]["content_hash"] == "abc"
    assert check_hits[0]["check_id"] == "schema-out"
    assert check_hits[0]["failure_class"] == "schema_mismatch"


def test_solver_requested_reconfigure_is_rejected_not_routed_through_architect() -> None:
    """Solver-requested reconfiguration is no longer part of the certified path.

    The solver may report a blocker, but it may not trigger a reconfigure or
    route through either the workbench architect or the legacy fallback path.
    """
    executor = MemoryExecutor(workspace_root="/app")
    legacy_ir = _ir()
    workbench = FakeWorkbenchArchitect(_workbench_config_json())
    turns = [
        SolverTurn(kind="request_reconfigure", summary="need fresh config"),
        SolverTurn(kind="submit_outcome", summary="submit"),
    ]
    hooks = CapturingHooks(legacy_ir, turns)

    result = AetherNextKernel(max_steps=3, workbench_architect=workbench).run(_env(), executor, hooks)

    assert result.status == "incomplete"
    assert not [r for r in result.receipts if r.kind == "reconfigure" and r.success]
    denied = [r for r in result.receipts if r.kind == "turn_validation" and not r.success]
    assert denied and "unknown turn kind" in denied[0].summary


def test_solver_requested_reconfigure_does_not_invoke_failed_workbench_repair() -> None:
    executor = MemoryExecutor(workspace_root="/app")
    workbench = FlakyWorkbenchArchitect(
        _workbench_config_json(),
        reconfigure_errors=["unterminated JSON string during reconfigure"],
    )
    original_ir = _ir()
    turns = [
        SolverTurn(kind="request_reconfigure", summary="need fresh config"),
        SolverTurn(kind="submit_outcome", summary="submit"),
    ]
    hooks = CapturingHooks(original_ir, turns)

    result = AetherNextKernel(max_steps=3, workbench_architect=workbench).run(_env(), executor, hooks)

    assert result.status == "incomplete"
    assert any(
        r.kind == "turn_validation" and not r.success and "unknown turn kind" in r.summary
        for r in result.receipts
    )
    assert not any(r.kind == "reconfigure_validation" for r in result.receipts)
    assert workbench.calls == 1





def test_stable_core_includes_every_generic_capability_tool() -> None:
    """The stable core is the full generic workbench.  A missing tool here is
    a hidden harness ceiling (the 2026-07-05 Docker smoke found service tasks
    unreachable because launch_process was absent)."""
    from aether_next.workbench_compile import STABLE_CORE_SOLVER_TOOLS

    required = {
        "read_file", "write_file", "run_command",
        "launch_process", "probe_service", "stop_process",
        "inspect_artifact", "bootstrap_acquire",
        "query_memory", "run_check", "inspect_checks",
    }
    missing = required - set(STABLE_CORE_SOLVER_TOOLS)
    assert not missing, f"stable core lost generic tools: {sorted(missing)}"
