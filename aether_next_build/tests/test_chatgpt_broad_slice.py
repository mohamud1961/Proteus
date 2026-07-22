from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from aether_next.analysis import _check_id
from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.context_compiler import ContextCompiler
from aether_next.execution import MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.kernel_config import resolve_runtime
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CheckSpec,
    CompletionPolicy,
    ContextPolicy,
    ContextRecipe,
    DeliverableSpec,
    EnvMap,
    EvalIndex,
    ModelVerifierPolicy,
    ObjectiveGraph,
    ProofObligation,
    RuntimeConfigIR,
    SolverTurn,
)
from aether_next.smoke_compile import compile_visible_smoke_tests
from aether_next.verifier import ModelVerifierResult, VerifierFinding
from aether_next.verifier_packets import build_verifier_packet
from aether_next.workbench_config import parse_harness_config_ir


_BUILD_ROOT = Path(__file__).resolve().parents[1]


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Create out.txt containing OK.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run shell", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
        },
    )


def _objective() -> ObjectiveGraph:
    return ObjectiveGraph(
        deliverables=(DeliverableSpec(path="out.txt"),),
        obligations=(ProofObligation("artifact:out.txt", "artifact", "out exists", "out.txt"),),
    )


def _compiled(*, context_policy: ContextPolicy | None = None):
    check_id = _check_id("test", "test -e out.txt")
    env = _env()
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(
        RuntimeConfigIR(
            architect_summary="summary",
            solver_identity_prompt="Use completion feedback as blocker and query_memory before repeats.",
            selected_capabilities=("shell", "filesystem"),
            completion_policy=CompletionPolicy(require_all_obligations=False, require_recent_progress=False),
            model_verifier_policy=ModelVerifierPolicy(enabled=True),
            check_plan=(check_id,),
            context_policy=context_policy or ContextPolicy(),
            success_definition="out.txt exists and contains OK.",
            local_verification_limits=("local checks cannot prove hidden grader behavior",),
        ),
        env,
        objective_graph=_objective(),
        eval_index=EvalIndex((CheckSpec(check_id, "exists", "test -e out.txt", "test"),)),
    )


def _workbench_json(smokes: list[dict]) -> str:
    return json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "Create out.txt.",
        "success_definition": "out.txt contains OK.",
        "solver_system_prompt": {
            "role": "verification-first solver",
            "workflow": ["inspect", "write", "self-check", "submit"],
            "self_verification": ["read out.txt and verify OK before submit"],
            "memory_use": ["query_memory before rereading or rerunning"],
        },
        "verifier_system_prompt": {
            "role": "Read-only verifier for out.txt",
            "success_criteria": ["out.txt contains OK"],
            "required_evidence": ["current file content or check evidence confirms OK"],
            "false_positive_traps": ["existence or stale evidence alone is insufficient"],
            "verdict_guidance": ["completed requires current evidence"],
            "feedback_guidance": ["identify the missing or wrong content"],
        },
        "evidence_requirements": ["out.txt contains OK", "current file content or check evidence confirms OK"],
        "false_positive_risks": ["existence or stale evidence alone is insufficient"],
        "minimum_completion_evidence": ["current out.txt content or check evidence"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "query_memory"]},
        "context_policy": {"mode": "retrieval_augmented"},
        "verification_policy": {"visible_smoke_tests": smokes},
        "model_verifier_policy": {"enabled": True},
        "local_verification_limits": ["visible smoke tests are internal evidence only"],
    })


def test_kernel_owned_memory_tools_record_observations_history_and_diff() -> None:
    env = _env()
    executor = MemoryExecutor(files={"out.txt": "old"})

    class Hooks:
        def architect(self, request):
            return RuntimeConfigIR(
                architect_summary="summary",
                solver_identity_prompt="solver",
                selected_capabilities=("filesystem",),
                completion_policy=CompletionPolicy(require_authoritative_check=False, require_all_obligations=False),
            )

        def solve(self, messages, compiled):
            turns = (
                ActionRequest("obs", "record_observation", "kernel", {"observation": "out.txt must contain OK", "path": "out.txt"}, "record", "saved", "continue"),
                ActionRequest("hist", "query_artifact_history", "kernel", {"path": "out.txt"}, "history", "events", "continue"),
                ActionRequest("diff", "inspect_diff", "kernel", {"path": "out.txt"}, "diff", "summary", "continue"),
            )
            index = getattr(self, "turn_index", 0)
            self.turn_index = index + 1
            return SolverTurn(kind="act", summary="use one memory tool", actions=(turns[index],))

    result = AetherNextKernel(max_steps=3).run(env, executor, Hooks())
    kinds = [receipt.kind for receipt in result.receipts]
    assert "record_observation" in kinds
    assert "query_artifact_history" in kinds
    assert "inspect_diff" in kinds
    observation = [r for r in result.receipts if r.kind == "record_observation"][0]
    assert observation.payload["observation"] == "out.txt must contain OK"


def test_write_file_receipt_records_before_after_hash_for_artifact_history() -> None:
    env = _env(); executor = MemoryExecutor(files={"out.txt": "old"})

    class Hooks:
        def architect(self, request):
            return RuntimeConfigIR(
                architect_summary="summary",
                solver_identity_prompt="solver",
                selected_capabilities=("filesystem",),
                completion_policy=CompletionPolicy(require_authoritative_check=False, require_all_obligations=False),
            )
        def solve(self, messages, compiled):
            return SolverTurn(kind="act", summary="write", actions=(
                ActionRequest("w", "write_file", "filesystem", {"path": "out.txt", "content": "OK"}, "write", "file", "continue"),
            ))

    result = AetherNextKernel(max_steps=1).run(env, executor, Hooks())
    write = [r for r in result.receipts if r.kind == "write_file"][0]
    assert write.payload["before_content_hash"] != write.payload["after_content_hash"]
    assert write.payload["excerpt"] == "OK"


def test_context_recipe_cannot_drop_active_findings_or_pending_checks() -> None:
    compiled = _compiled(context_policy=ContextPolicy(recipe=ContextRecipe(always_include=("observations",))))
    ledger = ExecutionLedger(); ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt("check-fail", 1, "check_result", False, "failed", failure_class="check_failed", payload={"check_id": compiled.check_plan_ids[0], "command": "test -e out.txt", "passed": False, "origin": "test"}))
    finding = VerifierFinding("vf", 2, "needs_repair", "blocking", "fix out", applies_to=("out.txt",))
    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=2)

    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    assert "pending_checks" in packet
    assert "active_completion_findings" in packet
    assert packet["context_recipe_realization"]["enabled"] is True


def test_visible_smoke_specs_compile_into_internal_checks_and_resolve_path() -> None:
    env = _env()
    config = parse_harness_config_ir(_workbench_json([
        {"type": "content_assertion", "path": "out.txt", "contains": "OK"},
        {"type": "syntax_check", "path": "script.py", "language": "python"},
    ]))
    smoke = compile_visible_smoke_tests(config, env)
    assert len(smoke.checks) == 2
    assert all(check.origin == "visible_smoke" for check in smoke.checks)

    class Workbench:
        def configure(self, request):
            return config, ()

    class Hooks:
        def architect(self, request):
            raise AssertionError("baseline architect should not run")

    resolved = resolve_runtime(env, ConfigCompiler(CapabilityRegistry.from_envmap(env)), Hooks(), workbench_architect=Workbench())
    planned = resolved.compiled.planned_checks()  # type: ignore[union-attr]
    assert [check.origin for check in planned] == ["visible_smoke", "visible_smoke"]
    assert resolved.compiled.config_realization["checks_compiled"] == [check.check_id for check in planned]  # type: ignore[union-attr]


def test_under_specified_visible_smoke_is_rejected_not_compiled() -> None:
    config = parse_harness_config_ir(_workbench_json([
        {"type": "content_assertion", "path": "out.txt"},
    ]))
    result = compile_visible_smoke_tests(config, _env())
    assert result.checks == ()
    assert result.rejected[0]["reason_code"] == "visible_smoke_missing_assertion"


def test_verifier_packet_keeps_active_findings_but_excludes_solver_journey_changes() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger(); ledger.ensure_objective(compiled.objective_graph)
    finding = VerifierFinding("vf-nochange", 1, "needs_repair", "blocking", "fix out", applies_to=("out.txt",))
    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=1)
    ledger.record(Receipt("read-same", 2, "read_file", True, "read only", payload={"path": "out.txt", "content_hash": "same"}))
    packet = build_verifier_packet(
        compiled, ledger, step=3, reason="solver_submit_success_candidate", envmap={"workspace": "/app"}
    )
    assert packet["active_findings"][0]["finding_id"] == "vf-nochange"
    assert "changes_since_active_findings" not in packet
    assert "recent_actions" not in packet
    assert "recent_receipts" not in packet

    ledger.record(Receipt("write-fix", 4, "write_file", True, "rewrote out", state_change=True, payload={"path": "out.txt", "modified_paths": ("out.txt",)}))
    packet = build_verifier_packet(
        compiled, ledger, step=5, reason="solver_submit_success_candidate", envmap={"workspace": "/app"}
    )
    assert packet["active_findings"][0]["finding_id"] == "vf-nochange"
    assert "changes_since_active_findings" not in packet


def test_uncertain_missing_evidence_does_not_resolve_existing_active_finding() -> None:
    ledger = ExecutionLedger()
    finding = VerifierFinding("vf-open", 1, "needs_repair", "blocking", "fix out", applies_to=("out.txt",))
    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=1)
    ledger.apply_verifier_result(ModelVerifierResult("uncertain_missing_evidence", missing_evidence_requests=("read out.txt",)), step=2)
    assert [item["finding_id"] for item in ledger.active_finding_context(3)] == ["vf-open"]


def test_verifier_only_fake_runner_writes_evidence_bundle(tmp_path: Path) -> None:
    out = tmp_path / "verifier_eval"
    completed = subprocess.run(
        [sys.executable, str(_BUILD_ROOT / "run_verifier_only_eval.py"), "--mode", "fake", "--out-dir", str(out)],
        check=True,
        text=True,
        capture_output=True,
        cwd=_BUILD_ROOT,
    )
    summary = json.loads(completed.stdout)
    assert len(summary["rows"]) == 6
    assert all(row["parse_ok"] for row in summary["rows"])
    assert {row["case"] for row in summary["rows"]} == {
        "semantic_wrong",
        "solver_claim_conflicts_with_raw_state",
        "missing_artifact",
        "schema_mismatch",
        "repeated_no_progress",
        "insufficient_evidence",
    }
    assert (out / "VERIFIER_ONLY_EXPERIMENT_REPORT.md").exists()
    assert (out / "semantic_wrong" / "verifier_packet.json").exists()
