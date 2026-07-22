"""Bounded verifier-disagreement protocol.

When the identical non-empty finding set survives STALEMATE_ROUNDS
consecutive verification rounds despite intervening solver evidence, the run
terminates with status ``verifier_stalemate`` and a full disagreement record.
The harness never adjudicates; the classifier labels it verification_failure,
never model_limit.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from aether_next.classifier import HarnessLimiterClassifier
from aether_next.execution import MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    EnvMap,
    SolverTurn,
)
from aether_next.workbench_config import parse_harness_config_ir


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Write /app/out.txt",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
        },
    )


def _config_json() -> str:
    return json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "write output",
        "success_definition": "out.txt is correct.",
        "solver_system_prompt": {
            "role": "solver", "workflow": ["write", "submit"],
            "self_verification": ["check"], "memory_use": ["none"],
            "stop_conditions": ["after write"],
        },
        "verifier_system_prompt": {
            "role": "verifier", "success_criteria": ["correct out.txt"],
            "required_evidence": ["state"], "false_positive_traps": ["presence"],
            "verdict_guidance": ["state"], "feedback_guidance": ["concrete"],
        },
        "evidence_requirements": ["current out.txt state"],
        "false_positive_risks": ["wrong content"],
        "minimum_completion_evidence": ["state"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "run_command"]},
        "context_policy": {"mode": "retrieval_augmented"},
        "model_verifier_policy": {"enabled": True},
    })


class _Workbench:
    def configure(self, request: Mapping[str, Any]):
        return parse_harness_config_ir(_config_json()), []


def _finding(fid: str) -> dict[str, Any]:
    return {
        "finding_id": fid,
        "summary": f"unresolved issue {fid}",
        "evidence": ["observed state contradicts the claim"],
        "repair_instruction": "fix the artifact",
        "applies_to": ["out.txt"],
        "priority": "blocking",
    }


class _StubbornHooks:
    """Solver alternates evidence-producing acts and submits; verifier keeps
    returning the configured finding sets."""

    def __init__(self, finding_sets: list[list[dict[str, Any]]]) -> None:
        self._finding_sets = list(finding_sets)
        self.verify_calls = 0
        self._writes = 0

    def architect(self, request: Mapping[str, Any]):
        raise AssertionError("workbench mode must not call hooks.architect")

    def solve(self, messages: list[dict[str, str]], compiled) -> SolverTurn:
        # Alternate: act (fresh evidence), submit, act, submit ...
        self._writes += 1
        if self._writes % 2 == 1:
            return SolverTurn(kind="act", summary=f"rewrite attempt {self._writes}", actions=(ActionRequest(
                action_id=f"a-{self._writes}", kind="write_file", capability_id="filesystem",
                arguments={"path": "out.txt", "content": f"attempt {self._writes}"},
                intent="address finding", expected_observation="file updated",
                if_fail_next="report blocker",
            ),))
        return SolverTurn(kind="submit_outcome", summary="resubmitting after repair")

    def verify(self, packet, compiled, ledger):
        self.verify_calls += 1
        if self._finding_sets:
            findings = self._finding_sets.pop(0)
        else:
            findings = [_finding("f-stuck")]
        return json.dumps({
            "verdict": "needs_repair",
            "confidence": "high",
            "summary": "state still contradicts the requirement",
            "findings": findings,
        })


def test_identical_findings_across_rounds_terminate_as_stalemate() -> None:
    hooks = _StubbornHooks([[_finding("f-stuck")]] * 10)
    result = AetherNextKernel(max_steps=20, workbench_architect=_Workbench()).run(
        _env(), MemoryExecutor(workspace_root="/app"), hooks,
    )

    assert result.status == "verifier_stalemate"
    assert hooks.verify_calls == AetherNextKernel.STALEMATE_ROUNDS
    stalemate = next(r for r in result.receipts if r.kind == "verifier_stalemate")
    assert stalemate.failure_class == "verifier_stalemate"
    assert stalemate.payload["finding_ids"] == ["f-stuck"]
    assert stalemate.payload["rounds"] == AetherNextKernel.STALEMATE_ROUNDS
    assert stalemate.payload["final_verifier_verdict"]["verdict"] == "needs_repair"
    assert result.blockers == ("f-stuck",)


def test_changing_findings_are_progress_not_stalemate() -> None:
    # Each round resolves the prior finding and raises a new one: disagreement
    # is moving, so the bounded protocol must not fire.
    sets = [
        [_finding("f-1")],
        [_finding("f-2")],
        [_finding("f-3")],
        [_finding("f-4")],
    ]
    hooks = _StubbornHooks(sets)
    result = AetherNextKernel(max_steps=8, workbench_architect=_Workbench()).run(
        _env(), MemoryExecutor(workspace_root="/app"), hooks,
    )
    assert result.status != "verifier_stalemate"


def test_stalemate_is_classified_verification_failure_not_model_limit() -> None:
    hooks = _StubbornHooks([[_finding("f-stuck")]] * 10)
    result = AetherNextKernel(max_steps=20, workbench_architect=_Workbench()).run(
        _env(), MemoryExecutor(workspace_root="/app"), hooks,
    )
    classification = HarnessLimiterClassifier().classify(result)
    assert classification.label == "verification_failure"
    assert classification.label != "model_limit"
    assert classification.evidence
