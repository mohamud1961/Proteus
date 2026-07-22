"""Tests for one causal action frontier or one certified observation batch."""
from __future__ import annotations

from typing import Any, Mapping

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.ledger import ExecutionLedger
from aether_next.observation_batch import MAX_OBSERVATIONS_PER_BATCH, execute_observation_batch
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CompletionPolicy,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)


def _envmap() -> EnvMap:
    return EnvMap(
        task_prompt="Inspect the task state.",
        workspace_root="/app",
        capabilities={
            "filesystem": CapabilityDescriptor("filesystem", "read and write files"),
            "shell": CapabilityDescriptor("shell", "run commands"),
        },
    )


def _ir() -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="Inspect current task state.",
        solver_identity_prompt="Use one causal frontier at a time.",
        selected_capabilities=("filesystem", "shell"),
        completion_policy=CompletionPolicy(
            require_authoritative_check=False,
            require_all_obligations=False,
            require_recent_progress=False,
            require_clean_integrity=True,
        ),
    )


def _compiled():
    envmap = _envmap()
    return envmap, ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(_ir(), envmap)


def _action(kind: str, arguments: Mapping[str, Any], action_id: str = "a1") -> ActionRequest:
    return ActionRequest(
        action_id=action_id,
        kind=kind,
        capability_id="filesystem",
        arguments=dict(arguments),
        intent="inspect current state",
        expected_observation="current evidence",
        if_fail_next="narrow the inspection",
    )


def test_act_turn_rejects_multiple_arbitrary_actions() -> None:
    turn = SolverTurn(
        kind="act",
        summary="try two frontiers",
        actions=(
            _action("read_file", {"path": "a.txt"}, "a1"),
            _action("write_file", {"path": "b.txt", "content": "x"}, "a2"),
        ),
    )

    assert "act turns require exactly one action frontier" in turn.validate()


def test_certified_reads_share_one_mutation_generation() -> None:
    envmap, compiled = _compiled()
    ledger = ExecutionLedger()
    executor = MemoryExecutor(
        workspace_root="/app",
        files={"a.txt": "A", "b.txt": "B"},
    )
    action = _action("observe_batch", {
        "operations": [
            {"request_id": "read-a", "kind": "read_file", "arguments": {"path": "a.txt"}},
            {"request_id": "read-b", "kind": "read_file", "arguments": {"path": "b.txt"}},
        ]
    }, "batch-1")

    receipts = execute_observation_batch(
        AetherNextKernel(), action, step=0, compiled=compiled,
        executor=executor, envmap=envmap, ledger=ledger,
    )

    reads = [receipt for receipt in receipts if receipt.kind == "read_file"]
    assert len(reads) == 2
    assert {receipt.payload["path"] for receipt in reads} == {"a.txt", "b.txt"}
    assert len({receipt.payload["observed_mutation_generation"] for receipt in reads}) == 1
    assert not any(receipt.state_change for receipt in receipts)
    summary = [receipt for receipt in receipts if receipt.kind == "observation_batch_result"][-1]
    assert summary.success is True
    assert summary.payload["complete_result_set"] is True


def test_batch_rejects_effect_unknown_or_mutating_child_before_execution() -> None:
    envmap, compiled = _compiled()
    ledger = ExecutionLedger()
    executor = MemoryExecutor(workspace_root="/app")
    action = _action("observe_batch", {
        "operations": [
            {"kind": "write_file", "arguments": {"path": "owned.txt", "content": "bad"}},
        ]
    }, "batch-mutating")

    receipts = execute_observation_batch(
        AetherNextKernel(), action, step=0, compiled=compiled,
        executor=executor, envmap=envmap, ledger=ledger,
    )

    assert len(receipts) == 1
    assert receipts[0].kind == "action_validation"
    assert receipts[0].success is False
    assert "owned.txt" not in executor.files


def test_batch_returns_successes_and_failures_together() -> None:
    envmap, compiled = _compiled()
    ledger = ExecutionLedger()
    executor = MemoryExecutor(workspace_root="/app", files={"present.txt": "yes"})
    action = _action("observe_batch", {
        "operations": [
            {"request_id": "present", "kind": "read_file", "arguments": {"path": "present.txt"}},
            {"request_id": "missing", "kind": "read_file", "arguments": {"path": "missing.txt"}},
        ]
    }, "batch-partial")

    receipts = execute_observation_batch(
        AetherNextKernel(), action, step=0, compiled=compiled,
        executor=executor, envmap=envmap, ledger=ledger,
    )

    reads = [receipt for receipt in receipts if receipt.kind == "read_file"]
    assert [receipt.success for receipt in reads] == [True, False]
    assert receipts[-1].kind == "observation_batch_result"
    assert receipts[-1].payload["complete_result_set"] is True


def test_batch_is_bounded() -> None:
    envmap, compiled = _compiled()
    action = _action("observe_batch", {
        "operations": [
            {"kind": "read_file", "arguments": {"path": f"{index}.txt"}}
            for index in range(MAX_OBSERVATIONS_PER_BATCH + 1)
        ]
    }, "batch-too-large")

    receipts = execute_observation_batch(
        AetherNextKernel(), action, step=0, compiled=compiled,
        executor=MemoryExecutor(workspace_root="/app"),
        envmap=envmap, ledger=ExecutionLedger(),
    )

    assert len(receipts) == 1
    assert receipts[0].kind == "action_validation"
    assert "requires 1-8 operations" in receipts[0].summary


class _Hooks:
    def __init__(self, turns: list[SolverTurn]) -> None:
        self.turns = list(turns)

    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        return _ir()

    def solve(self, messages: list[dict[str, str]], compiled: Any) -> SolverTurn:
        return self.turns.pop(0) if self.turns else SolverTurn(
            kind="submit_outcome", summary="submit"
        )


def test_kernel_records_one_batch_decision_and_complete_result_before_next_turn() -> None:
    action = _action("observe_batch", {
        "operations": [
            {"kind": "read_file", "arguments": {"path": "a.txt"}},
            {"kind": "read_file", "arguments": {"path": "b.txt"}},
        ]
    }, "batch-kernel")
    hooks = _Hooks([
        SolverTurn(
            kind="act",
            summary="inspect both inputs",
            evidence_gap="contents of both inputs are unknown",
            actions=(action,),
        ),
        SolverTurn(kind="submit_outcome", summary="submit"),
    ])
    executor = MemoryExecutor(
        workspace_root="/app", files={"a.txt": "A", "b.txt": "B"},
    )

    result = AetherNextKernel(max_steps=3).run(_envmap(), executor, hooks)

    decisions = [receipt for receipt in result.receipts if receipt.kind == "solver_decision_state"]
    assert len(decisions) == 1
    assert decisions[0].payload["evidence_gap"] == "contents of both inputs are unknown"
    batch_results = [receipt for receipt in result.receipts if receipt.kind == "observation_batch_result"]
    assert len(batch_results) == 1
    assert batch_results[0].payload["operation_count"] == 2
    progress = [receipt for receipt in result.receipts if receipt.kind == "solver_progress_assessment"]
    assert progress[-1].payload["classification"] == "new_evidence"
    assert result.accounting is not None
    assert result.accounting["solver_accepted_task_actions"] == 1
