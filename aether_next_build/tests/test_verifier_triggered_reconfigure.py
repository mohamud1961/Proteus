"""Certified production does not permit model-authored reconfiguration.

The legacy reconfiguration implementation remains an audit/reference surface,
but a certified run must preserve its original contract rather than allowing a
Verifier response to rewrite it mid-episode.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

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


def _config_json(understanding: str) -> str:
    return json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": understanding,
        "success_definition": "out.txt exists.",
        "solver_system_prompt": {
            "role": "Workbench file solver",
            "workflow": ["write output", "submit"],
            "self_verification": ["check output"],
            "memory_use": ["none"],
            "stop_conditions": ["submit after writing"],
        },
        "verifier_system_prompt": {
            "role": "State verifier",
            "success_criteria": ["out.txt exists"],
            "required_evidence": ["current file state"],
            "false_positive_traps": ["presence is not correctness"],
            "verdict_guidance": ["judge current state"],
            "feedback_guidance": ["be concrete"],
        },
        "evidence_requirements": ["current out.txt state"],
        "false_positive_risks": ["file present but wrong content"],
        "minimum_completion_evidence": ["out.txt current state"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "run_command"]},
        "context_policy": {"mode": "retrieval_augmented"},
        "model_verifier_policy": {"enabled": True},
    })


class RecordingWorkbenchArchitect:
    def __init__(self) -> None:
        self.configure_calls: list[Mapping[str, Any]] = []

    def configure(self, request: Mapping[str, Any]):
        self.configure_calls.append(dict(request))
        tag = "reconfigured" if "reconfigure_context" in request else "initial"
        return parse_harness_config_ir(_config_json(f"{tag} config")), []


class BlockedThenCompletedHooks:
    """Solver writes then submits repeatedly; verifier: blocked -> completed."""

    def __init__(self, verdicts: list[dict[str, Any]]) -> None:
        self._verdicts = list(verdicts)
        self.verify_calls = 0
        self._acted = False

    def architect(self, request: Mapping[str, Any]):
        raise AssertionError("workbench mode must not call hooks.architect")

    def solve(self, messages: list[dict[str, str]], compiled) -> SolverTurn:
        if not self._acted:
            self._acted = True
            return SolverTurn(kind="act", summary="write out", actions=(ActionRequest(
                action_id="a-w", kind="write_file", capability_id="filesystem",
                arguments={"path": "out.txt", "content": "data"},
                intent="produce deliverable", expected_observation="file",
                if_fail_next="report blocker",
            ),))
        return SolverTurn(kind="submit_outcome", summary="submit")

    def verify(self, packet, compiled, ledger):
        self.verify_calls += 1
        if self._verdicts:
            return json.dumps(self._verdicts.pop(0))
        return json.dumps({
            "verdict": "completed", "confidence": "high",
            "summary": "state confirmed",
            "completion_evidence": [{
                "requirement": "out.txt exists with the produced data",
                "observed": "workspace state shows out.txt with the deliverable content",
                "inspection_refs": ["out.txt"],
                "falsification_check": "missing or divergent out.txt content would contradict completion",
            }],
        })


_BLOCKED = {
    "verdict": "blocked_by_harness_config",
    "confidence": "high",
    "summary": "solver lacks a required generic tool for this task",
    "findings": [{
        "finding_id": "cfg-1",
        "summary": "tool policy omits run_command needed to validate the artifact",
        "evidence": ["packet config_realization shows run_command missing"],
        "repair_instruction": "enable run_command",
        "applies_to": ["config"],
        "priority": "blocking",
    }],
}


def test_certified_run_suspends_blocked_verdict_reconfiguration() -> None:
    architect = RecordingWorkbenchArchitect()
    hooks = BlockedThenCompletedHooks([_BLOCKED])
    result = AetherNextKernel(
        max_steps=6, workbench_architect=architect, certified_production=True,
    ).run(
        _env(), MemoryExecutor(workspace_root="/app"), hooks,
    )

    assert result.status == "solver_submit_stalemate"
    assert result.reconfigurations == 0
    assert result.architect_defect is False
    assert len(architect.configure_calls) == 1
    assert any(r.kind == "verifier_reconfigure_suspended" for r in result.receipts)


def test_certified_run_never_consumes_reconfiguration_budget() -> None:
    architect = RecordingWorkbenchArchitect()
    hooks = BlockedThenCompletedHooks([_BLOCKED, dict(_BLOCKED)])
    result = AetherNextKernel(
        max_steps=8, workbench_architect=architect, certified_production=True,
    ).run(
        _env(), MemoryExecutor(workspace_root="/app"), hooks,
    )

    assert result.reconfigurations == 0
    assert len(architect.configure_calls) == 1
    assert any(r.kind == "verifier_reconfigure_suspended" for r in result.receipts)


def test_clean_run_has_no_architect_defect() -> None:
    architect = RecordingWorkbenchArchitect()
    hooks = BlockedThenCompletedHooks([])
    result = AetherNextKernel(max_steps=4, workbench_architect=architect).run(
        _env(), MemoryExecutor(workspace_root="/app"), hooks,
    )
    assert result.status == "completed"
    assert result.architect_defect is False
    assert result.architect_defect_reasons == ()
