from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from proteus.aether_next.compiler import CapabilityRegistry, ConfigCompiler
from proteus.aether_next.context_compiler import ContextCompiler
from proteus.aether_next.environment_probe import probe_environment
from proteus.aether_next.envmap_builder import build_envmap_from_task
from proteus.aether_next.execution import CommandResult, MemoryExecutor
from proteus.aether_next.kernel import AetherNextKernel
from proteus.aether_next.ledger import ExecutionLedger, Receipt
from proteus.aether_next.model_hooks import ModelHooks, ModelOutputError
from proteus.aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CompiledRuntime,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)
from proteus.aether_next.verifier_packets import build_verifier_packet


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Create out.txt.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands"),
            "filesystem": CapabilityDescriptor("filesystem", "Files"),
        },
    )


def _compiled() -> CompiledRuntime:
    env = _env()
    ir = RuntimeConfigIR(
        architect_summary="test",
        solver_identity_prompt="solver prompt",
        verifier_identity_prompt="verifier prompt",
        selected_capabilities=("shell", "filesystem"),
        success_definition="out.txt exists and contains OK",
        evidence_requirements=("read current out.txt",),
        minimum_completion_evidence=("current file state",),
    )
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)


def _model(response: str):
    def call(messages: list[dict[str, str]], *, max_output_tokens: int = 8000) -> str:
        return response
    return call


def _valid_submit() -> str:
    return json.dumps({"kind": "submit_outcome", "summary": "ready"})


def test_solver_parse_failure_raises_and_preserves_raw_output() -> None:
    hooks = ModelHooks(architect_model=_model("{}"), solver_model=_model("not json"))
    with pytest.raises(ModelOutputError):
        hooks.solve([], _compiled())
    assert getattr(hooks, "last_raw_solver_output") == "not json"
    assert hooks.last_parse_errors


class _ParseThenValidHooks:
    def __init__(self, ir: RuntimeConfigIR) -> None:
        self.ir = ir
        self.calls = 0
        self.last_raw_solver_output = ""

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        return self.ir

    def solve(self, messages: list[dict[str, str]], compiled: CompiledRuntime) -> SolverTurn:
        self.calls += 1
        if self.calls == 1:
            self.last_raw_solver_output = "broken turn"
            raise ModelOutputError("no JSON object found")
        return SolverTurn(kind="act", summary="write", actions=(ActionRequest(
            action_id="a-write",
            kind="write_file",
            capability_id="filesystem",
            arguments={"path": "out.txt", "content": "OK"},
            intent="write output",
            expected_observation="file written",
            if_fail_next="report blocker",
        ),))


def test_kernel_records_parse_error_and_retries_once() -> None:
    env = _env()
    ir = RuntimeConfigIR(
        architect_summary="test",
        solver_identity_prompt="solver prompt",
        verifier_identity_prompt="verifier prompt",
        selected_capabilities=("filesystem",),
    )
    hooks = _ParseThenValidHooks(ir)
    result = AetherNextKernel(max_steps=1).run(env, MemoryExecutor(workspace_root="/app"), hooks)
    kinds = [r.kind for r in result.receipts]
    assert "solver_parse_error" in kinds
    assert "write_file" in kinds
    receipt = next(r for r in result.receipts if r.kind == "solver_parse_error")
    assert receipt.payload["redacted_output"] == "broken turn"
    assert "raw_output" not in receipt.payload
    assert receipt.payload["raw_output_storage"] == "protected_provider_evidence_only"


class _ReconfigHooks:
    def __init__(self, ir: RuntimeConfigIR) -> None:
        self.ir = ir
        self.reconfigure_calls = 0

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        return self.ir

    def solve(self, messages: list[dict[str, str]], compiled: CompiledRuntime) -> SolverTurn:
        return SolverTurn(kind="request_reconfigure", summary="no")

    def reconfigure(self, request: Mapping[str, Any], compiled: CompiledRuntime, ledger: ExecutionLedger) -> RuntimeConfigIR:
        self.reconfigure_calls += 1
        return self.ir


def test_solver_requested_reconfigure_is_visible_denial_not_config_change() -> None:
    env = _env()
    ir = RuntimeConfigIR(
        architect_summary="test",
        solver_identity_prompt="solver prompt",
        verifier_identity_prompt="verifier prompt",
        selected_capabilities=("filesystem",),
    )
    hooks = _ReconfigHooks(ir)
    result = AetherNextKernel(max_steps=1).run(env, MemoryExecutor(workspace_root="/app"), hooks)
    assert hooks.reconfigure_calls == 0
    assert result.reconfigurations == 0
    denied = [r for r in result.receipts if r.kind == "turn_validation" and not r.success]
    assert denied and "unknown turn kind" in denied[0].summary


def test_command_output_has_full_handles_and_context_floor() -> None:
    env = _env()
    executor = MemoryExecutor(workspace_root="/app")
    long = "A" * 9000 + "TAIL"
    executor.register_command("make noise", lambda ex, cmd: CommandResult(command=cmd, exit_code=0, stdout=long))
    ir = RuntimeConfigIR(
        architect_summary="test",
        solver_identity_prompt="solver prompt",
        verifier_identity_prompt="verifier prompt",
        selected_capabilities=("shell",),
    )
    action = ActionRequest(
        action_id="a-run",
        kind="run_command",
        capability_id="shell",
        arguments={"command": "make noise"},
        intent="produce output",
        expected_observation="output",
        if_fail_next="inspect",
    )
    class Hooks:
        def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR: return ir
        def solve(self, messages: list[dict[str, str]], compiled: CompiledRuntime) -> SolverTurn:
            return SolverTurn(kind="act", summary="run", actions=(action,))
    result = AetherNextKernel(max_steps=1).run(env, executor, Hooks())
    cmd = next(r for r in result.receipts if r.kind == "run_command")
    assert cmd.payload["stdout_full"] == long
    assert cmd.payload["stdout_handle"]
    ctx = ContextCompiler().compile(_compiled(), ExecutionLedger(), [])
    # Separate ledger context check with actual run ledger.
    ledger = ExecutionLedger()
    for r in result.receipts: ledger.record(r)
    ctx = ContextCompiler().compile(_compiled(), ledger, [])
    assert ctx["output_handles"][0]["handle"] == cmd.payload["stdout_handle"]
    assert "TAIL" in ctx["command_results"][0]["stdout"]


def test_envmap_network_scope_is_unknown_unless_probe_proves_value() -> None:
    envmap = build_envmap_from_task("/does/not/exist", "Do task", task_metadata={})
    assert envmap.network_scope == "unknown"
    probed = build_envmap_from_task(
        "/does/not/exist",
        "Do task",
        task_metadata={"environment_probe": {"network": {"status": "probed_false"}}},
    )
    assert probed.network_scope == "unenforced_probe_observation"


def test_verifier_packet_has_no_solver_journey_fields() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt(
        "cmd", 1, "run_command", True, "solver command",
        payload={"command": "echo solver says OK", "stdout": "solver proof", "stdout_handle": "1:a:stdout", "stdout_full": "solver proof"},
    ))
    packet = build_verifier_packet(compiled, ledger, step=2, reason="solver_submit", envmap=_env())
    for forbidden in (
        "solver_claim",
        "submit_summary",
        "solver_proof",
        "solver_authored_evidence",
        "recent_receipts",
        "latest_file_reads",
        "command_results",
        "automatic_memory_findings",
        "no_progress_controls",
        "artifact_history",
        "memory_events",
        "observations",
        "solver_system_prompt",
    ):
        assert forbidden not in packet
    assert packet["state_inspection_handles"]
