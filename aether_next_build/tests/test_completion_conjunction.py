"""Production-bound tests for the final completion conjunction.

A Verifier verdict is semantic evidence only.  It must never override a fresh
mechanical blocker returned by CompletionGate.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

import aether_next.kernel as kernel_module
from aether_next.completion import Blocker, CompletionDecision
from aether_next.execution import MemoryExecutor
from aether_next.kernel import AetherNextKernel, _completed_result
from aether_next.ledger import ExecutionLedger
from aether_next.runtime_ir import CapabilityDescriptor, EnvMap, SolverTurn
from aether_next.workbench_config import parse_harness_config_ir


class _SubmitHooks:
    def solve(self, messages: list[dict[str, str]], compiled: Any) -> SolverTurn:
        return SolverTurn(kind="submit_outcome", summary="submit current state")


class _StaticWorkbenchArchitect:
    def __init__(self) -> None:
        self._config = parse_harness_config_ir(json.dumps({
            "schema_version": "harness_config.v1",
            "task_understanding": "Produce the required task result.",
            "success_definition": "The current task state satisfies every requirement.",
            "solver_system_prompt": {
                "role": "Task solver",
                "workflow": ["inspect", "implement", "verify", "submit"],
                "self_verification": ["submit only after current evidence supports completion"],
                "memory_use": ["use retained evidence before repeating work"],
                "stop_conditions": ["all requirements are currently satisfied"],
                "avoid": ["do not submit on proxy evidence"],
            },
            "verifier_system_prompt": {
                "role": "Independent current-state verifier",
                "success_criteria": ["all task requirements are currently satisfied"],
                "required_evidence": ["current inspectable state supports completion"],
                "false_positive_traps": ["shape or liveness can hide semantic failure"],
                "verdict_guidance": ["completed requires current evidence"],
                "feedback_guidance": ["identify the blocking requirement and evidence"],
            },
            "evidence_requirements": ["current state evidence"],
            "false_positive_risks": ["proxy evidence"],
            "minimum_completion_evidence": ["independent current-state inspection"],
            "tool_policy": {"enabled_tools": []},
            "context_policy": {"mode": "default_bounded"},
            "model_verifier_policy": {"enabled": True, "runs_on": ["solver_submit"]},
        }))

    def configure(self, request: Mapping[str, Any]):
        return self._config, []


class _AlwaysBlockedGate:
    def __init__(self, blocker_code: str) -> None:
        self.blocker_code = blocker_code
        self.calls = 0

    def evaluate(self, compiled: Any, ledger: Any, alerts: Any) -> CompletionDecision:
        self.calls += 1
        return CompletionDecision(
            ready=False,
            blockers=(Blocker(self.blocker_code, "still blocked", "test"),),
        )


def _envmap() -> EnvMap:
    return EnvMap(
        task_prompt="Produce the required task result.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "run commands"),
            "filesystem": CapabilityDescriptor("filesystem", "read and write files"),
        },
    )


def test_completed_result_rejects_non_ready_decision() -> None:
    compiled = SimpleNamespace(planned_checks=lambda: (), env_digest="test")
    decision = CompletionDecision(
        ready=False,
        blockers=(Blocker("missing_artifacts", "result.json", "objective_graph"),),
    )

    with pytest.raises(ValueError, match="requires a ready completion decision"):
        _completed_result(0, 0, decision, compiled, ExecutionLedger())


@pytest.mark.parametrize("blocker_code", [
    "missing_artifacts",
    "missing_authoritative_check",
    "missing_clause_evidence",
    "stale_clause_evidence",
    "active_verifier_finding",
    "unsatisfied_obligations",
    "integrity_violation",
    "schema_unverified",
    "process_generation_mismatch",
    "incomplete_state_capture",
])
def test_verifier_completed_never_overrides_fresh_gate_blocker(
    monkeypatch: pytest.MonkeyPatch,
    blocker_code: str,
) -> None:
    """Exercise the canonical Workbench submit path for every blocker family."""

    monkeypatch.setattr(
        kernel_module,
        "run_model_verifier_if_available",
        lambda *args, **kwargs: SimpleNamespace(
            verdict="completed",
            findings=(),
            completion_evidence=(),
        ),
    )

    kernel = AetherNextKernel(
        max_steps=1,
        workbench_architect=_StaticWorkbenchArchitect(),
        certified_production=True,
    )
    fake_gate = _AlwaysBlockedGate(blocker_code)
    kernel.completion_gate = fake_gate

    result = kernel.run(_envmap(), MemoryExecutor(workspace_root="/app"), _SubmitHooks())

    assert result.status != "completed"
    assert fake_gate.calls >= 2, "gate must be evaluated before and after Verifier"
    receipts = [
        receipt for receipt in result.receipts
        if receipt.kind == "verifier_completed_gate_not_ready"
    ]
    assert receipts, "fresh non-ready decision must be recorded"
    assert receipts[-1].failure_class == "completion_gate_not_ready"
    assert receipts[-1].payload["blockers"][0]["code"] == blocker_code
