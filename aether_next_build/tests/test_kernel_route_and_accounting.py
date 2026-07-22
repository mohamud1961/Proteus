from __future__ import annotations

from typing import Any, Mapping

from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.runtime_ir import ActionRequest, CapabilityDescriptor, EnvMap, RuntimeConfigIR, SolverTurn


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Produce the required output.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "files", tool_names=("read_file", "write_file")),
        },
    )


def _proof_ir() -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="prove the interface",
        solver_identity_prompt="work carefully",
        selected_capabilities=("shell", "filesystem"),
        semantic_clause_coverage=({
            "clause_id": "protocol",
            "solver_handling": "implement exact contract",
            "verifier_check": "independent client",
        },),
        semantic_verifier_checks=({
            "clause_id": "protocol",
            "inspection_route": "overlay_run_command:python3 independent_client.py",
            "fallback_route": None,
            "falsification_check": "the independent client rejects bad data",
            "required_evidence_class": "exact_contract",
        },),
        semantic_false_positive_traps=("a live process is not protocol proof",),
    )


class _Hooks:
    def __init__(self, turns: list[SolverTurn], ir: RuntimeConfigIR | None = None) -> None:
        self.turns = list(turns)
        self.ir = ir or RuntimeConfigIR(
            architect_summary="simple", solver_identity_prompt="work",
            selected_capabilities=("shell", "filesystem"),
        )
        self.solve_calls = 0

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        return self.ir

    def solve(self, messages: list[dict[str, str]], compiled: Any) -> SolverTurn:
        del messages, compiled
        self.solve_calls += 1
        return self.turns.pop(0) if self.turns else SolverTurn(kind="submit_outcome", summary="submit")


class _ParityExecutor:
    def run_command(self, command: str, *, cwd: str | None = None, timeout_s: int = 30) -> CommandResult:
        del cwd, timeout_s
        if command.startswith("command -v python3"):
            return CommandResult(command, 127, "", "not found")
        return CommandResult(command, 0, "", "")

    def exists(self, path: str) -> bool:
        return True

    def read_file(self, path: str) -> str:
        return ""

    def probe_process(self, target: str):
        return None


def test_failed_route_preflight_stops_before_solver_call() -> None:
    hooks = _Hooks([], _proof_ir())
    result = AetherNextKernel(max_steps=3).run(_env(), _ParityExecutor(), hooks)

    assert result.status == "config_invalid"
    assert hooks.solve_calls == 0
    receipt = next(receipt for receipt in result.receipts if receipt.kind == "route_preflight")
    assert receipt.success is False
    assert {row["code"] for row in receipt.payload["issues"]} == {
        "solver_route_preflight_failed", "verifier_route_preflight_failed",
    }


def test_solver_turns_and_accepted_actions_are_separate_receipted_counters() -> None:
    actions = tuple(
        ActionRequest(
            action_id=f"observe-{index}", kind="record_observation", capability_id="shell",
            arguments={"observation": f"fact {index}"}, intent="record", expected_observation="stored",
            if_fail_next="stop",
        )
        for index in (1, 2)
    )
    hooks = _Hooks([
        SolverTurn(kind="act", summary="first observation", actions=(actions[0],)),
        SolverTurn(kind="act", summary="second observation", actions=(actions[1],)),
    ])
    result = AetherNextKernel(
        max_steps=3, max_solver_turns=2, max_accepted_task_actions=1,
    ).run(_env(), MemoryExecutor(workspace_root="/app"), hooks)

    assert result.status == "solver_turn_budget_exhausted"
    assert result.accounting == {
        "solver_accepted_task_actions": 1,
        "solver_provider_turns": 2,
        "solver_refused_actions": 1,
    }
    events = [receipt.payload["event"] for receipt in result.receipts if receipt.kind == "runtime_accounting"]
    assert "accepted_for_dispatch" in events
    assert "accepted_action_budget_exhausted" in events
