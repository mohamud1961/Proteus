"""Verifier economics: identical state is never re-judged at full price.

Live evidence: openssl spent 16 verifier rounds and kv-store 117 check
executions on materially unchanged state.  Now: an unchanged packet reuses
the previous verdict without a model call (still counting toward stalemate),
and planned checks skip re-execution when nothing changed since they last ran.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    EnvMap,
    SolverTurn,
)
from aether_next.verifier_packets import packet_state_signature
from aether_next.workbench_config import parse_harness_config_ir
from aether_next.task_contract import TaskClause, TaskContract
from aether_next.world import WorldState


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
        "task_understanding": "write out.txt",
        "success_definition": "out.txt is correct.",
        "solver_system_prompt": {
            "role": "solver", "workflow": ["write", "submit"],
            "self_verification": ["check"], "memory_use": ["auto"],
            "stop_conditions": ["ready"],
        },
        "verifier_system_prompt": {
            "role": "verifier", "success_criteria": ["correct"],
            "required_evidence": ["state"], "false_positive_traps": ["shape"],
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


class _RepairThenIdleHooks:
    """Writes once, then submits repeatedly with no new evidence."""

    def __init__(self) -> None:
        self.verify_calls = 0
        self._acted = False

    def architect(self, request):
        raise AssertionError("workbench mode must not call hooks.architect")

    def solve(self, messages, compiled) -> SolverTurn:
        if not self._acted:
            self._acted = True
            return SolverTurn(kind="act", summary="write", actions=(ActionRequest(
                action_id="a-w", kind="write_file", capability_id="filesystem",
                arguments={"path": "out.txt", "content": "data"},
                intent="produce", expected_observation="file", if_fail_next="blocker",
            ),))
        return SolverTurn(kind="submit_outcome", summary="submit again")

    def verify(self, packet, compiled, ledger):
        self.verify_calls += 1
        return json.dumps({
            "verdict": "needs_repair",
            "confidence": "high",
            "summary": "content still wrong",
            "findings": [{
                "finding_id": "f-content",
                "summary": "out.txt content is wrong",
                "evidence": ["observed content mismatch"],
                "repair_instruction": "fix the content",
                "applies_to": ["out.txt"],
                "priority": "blocking",
            }],
        })


def test_unchanged_state_reuses_verdict_without_model_call() -> None:
    hooks = _RepairThenIdleHooks()
    world = WorldState(
        task_contract=TaskContract.create(
            "Write /app/out.txt",
            [TaskClause("artifact", "out.txt is present")],
        ),
    )
    result = AetherNextKernel(max_steps=12, workbench_architect=_Workbench()).run(
        _env(), MemoryExecutor(workspace_root="/app"), hooks, world_state=world,
    )
    # One real verifier round; every later submit on identical state reuses it
    # (or is gated by active-finding evidence pressure) until a bound fires.
    assert hooks.verify_calls == 1, f"expected 1 model verification, got {hooks.verify_calls}"
    assert result.status in {"solver_submit_stalemate", "verifier_stalemate"}
    assert result.step < 12, "bounded termination must beat max_steps"


class _CheckHooks:
    def __init__(self) -> None:
        self._step = 0

    def architect(self, request):
        raise AssertionError("workbench mode not used here")

    def solve(self, messages, compiled) -> SolverTurn:
        self._step += 1
        if self._step == 1:
            return SolverTurn(kind="act", summary="write", actions=(ActionRequest(
                action_id="a-w", kind="write_file", capability_id="filesystem",
                arguments={"path": "out.txt", "content": "v1"},
                intent="produce", expected_observation="file", if_fail_next="blocker",
            ),))
        return SolverTurn(kind="submit_outcome", summary="submit")


def test_planned_checks_skip_when_no_state_change(tmp_path) -> None:
    from aether_next.compiler import CapabilityRegistry, ConfigCompiler
    from aether_next.runtime_ir import RuntimeConfigIR

    executor = MemoryExecutor(workspace_root="/app")
    calls = {"n": 0}

    def counting_check(ex, cmd):
        calls["n"] += 1
        return CommandResult(command=cmd, exit_code=1, stderr="still failing")

    executor.register_command("test -e never.txt", counting_check)

    env = EnvMap(
        task_prompt="Write out.txt",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands"),
            "filesystem": CapabilityDescriptor("filesystem", "Files"),
        },
        grader_hints={"verify_commands": ["test -e never.txt"]},
    )
    _, eval_index = ConfigCompiler(CapabilityRegistry.from_envmap(env)).analyze_envmap(env)
    check_id = eval_index.checks[0].check_id
    ir = RuntimeConfigIR(
        architect_summary="s", solver_identity_prompt="solver",
        verifier_identity_prompt="verifier",
        selected_capabilities=("shell", "filesystem"),
        check_plan=(check_id,),
    )

    class Hooks(_CheckHooks):
        def architect(self, request):
            return ir

    result = AetherNextKernel(max_steps=5).run(env, executor, Hooks())
    executions = [r for r in result.receipts if r.kind == "check_result" and (r.payload or {}).get("check_id") == check_id and (r.payload or {}).get("origin") != "probe"]
    skips = [r for r in result.receipts if r.kind == "check_skipped_unchanged"]
    # Failing check + no state change between submits => executed once at the
    # first submit, skipped afterwards.
    assert calls["n"] <= 2, f"check executed {calls['n']} times"
    assert skips, "expected check_skipped_unchanged receipts on later submits"


def test_packet_signature_ignores_volatile_fields() -> None:
    base = {
        "step": 3,
        "reason": "solver_submit",
        "artifacts_present": ["out.txt"],
        "state_inspection_handles": [
            {"kind": "file", "handle": "3:a:file", "path": "out.txt", "bytes": 4, "content_hash": "abcd"},
        ],
        "open_obligations": [],
        "active_findings": [{"finding_id": "f-1", "age_steps": 1}],
        "solver_reported_blockers": [],
        "architect_verifier_prompt": {"hash": "h1"},
    }
    later = dict(base)
    later["step"] = 9
    later["state_inspection_handles"] = [
        {"kind": "file", "handle": "9:b:file", "path": "out.txt", "bytes": 4, "content_hash": "abcd"},
    ]
    later["active_findings"] = [{"finding_id": "f-1", "age_steps": 7}]
    assert packet_state_signature(base) == packet_state_signature(later)

    changed = dict(later)
    changed["state_inspection_handles"] = [
        {"kind": "file", "handle": "9:b:file", "path": "out.txt", "bytes": 9, "content_hash": "zzzz"},
    ]
    assert packet_state_signature(base) != packet_state_signature(changed)


def test_packet_signature_includes_same_id_finding_and_obligation_content() -> None:
    base = {
        "open_obligations": [{
            "obligation_id": "o-1",
            "kind": "artifact",
            "description": "out.txt exists",
            "target": "out.txt",
            "status": "open",
            "evidence_ids": [],
        }],
        "active_findings": [{
            "finding_id": "f-1",
            "verdict": "needs_repair",
            "priority": "blocking",
            "summary": "content is wrong",
            "evidence": ["file hash"],
            "repair_instruction": "rewrite the file",
            "age_steps": 1,
            "stale_cycles": 0,
        }],
    }
    changed_finding = {
        **base,
        "active_findings": [{
            **base["active_findings"][0],
            "summary": "content is missing",
            "evidence": ["new file hash"],
            "age_steps": 9,
            "stale_cycles": 2,
        }],
    }
    changed_obligation = {
        **base,
        "open_obligations": [{
            **base["open_obligations"][0],
            "description": "out.txt contains exact bytes",
        }],
    }
    assert packet_state_signature(base) != packet_state_signature(changed_finding)
    assert packet_state_signature(base) != packet_state_signature(changed_obligation)


def test_prose_missing_evidence_requests_are_realized_as_inspections() -> None:
    """Live defect: the verifier asked the SOLVER to 'provide the contents of
    /app/output.txt' -- unsatisfiable, since solver claims never enter the
    state-only packet.  Path-bearing requests now trigger the verifier's own
    read-only inspection within the same round."""
    from aether_next.model_hooks import _inspections_from_missing_evidence
    from aether_next.verifier import ModelVerifierResult

    result = ModelVerifierResult(
        verdict="uncertain_missing_evidence",
        summary="need raw state",
        missing_evidence_requests=(
            "Directly inspect and provide the contents of /app/output.txt.",
            "Please provide the raw transcription in /app/.aether_tools/answer_notes.txt",
            "Show the code image /app/code.png you read.",
            "General clarification with no file named.",
        ),
    )
    requests = _inspections_from_missing_evidence(result)
    by_path = {r.path: r.kind for r in requests}
    assert by_path.get("output.txt") == "read_file"
    assert by_path.get(".aether_tools/answer_notes.txt") == "read_file"
    assert by_path.get("code.png") == "perceive_artifact"
    assert len(requests) == 3  # the pathless request produces nothing


def test_prose_missing_evidence_transcript_request_realizes_read_output() -> None:
    from aether_next.model_hooks import _inspections_from_missing_evidence
    from aether_next.verifier import ModelVerifierResult

    result = ModelVerifierResult(
        verdict="uncertain_missing_evidence",
        summary="need transcript",
        missing_evidence_requests=(
            "Please surface the actual stdout/stderr or receipt text from the run.",
            "Please surface a compact transcript for frames around the claimed window.",
        ),
    )
    packet = {
        "state_inspection_handles": [
            {"kind": "output", "handle": "4:a-0:stdout", "stream": "stdout", "bytes": 128},
            {"kind": "output", "handle": "5:a-1:stdout", "stream": "stdout", "bytes": 860},
            {"kind": "output", "handle": "5:a-1:stderr", "stream": "stderr", "bytes": 0},
        ],
    }

    requests = _inspections_from_missing_evidence(result, packet=packet)

    read_output = [r for r in requests if r.kind == "read_output"]
    assert [r.handle for r in read_output] == ["5:a-1:stdout", "5:a-1:stderr"]
