from __future__ import annotations

import json
import random
import time
from dataclasses import replace
from typing import Any, Mapping

from aether_next.analysis import _check_id
from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.context_compiler import ContextCompiler
from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.kernel_verifier import run_model_verifier_if_available
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CheckSpec,
    CompletionPolicy,
    ContextPolicy,
    ContextRecipe,
    ContextRecipeRecent,
    DeliverableSpec,
    EnvMap,
    EvalIndex,
    ObjectiveGraph,
    ProofObligation,
    ModelVerifierPolicy,
    RuntimeConfigIR,
    SolverTurn,
)
from aether_next.smoke_compile import compile_visible_smoke_tests
from aether_next.verifier import CompletionEvidenceEntry, ModelVerifierResult, VerifierFinding, parse_model_verifier_result
from aether_next.verifier_packets import build_verifier_packet
from aether_next.workbench_compile import config_realization_audit, harness_config_to_runtime_ir
from aether_next.workbench_config import UNSUPPORTED_VISIBLE_SMOKE_TEST_TYPE_CODE, parse_workbench_architect_output


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Write `out.txt`. You can run `test -e out.txt` to verify.",
        workspace_root="/app",
        grader_hints={"verify_commands": ("test -e out.txt",)},
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


def _check_id_out() -> str:
    return _check_id("grader_hint", "test -e out.txt")


def _eval_index() -> EvalIndex:
    return EvalIndex(checks=(CheckSpec(_check_id_out(), "exists:out.txt", "test -e out.txt", "test"),))


def _ir(*, context_policy: ContextPolicy | None = None) -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="summary",
        solver_identity_prompt="task-specific solver prompt with query_memory guidance",
        selected_capabilities=("shell", "filesystem"),
        completion_policy=CompletionPolicy(
            require_authoritative_check=True,
            require_all_obligations=False,
            require_recent_progress=False,
        ),
        context_policy=context_policy or ContextPolicy(),
        check_plan=(_check_id_out(),),
        inspection_plan=("inspect",),
        proof_plan=("prove",),
        success_definition="out.txt exists and visible checks pass.",
        local_verification_limits=("local checks cannot prove hidden grader expectations",),
    )


def _compiled(*, context_policy: ContextPolicy | None = None):
    return ConfigCompiler(CapabilityRegistry.from_envmap(_env())).compile(
        _ir(context_policy=context_policy), _env(), objective_graph=_objective(), eval_index=_eval_index()
    )


def _recipe_policy(
    *,
    mode: str = "default_bounded",
    always_include: tuple[str, ...] = (),
    include_recent: tuple[tuple[str, int], ...] = (),
    include_last_failure: int = 0,
    preserve_exact: tuple[str, ...] = (),
    make_queryable_not_inline: tuple[str, ...] = (),
    unsupported_fields: tuple[str, ...] = (),
) -> ContextPolicy:
    return ContextPolicy(
        mode=mode,
        recipe=ContextRecipe(
            always_include=always_include,
            include_recent=tuple(
                ContextRecipeRecent(selector=selector, count=count)
                for selector, count in include_recent
            ),
            include_last_failure=include_last_failure,
            preserve_exact=preserve_exact,
            make_queryable_not_inline=make_queryable_not_inline,
            unsupported_fields=unsupported_fields,
        ),
    )


def _action(kind: str, args: Mapping[str, Any], *, cap: str = "shell", action_id: str = "a") -> ActionRequest:
    return ActionRequest(
        action_id=action_id,
        kind=kind,
        capability_id=cap,
        arguments=args,
        intent="test",
        expected_observation="test",
        if_fail_next="fix",
    )


class Hooks:
    def __init__(self, turns: list[SolverTurn], verifier_result: ModelVerifierResult | None = None) -> None:
        self.turns = list(turns)
        self.verifier_result = verifier_result
        self.verify_packets: list[dict[str, Any]] = []

    def architect(self, request):
        return _ir()

    def solve(self, messages, compiled):
        if self.turns:
            return self.turns.pop(0)
        return SolverTurn(kind="submit_outcome", summary="submit")

    def reconfigure(self, request, compiled, ledger):
        return _ir()

    def verify(self, packet, compiled, ledger):
        self.verify_packets.append(packet)
        assert self.verifier_result is not None
        return self.verifier_result


class DisabledVerifierHooks(Hooks):
    def architect(self, request):
        return replace(_ir(), model_verifier_policy=ModelVerifierPolicy(enabled=False))

    def verify(self, packet, compiled, ledger):
        raise AssertionError("verifier hook should not be called when policy is disabled")


class InvalidReconfigureHooks(Hooks):
    def reconfigure(self, request, compiled, ledger):
        return replace(_ir(), selected_capabilities=("does_not_exist",))


def _repaired_workbench_config_json() -> str:
    return json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "Write out.txt.",
        "success_definition": "out.txt exists and contains the requested content.",
        "solver_system_prompt": {
            "role": "Workbench verifier packet test solver",
            "workflow": ["inspect", "write", "verify", "submit"],
            "self_verification": ["check the visible output before submitting"],
            "memory_use": ["query_memory before repeating work"],
        },
        "verifier_system_prompt": {
            "role": "Read-only verifier for out.txt",
            "success_criteria": ["out.txt exists and contains the requested content"],
            "required_evidence": ["current artifact or check evidence supports completion"],
            "false_positive_traps": ["unsupported local checks cannot prove hidden grader behavior"],
            "verdict_guidance": ["completed requires current evidence"],
            "feedback_guidance": ["name missing evidence or repair target"],
        },
        "evidence_requirements": ["out.txt exists", "out.txt content supports the request"],
        "false_positive_risks": ["local checks may not prove hidden grader behavior"],
        "minimum_completion_evidence": ["current artifact or check evidence"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "query_memory"]},
        "context_policy": {"mode": "retrieval_augmented"},
        "verification_policy": {"visible_smoke_tests": [{"type": "grader_clone"}]},
        "model_verifier_policy": {"enabled": True},
        "local_verification_limits": ["local checks cannot prove hidden grader behavior"],
    })


def test_query_memory_supports_filters_and_repeat_guard() -> None:
    ledger = ExecutionLedger()
    ledger.record(Receipt("r1", 1, "read_file", True, "read TTL", payload={"path": "university_graph.ttl", "content_hash": "abc"}))
    ledger.record(Receipt("r2", 2, "read_file", True, "read TTL again", payload={"path": "university_graph.ttl", "content_hash": "abc"}))
    ledger.record(Receipt("c1", 3, "check_result", False, "schema failed", failure_class="schema_mismatch", payload={"check_id": "schema-out", "detail": "missing G"}))

    hits = ledger.query_memory("missing", filters={"check_id": "schema-out"})
    guard = ledger.repeat_guard(kind="read_file", target="university_graph.ttl")

    assert hits[0]["receipt_id"] == "c1"
    assert hits[0]["check_id"] == "schema-out"
    assert guard["repeat_count"] == 2
    assert guard["same_content_hash"] is True
    assert guard["likely_wasteful"] is True


def test_context_policy_latest_tool_result_only_excludes_old_receipts() -> None:
    compiled = _compiled(context_policy=ContextPolicy(mode="latest_tool_result_only"))
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    ledger.record(Receipt("old", 1, "read_file", True, "read input", payload={"path": "input.txt"}))
    ledger.record(Receipt("latest", 2, "run_command", True, "command exit=0: echo ok", payload={"command": "echo ok"}))

    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    assert packet["latest_tool_result"]["receipt_id"] == "latest"
    assert "recent_progress" not in packet
    assert packet["automatic_memory_available"] is True


def test_solver_always_sees_its_own_command_stdout_regardless_of_mode() -> None:
    """Regression for the VM batch false-clean/step-blowup investigation: the
    solver was told to "produce X evidence" by an active finding while the
    stdout of the command it had just run (which WAS X) was invisible to it on
    the next step, because command_results was gated behind an explicit
    architect context_policy.include_sections choice most architects never
    made. This must hold for every context mode -- the harness must not
    discard the solver's own working memory and then blame it for repeating.
    """
    stdout = "KEY_MODE 600 server.key\nCERT_SUBJECT=CN=dev-internal.company.local"
    for mode in ("default_bounded", "retrieval_augmented", "rolling_recent", "failure_focused", "latest_tool_result_only"):
        compiled = _compiled(context_policy=ContextPolicy(mode=mode))
        ledger = ExecutionLedger()
        ledger.ensure_objective(_objective())
        ledger.record(Receipt(
            "cmd-1", 1, "run_command", True, "command exit=0: stat/openssl",
            payload={"command": "stat -c '%a %n' server.key; openssl x509 -noout -subject", "stdout": stdout},
        ))

        packet = ContextCompiler().compile(compiled, ledger, alerts=[])

        assert "command_results" in packet, f"mode={mode} dropped command_results"
        rows = packet["command_results"]
        assert any(stdout in str(row.get("stdout", "")) for row in rows), (
            f"mode={mode}: solver's own command stdout not visible in its next context"
        )


def _context_ledger_with_state() -> ExecutionLedger:
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    ledger.seed_capabilities(("shell", "filesystem"))
    ledger.record(Receipt("read-1", 1, "read_file", True, "read input", state_change=True, payload={"path": "input.txt"}))
    ledger.record(Receipt("write-1", 2, "write_file", True, "wrote out", state_change=True, payload={"path": "out.txt"}))
    ledger.record(Receipt(
        "check-1", 3, "check_result", False, "check failed",
        failure_class="check_failed",
        payload={
            "check_id": _check_id_out(),
            "command": "test -e out.txt",
            "passed": False,
            "origin": "test",
            "detail": "missing out",
            "blocker_code": "check_failed",
        },
    ))
    finding = VerifierFinding(
        finding_id="vf-context", created_step=4, verdict="needs_repair", priority="blocking",
        summary="out.txt content is not verified", applies_to=("out.txt",),
        repair_instruction="verify out.txt content",
    )
    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=4)
    return ledger


def test_context_policy_default_bounded_exact_sections() -> None:
    compiled = _compiled(context_policy=ContextPolicy(mode="default_bounded"))
    packet = ContextCompiler().compile(compiled, _context_ledger_with_state(), alerts=[])

    expected = {
        "open_obligations",
        "obligation_status",
        "monitor_alerts",
        "live_processes",
        "recent_progress",
        "failure_clusters",
        "artifacts_present",
        "candidate_leaderboard",
        "installed_capabilities",
        "planned_checks",
        "pending_checks",
        "command_results",
        "tool_results",
        "active_completion_findings",
        "files_already_read",
        "latest_file_reads",
        "stuck",
        "automatic_memory_available",
    }
    assert set(packet) == expected


def test_context_policy_retrieval_augmented_adds_memory_guidance_without_dropping_default_sections() -> None:
    compiled = _compiled(context_policy=ContextPolicy(mode="retrieval_augmented"))
    packet = ContextCompiler().compile(compiled, _context_ledger_with_state(), alerts=[])

    assert packet["automatic_memory_available"] is True
    assert "automatic_memory_guidance" in packet
    assert "recent_progress" in packet
    assert "pending_checks" in packet
    assert "active_completion_findings" in packet


def test_context_policy_rolling_recent_exact_sections() -> None:
    compiled = _compiled(context_policy=ContextPolicy(mode="rolling_recent"))
    packet = ContextCompiler().compile(compiled, _context_ledger_with_state(), alerts=[])

    assert set(packet) == {
        "recent_progress",
        "pending_checks",
        "artifacts_present",
        "active_completion_findings",
        "automatic_memory_available",
        "command_results",
        "tool_results",
    }


def test_context_policy_failure_focused_exact_sections() -> None:
    compiled = _compiled(context_policy=ContextPolicy(mode="failure_focused"))
    ledger = _context_ledger_with_state()
    ledger.record(Receipt("read-2", 5, "read_file", True, "read input again", payload={"path": "input.txt"}))
    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    assert set(packet) == {
        "active_completion_findings",
        "pending_checks",
        "failure_clusters",
        "files_already_read",
        "repeated_actions",
        "repeat_efficiency_guidance",
        "stuck",
        "automatic_memory_available",
        "command_results",
        "tool_results",
    }
    assert "recent_progress" not in packet
    assert "artifacts_present" not in packet


def test_context_policy_latest_tool_result_only_exact_sections() -> None:
    compiled = _compiled(context_policy=ContextPolicy(mode="latest_tool_result_only"))
    packet = ContextCompiler().compile(compiled, _context_ledger_with_state(), alerts=[])

    assert set(packet) == {
        "automatic_memory_available",
        "latest_tool_result",
        "pending_checks",
        "active_completion_findings",
        "stuck",
        "command_results",
        "tool_results",
    }
    assert packet["latest_tool_result"]["kind"] == "check_result"


def test_context_compression_triggers_at_sixty_percent_and_preserves_findings() -> None:
    policy = ContextPolicy(model_context_window_tokens=80, compression_trigger_ratio=0.60)
    compiled = _compiled(context_policy=policy)
    ledger = ExecutionLedger()
    finding = VerifierFinding(
        finding_id="vf-1", created_step=1, verdict="needs_repair", priority="blocking",
        summary="out.txt is wrong", applies_to=("out.txt",), repair_instruction="rewrite out.txt",
    )
    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=1)
    for i in range(12):
        ledger.record(Receipt(f"p{i}", i + 2, "write_file", True, "wrote progress " + "x" * 80, state_change=True, payload={"path": f"f{i}.txt"}))

    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    assert packet["context_compression"]["triggered"] is True
    assert packet["active_completion_findings"][0]["finding_id"] == "vf-1"
    assert packet["context_compression"]["threshold_ratio"] == 0.60
    assert "pending_checks" in packet["context_compression"]["preserved_exact"]


def test_context_compression_does_not_trigger_below_sixty_percent() -> None:
    policy = ContextPolicy(model_context_window_tokens=4000, compression_trigger_ratio=0.60)
    compiled = _compiled(context_policy=policy)
    ledger = _context_ledger_with_state()

    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    assert "context_compression" not in packet


def test_context_recipe_last_two_tool_results_and_single_last_failure() -> None:
    compiled = _compiled(context_policy=_recipe_policy(
        include_recent=(("tool_results", 2),),
        include_last_failure=1,
    ))
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    ledger.record(Receipt("read-1", 1, "read_file", True, "read alpha", payload={"path": "alpha.txt", "excerpt": "alpha"}))
    ledger.record(Receipt("cmd-1", 2, "run_command", True, "command exit=0: echo ok", payload={"command": "echo ok", "stdout": "ok"}))
    ledger.record(Receipt("check-1", 3, "check_result", False, "check failed", failure_class="check_failed", payload={"check_id": _check_id_out(), "detail": "missing out"}))
    ledger.record(Receipt("write-1", 4, "write_file", True, "wrote out", state_change=True, payload={"path": "out.txt"}))

    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    assert packet["automatic_memory_available"] is True
    assert [item["receipt_id"] for item in packet["tool_results"]] == ["cmd-1", "write-1"]
    assert [item["receipt_id"] for item in packet["last_failures"]] == ["check-1"]
    realization = packet["context_recipe_realization"]
    assert realization["selected"][0]["selector"] == "tool_results"
    assert realization["selected"][1]["selector"] == "last_failures"


def test_context_recipe_preserves_active_findings_pending_checks_and_stuck_exactly() -> None:
    compiled = _compiled(context_policy=_recipe_policy(
        preserve_exact=("active_completion_findings", "pending_checks", "stuck"),
        make_queryable_not_inline=("recent_progress",),
    ))
    ledger = _context_ledger_with_state()
    ledger.record(Receipt("noop-1", 5, "run_command", False, "command exit=1: false", failure_class="command_failure", payload={"command": "false"}))
    ledger.record(Receipt("noop-2", 6, "run_command", False, "command exit=1: false", failure_class="command_failure", payload={"command": "false"}))
    ledger.record(Receipt("noop-3", 7, "run_command", False, "command exit=1: false", failure_class="command_failure", payload={"command": "false"}))

    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    assert "active_completion_findings" in packet
    assert "pending_checks" in packet
    assert packet["stuck"]["no_progress"] is True
    assert "recent_progress" not in packet
    assert packet["context_recipe_realization"]["queryable_not_inline"][0]["selector"] == "recent_progress"


def test_context_recipe_large_outputs_are_queryable_not_inline() -> None:
    compiled = _compiled(context_policy=_recipe_policy(
        include_recent=(("command_results", 1), ("file_reads", 1)),
        make_queryable_not_inline=("command_results", "file_reads"),
    ))
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    long_stdout = "x" * 1200
    long_excerpt = "y" * 900
    ledger.record(Receipt("cmd-1", 1, "run_command", True, "command exit=0: dump", payload={"command": "dump", "stdout": long_stdout}))
    ledger.record(Receipt("read-1", 2, "read_file", True, "read huge.txt", payload={"path": "huge.txt", "excerpt": long_excerpt, "bytes": len(long_excerpt)}))

    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    assert "command_results" not in packet
    assert "file_reads" not in packet
    queryable = {item["selector"]: item for item in packet["context_recipe_realization"]["queryable_not_inline"]}
    assert queryable["command_results"]["matching_count"] == 1
    assert queryable["file_reads"]["matching_count"] == 1
    assert long_stdout not in json.dumps(packet, sort_keys=True)
    assert long_excerpt not in json.dumps(packet, sort_keys=True)


def test_context_recipe_rejects_unsupported_selectors_without_crashing() -> None:
    compiled = _compiled(context_policy=_recipe_policy(
        preserve_exact=("active_completion_findings", "banana_selector"),
        include_recent=(("tool_results", 1), ("imaginary_results", 2)),
        make_queryable_not_inline=("unsupported_queryable",),
        unsupported_fields=("memory_query_available",),
    ))
    packet = ContextCompiler().compile(compiled, _context_ledger_with_state(), alerts=[])

    rejected = packet["context_recipe_realization"]["rejected"]
    rejected_selectors = {item.get("selector") or item.get("field") for item in rejected}

    assert "banana_selector" in rejected_selectors
    assert "imaginary_results" in rejected_selectors
    assert "unsupported_queryable" in rejected_selectors
    assert "memory_query_available" in rejected_selectors
    assert "banana_selector" not in packet
    assert "imaginary_results" not in packet


def test_context_recipe_fuzz_counts_are_deterministic_and_unsupported_selectors_are_quarantined() -> None:
    rng = random.Random(7)
    kind_pool = (
        "read_file",
        "write_file",
        "run_command",
        "check_result",
        "query_memory",
        "model_verifier_result",
    )
    supported = {
        "tool_results",
        "file_reads",
        "file_writes",
        "command_results",
        "check_results",
        "query_memory_results",
        "verifier_results",
        "recent_progress",
    }
    ledgers: list[ExecutionLedger] = []
    for _ in range(12):
        ledger = ExecutionLedger()
        ledger.ensure_objective(_objective())
        for step in range(1, 30):
            kind = rng.choice(kind_pool)
            success = rng.choice((True, False))
            payload: dict[str, Any] = {}
            state_change = kind in {"write_file"} and success
            if kind == "read_file":
                payload = {"path": f"f{step}.txt", "excerpt": f"e{step}"}
            elif kind == "write_file":
                payload = {"path": f"w{step}.txt"}
            elif kind == "run_command":
                payload = {"command": f"cmd-{step}", "stdout": f"stdout-{step}"}
            elif kind == "check_result":
                payload = {"check_id": f"check-{step}", "detail": f"detail-{step}"}
            elif kind == "query_memory":
                payload = {"query": f"q{step}", "results": [{"receipt_id": f"r{step}"}]}
            elif kind == "model_verifier_result":
                payload = {"verdict": "completed" if success else "needs_repair"}
            ledger.record(Receipt(
                f"r-{step}",
                step,
                kind,
                success,
                f"{kind}-{step}",
                state_change=state_change,
                failure_class="" if success else f"{kind}_failed",
                payload=payload,
            ))
        ledgers.append(ledger)

    for ledger in ledgers:
        requested_selector = rng.choice(sorted(supported))
        requested_count = rng.randint(1, 4)
        compiled = _compiled(context_policy=_recipe_policy(
            include_recent=((requested_selector, requested_count), ("not_real", 2)),
            preserve_exact=("pending_checks",),
        ))
        packet = ContextCompiler().compile(compiled, ledger, alerts=[])
        packet_again = ContextCompiler().compile(compiled, ledger, alerts=[])

        assert packet == packet_again
        rejected = packet["context_recipe_realization"]["rejected"]
        assert any(item.get("selector") == "not_real" for item in rejected)
        if requested_selector in packet:
            assert len(packet[requested_selector]) <= requested_count
            expected_ids = [
                receipt.receipt_id
                for receipt in ContextCompiler()._recent_receipts(requested_selector, ledger, requested_count)
            ]
            assert [row["receipt_id"] for row in packet[requested_selector]] == expected_ids


def test_solver_can_call_configured_run_check_tool() -> None:
    executor = MemoryExecutor(workspace_root="/app", files={"out.txt": "ok"})
    executor.register_command("test -e out.txt", lambda ex, cmd: CommandResult(cmd, 0, stdout="ok"))
    turn = SolverTurn(kind="act", summary="check", actions=(_action("run_check", {"check_id": _check_id_out()}, action_id="check-1"),))
    hooks = Hooks([turn], ModelVerifierResult("completed"))

    result = AetherNextKernel(max_steps=1).run(_env(), executor, hooks)

    receipts = [r for r in result.receipts if r.kind == "check_result"]
    assert receipts
    assert receipts[0].payload["origin"] == "solver_callable"
    assert executor.command_history[0] == "test -e out.txt"


def test_model_verifier_blocks_internal_completion_and_persists_finding() -> None:
    executor = MemoryExecutor(workspace_root="/app", files={"out.txt": "bad"})
    executor.register_command("test -e out.txt", lambda ex, cmd: CommandResult(cmd, 0, stdout="ok"))
    finding = VerifierFinding(
        finding_id="vf-bad-content", created_step=0, verdict="needs_repair", priority="blocking",
        summary="out.txt exists but content is semantically wrong", applies_to=("out.txt",),
        evidence=("out.txt contains bad",), repair_instruction="rewrite out.txt with the requested content",
    )
    hooks = Hooks([SolverTurn(kind="submit_outcome", summary="submit")], ModelVerifierResult("needs_repair", findings=(finding,)))

    result = AetherNextKernel(max_steps=1).run(_env(), executor, hooks)

    assert result.status == "incomplete"
    assert hooks.verify_packets[0]["reason"] == "solver_submit"
    verifier_receipts = [r for r in result.receipts if r.kind == "model_verifier_result"]
    assert verifier_receipts and verifier_receipts[0].failure_class == "needs_repair"
    assert any(r.kind == "model_verifier_result" for r in result.receipts)


def test_model_verifier_completed_allows_internal_completion() -> None:
    executor = MemoryExecutor(workspace_root="/app", files={"out.txt": "ok"})
    executor.register_command("test -e out.txt", lambda ex, cmd: CommandResult(cmd, 0, stdout="ok"))
    hooks = Hooks([SolverTurn(kind="submit_outcome", summary="submit")], ModelVerifierResult("completed", summary="done"))

    result = AetherNextKernel(max_steps=1).run(_env(), executor, hooks)

    assert result.status == "completed"
    assert [r for r in result.receipts if r.kind == "model_verifier_result"]


def test_incomplete_repeated_memory_queries_do_not_call_verifier_without_submit() -> None:
    executor = MemoryExecutor(workspace_root="/app", files={"input.txt": "alpha beta gamma"})
    finding = VerifierFinding(
        finding_id="vf-memory-loop",
        created_step=3,
        verdict="needs_repair",
        priority="blocking",
        summary="solver is repeating memory queries instead of acting on file evidence",
        applies_to=("input.txt",),
        evidence=("latest_file_reads contains input.txt",),
        repair_instruction="Use the visible file evidence to write the required artifact.",
    )
    turns = [
        SolverTurn(kind="act", summary="read", actions=(
            _action("read_file", {"path": "input.txt"}, cap="filesystem", action_id="a1"),
        )),
        SolverTurn(kind="act", summary="query", actions=(
            _action("query_memory", {"query": "nothing here"}, cap="memory", action_id="a1"),
        )),
        SolverTurn(kind="act", summary="query again", actions=(
            _action("query_memory", {"query": "nothing here"}, cap="memory", action_id="a1"),
        )),
    ]
    hooks = Hooks(turns, ModelVerifierResult("needs_repair", findings=(finding,)))

    result = AetherNextKernel(max_steps=3).run(_env(), executor, hooks)

    assert result.status == "incomplete"
    assert hooks.verify_packets == []
    assert not any(r.kind == "model_verifier_result" for r in result.receipts)


def test_incomplete_max_steps_skips_verifier_when_policy_disabled() -> None:
    executor = MemoryExecutor(workspace_root="/app", files={})
    hooks = DisabledVerifierHooks([
        SolverTurn(kind="act", summary="noop", actions=(
            _action("record_observation", {"observation": "still investigating"}, cap="memory", action_id="a1"),
        )),
    ])

    result = AetherNextKernel(max_steps=1).run(_env(), executor, hooks)

    assert result.status == "incomplete"
    assert hooks.verify_packets == []
    assert not any(r.kind == "model_verifier_skipped" for r in result.receipts)
    assert not any(r.kind == "model_verifier_result" for r in result.receipts)


def test_invalid_reconfigure_no_longer_calls_verifier_without_submit() -> None:
    executor = MemoryExecutor(workspace_root="/app", files={})
    finding = VerifierFinding(
        finding_id="vf-config-block",
        created_step=0,
        verdict="blocked_by_harness_config",
        priority="blocking",
        summary="requested runtime config could not be realized",
        evidence=("reconfiguration invalid",),
        repair_instruction="Choose only available capabilities before retrying.",
    )
    hooks = InvalidReconfigureHooks(
        [SolverTurn(kind="request_reconfigure", summary="need missing tool")],
        ModelVerifierResult("blocked_by_harness_config", findings=(finding,)),
    )

    result = AetherNextKernel(max_steps=1).run(_env(), executor, hooks)

    assert result.status == "incomplete"
    assert hooks.verify_packets == []
    assert any(
        r.kind == "turn_validation" and not r.success and "unknown turn kind" in r.summary
        for r in result.receipts
    )
    assert not any(r.kind == "reconfigure_validation" for r in result.receipts)
    assert not any(r.kind == "model_verifier_result" for r in result.receipts)


def test_verifier_packet_contains_state_contract_for_fake_verifier() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    ledger.record(Receipt(
        "write-1", 1, "write_file", True, "wrote out", state_change=True,
        payload={"path": "out.txt", "artifact_paths": ("out.txt",), "file_handle": "file:out.txt", "bytes": 3},
    ))
    ledger.record(Receipt(
        "check-1", 2, "check_result", False, "check failed",
        failure_class="check_failed",
        payload={"check_id": _check_id_out(), "command": "test -e out.txt", "passed": False, "detail": "missing"},
    ))

    packet = build_verifier_packet(compiled, ledger, step=2, reason="deterministic_failure", envmap=_env())

    assert packet["reason"] == "deterministic_failure"
    assert packet["task_contract"]["raw_task_prompt"] == compiled.task_prompt
    assert packet["evidence_requirements"]["local_limits"] == [
        {
            "source": "runtime_config",
            "statement": "local checks cannot prove hidden grader expectations",
        }
    ]
    assert "solver_system_prompt" not in packet
    assert "official_grader_authority" not in packet
    assert "deterministic_checks" not in packet
    assert "recent_receipts" not in packet
    assert "solver_authored_evidence" not in packet
    assert packet["dynamic_state"]["artifacts"]["out.txt"]["status"] == "present"
    assert any(item.get("handle") == "file:out.txt" for item in packet["state_inspection_handles"])
    assert "config_realization" not in packet


def test_verifier_packet_includes_envmap_raw_state_candidates_as_non_authoritative() -> None:
    env = EnvMap(
        task_prompt="Summarize /app/data/events.log into /app/summary.csv.",
        workspace_root="/app",
        visible_files=("data/events.log", "summary.csv", "tests/check_summary.py"),
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run shell", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
        },
        file_map_summary={
            "likely_inputs": ["data/events.log"],
            "instruction_referenced_visible_paths": ["data/events.log", "summary.csv"],
            "prompt_declared_output_paths": ["summary.csv"],
            "likely_tests_or_checkers": ["tests/check_summary.py"],
        },
    )
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(_ir(), env)
    packet = build_verifier_packet(compiled, ExecutionLedger(), step=0, reason="solver_submit", envmap=env)

    assert packet["raw_state_candidates"] == [
        {
            "path": "data/events.log",
            "source": "envmap.likely_inputs",
            "authority": "candidate_only",
        }
    ]


def test_verifier_packet_excludes_solver_command_stdout_but_exposes_output_handle() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    ledger.record(Receipt(
        "cmd-1", 3, "run_command", True, "command exit=0: python3 compare.py",
        payload={
            "command": "python3 compare.py",
            "exit_code": 0,
            "stdout": "BEFORE==AFTER True\nsemantic evidence\n",
            "stdout_handle": "out:cmd-1:stdout",
            "stdout_bytes": 36,
            "stderr": "",
            "modified_paths": ("clean.html",),
        },
    ))

    packet = build_verifier_packet(compiled, ledger, step=3, reason="deterministic_failure", envmap=_env())

    assert "solver_authored_evidence" not in packet
    assert "command_results" not in packet
    assert "BEFORE==AFTER True" not in json.dumps(packet)
    assert any(item.get("handle") == "out:cmd-1:stdout" for item in packet["state_inspection_handles"])
    assert "recent_command_receipts" not in packet


def test_verifier_packet_has_no_privileged_solver_or_proof_fields() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    ledger.record(Receipt(
        "cmd-1", 3, "run_command", True, "command exit=0: python3 compare.py",
        payload={
            "command": "python3 compare.py",
            "exit_code": 0,
            "stdout": "solver says task is correct\n",
            "stderr": "",
        },
    ))

    packet = build_verifier_packet(compiled, ledger, step=3, reason="solver_submit", envmap=_env())
    forbidden_names = {
        "solver_claim",
        "solver_claims",
        "submit_summary",
        "privileged_solver_proof",
        "solver_proof",
        "proof_contract",
        "proof_contract_analysis",
    }

    def walk(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key not in forbidden_names
                yield from walk(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from walk(nested)

    list(walk(packet))
    assert "solver_authored_evidence" not in packet
    assert "command_results" not in packet
    assert "solver says task is correct" not in json.dumps(packet)


def test_recent_command_receipts_are_audit_trail_not_privileged_proof() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    ledger.record(Receipt(
        "cmd-1", 3, "run_command", True, "command exit=0: python3 judge.py",
        payload={
            "command": "python3 judge.py",
            "exit_code": 0,
            "stdout": "task solved perfectly\n",
            "stdout_handle": "out:cmd-1:stdout",
            "stderr": "",
            "stderr_handle": "out:cmd-1:stderr",
        },
    ))

    packet = build_verifier_packet(compiled, ledger, step=3, reason="solver_submit", envmap=_env())

    assert "recent_command_receipts" not in packet
    dumped = json.dumps(packet)
    assert "task solved perfectly" not in dumped
    assert "solver_claim" not in dumped


def test_verifier_packet_includes_workbench_realization_and_repair_metadata() -> None:
    repaired = parse_workbench_architect_output(_repaired_workbench_config_json())
    assert repaired.config is not None

    envmap = _env()
    ir = harness_config_to_runtime_ir(repaired.config, envmap)
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(
        ir,
        envmap,
        objective_graph=_objective(),
        eval_index=_eval_index(),
    )
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    realization = dict(compiled.config_realization)
    realization["architect_path"] = "workbench"
    realization["harness_config_schema_version"] = repaired.config.schema_version
    realization["workbench_repair_warning_codes"] = list(repaired.config.repair_warning_codes)
    realization["workbench_repair_warnings"] = list(repaired.config.repair_warnings)
    realization["workbench_rejected_config_items"] = [dict(item) for item in repaired.config.rejected_config_items]
    realization["harness_config_realization_audit"] = config_realization_audit(repaired.config, envmap)
    ledger.record_config_realization(realization)
    hooks = Hooks([], ModelVerifierResult("completed", summary="ok"))

    result = run_model_verifier_if_available(
        hooks,
        compiled,
        ledger,
        step=0,
        reason="solver_submit",
        envmap=envmap,
        dynamic_state={},
    )

    assert result is not None and result.verdict == "completed"
    packet = hooks.verify_packets[0]
    assert packet["task_contract"]["raw_task_prompt"] == compiled.task_prompt
    assert packet["evidence_requirements"]["local_limits"] == [
        {
            "source": "runtime_config",
            "statement": "local checks cannot prove hidden grader behavior",
        }
    ]
    assert "config_realization" not in packet


def test_active_verifier_finding_persists_with_age_and_memory_since_finding() -> None:
    ledger = ExecutionLedger()
    finding = VerifierFinding(
        finding_id="vf-age", created_step=2, verdict="needs_repair", priority="blocking",
        summary="out.txt content unsupported", applies_to=("out.txt",),
        evidence=("check failed",), repair_instruction="rewrite out.txt",
    )

    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=2)
    ledger.record(Receipt("write-after", 5, "write_file", True, "rewrote out", state_change=True, payload={"path": "out.txt"}))

    context = ledger.active_finding_context(step=7)
    since_hits = ledger.query_memory("rewrote out", filters={"since_step": 3, "path": "out.txt"})

    assert context[0]["finding_id"] == "vf-age"
    assert context[0]["age_steps"] == 5
    assert since_hits and since_hits[0]["receipt_id"] == "write-after"


def test_completed_verifier_result_resolves_active_findings() -> None:
    ledger = ExecutionLedger()
    finding = VerifierFinding(
        finding_id="vf-resolve", created_step=1, verdict="needs_repair", priority="blocking",
        summary="needs repair", applies_to=("out.txt",),
    )

    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=1)
    assert ledger.active_finding_context(step=2)

    ledger.record(Receipt(
        receipt_id="inspection:resolved-out",
        step=3,
        kind="inspection_record",
        success=True,
        summary="current out.txt is fixed",
        payload={
            "inspection_id": "inspection:resolved-out",
            "route": "read_file:out.txt",
            "route_kind": "read_file",
            "target_identity": "path:out.txt",
            "target_generation": "content_hash:fixed",
            "task_state_generation": ledger.task_state_generation(),
            "tool_identity": "test.inspector",
            "result_hash": "fixed",
            "evidence_ceiling": "exact_contract",
            "eligible_for_proof": True,
        },
    ))
    ledger.apply_verifier_result(ModelVerifierResult(
        "completed",
        summary="fixed",
        completion_evidence=(CompletionEvidenceEntry(
            requirement="out.txt is fixed",
            observed="current inspection confirms fixed content",
            falsification_check="wrong content would contradict",
            inspection_refs=("inspection:resolved-out",),
        ),),
    ), step=3)

    assert ledger.active_finding_context(step=4) == []
    assert ledger.findings.archived["vf-resolve"].status == "resolved_by_current_evidence"


def test_overlapping_verifier_finding_supersedes_prior_finding() -> None:
    ledger = ExecutionLedger()
    first = VerifierFinding(
        finding_id="vf-old", created_step=1, verdict="needs_repair", priority="blocking",
        summary="old failure", applies_to=("out.txt",),
    )
    second = VerifierFinding(
        finding_id="vf-new", created_step=3, verdict="needs_repair", priority="blocking",
        summary="newer failure", applies_to=("out.txt",),
    )

    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(first,)), step=1)
    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(second,)), step=3)

    assert [item["finding_id"] for item in ledger.active_finding_context(step=4)] == ["vf-new"]
    assert ledger.findings.archived["vf-old"].status == "superseded"
    assert ledger.findings.archived["vf-old"].superseded_by == "vf-new"


def test_active_finding_can_be_invalidated_explicitly() -> None:
    ledger = ExecutionLedger()
    finding = VerifierFinding(
        finding_id="vf-invalid", created_step=1, verdict="needs_repair", priority="blocking",
        summary="bad finding", applies_to=("out.txt",),
    )

    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=1)
    ledger.findings.invalidate("vf-invalid")

    assert ledger.active_finding_context(step=2) == []
    assert ledger.findings.archived["vf-invalid"].status == "invalidated"


def test_non_completed_verifier_verdicts_record_failure_class() -> None:
    ledger = ExecutionLedger()

    ledger.apply_verifier_result(ModelVerifierResult("uncertain_missing_evidence", missing_evidence_requests=("read out.txt",)), step=1)
    ledger.apply_verifier_result(ModelVerifierResult("blocked_by_tooling", summary="tool unavailable"), step=2)

    receipts = [r for r in ledger.all_receipts() if r.kind == "model_verifier_result"]
    assert receipts[0].failure_class == "uncertain_missing_evidence"
    assert receipts[1].failure_class == "blocked_by_tooling"


def test_model_verifier_policy_skips_disabled_verifier_hook() -> None:
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(_env())).compile(
        RuntimeConfigIR(
            architect_summary="summary",
            solver_identity_prompt="solver",
            selected_capabilities=("shell", "filesystem"),
            completion_policy=CompletionPolicy(
                require_authoritative_check=True,
                require_all_obligations=False,
                require_recent_progress=False,
            ),
            model_verifier_policy=ModelVerifierPolicy(enabled=False),
            check_plan=(_check_id_out(),),
        ),
        _env(),
        objective_graph=_objective(),
        eval_index=_eval_index(),
    )
    ledger = ExecutionLedger()
    hooks = Hooks([], ModelVerifierResult("completed", summary="should not run"))

    result = run_model_verifier_if_available(
        hooks,
        compiled,
        ledger,
        step=2,
        reason="solver_submit",
    )

    assert result is None
    assert hooks.verify_packets == []
    receipts = ledger.all_receipts()
    assert receipts[-1].kind == "model_verifier_skipped"
    assert receipts[-1].payload["policy"]["enabled"] is False


def test_model_verifier_policy_runs_on_filters_verifier_reasons() -> None:
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(_env())).compile(
        RuntimeConfigIR(
            architect_summary="summary",
            solver_identity_prompt="solver",
            selected_capabilities=("shell", "filesystem"),
            completion_policy=CompletionPolicy(
                require_authoritative_check=True,
                require_all_obligations=False,
                require_recent_progress=False,
            ),
            model_verifier_policy=ModelVerifierPolicy(enabled=True, runs_on=("solver_submit",)),
            check_plan=(_check_id_out(),),
        ),
        _env(),
        objective_graph=_objective(),
        eval_index=_eval_index(),
    )
    ledger = ExecutionLedger()
    hooks = Hooks([], ModelVerifierResult("completed", summary="ok"))

    skipped = run_model_verifier_if_available(
        hooks,
        compiled,
        ledger,
        step=3,
        reason="deterministic_failure",
        envmap=_env(),
        dynamic_state={},
    )
    ran = run_model_verifier_if_available(
        hooks,
        compiled,
        ledger,
        step=4,
        reason="solver_submit",
        envmap=_env(),
        dynamic_state={},
    )

    assert skipped is None
    assert ran is not None and ran.verdict == "completed"
    assert len(hooks.verify_packets) == 1
    assert hooks.verify_packets[0]["reason"] == "solver_submit"
    assert any(r.kind == "model_verifier_packet" and r.payload["reason"] == "solver_submit" for r in ledger.all_receipts())


def test_model_verifier_timeout_records_error_after_packet(monkeypatch) -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()

    class SlowVerifierHooks:
        def verify(self, packet, compiled, ledger):
            time.sleep(0.2)
            return {"verdict": "completed", "summary": "too late"}

    monkeypatch.setenv("AETHER_MODEL_VERIFIER_TIMEOUT_S", "0.01")

    result = run_model_verifier_if_available(
        SlowVerifierHooks(),
        compiled,
        ledger,
        step=9,
        reason="solver_submit",
        envmap=_env(),
        dynamic_state={},
    )

    receipts = ledger.all_receipts()
    assert result is not None and result.verdict == "blocked_by_tooling"
    assert any(receipt.kind == "model_verifier_packet" and receipt.payload["packet"]["reason"] == "solver_submit" for receipt in receipts)
    assert any(receipt.kind == "model_verifier_error" and "timed out" in receipt.summary for receipt in receipts)


def test_parse_model_verifier_result_accepts_json_string_and_normalizes_findings() -> None:
    raw = json.dumps({
        "verdict": "needs_repair",
        "confidence": "high",
        "summary": "The artifact is wrong.",
        "blocking_findings": [
            {
                "id": "vf-json",
                "issue": "out.txt has the wrong content",
                "evidence": ["Observed: no requested value"],
                "next_action": "Rewrite out.txt with the requested value.",
                "paths": ["out.txt"],
            }
        ],
    })

    result = parse_model_verifier_result(raw)

    assert result.verdict == "needs_repair"
    assert result.confidence == "high"
    assert result.findings[0].finding_id == "vf-json"
    assert result.findings[0].summary == "out.txt has the wrong content"
    assert result.findings[0].repair_instruction == "Rewrite out.txt with the requested value."
    assert result.findings[0].applies_to == ("out.txt",)


def test_parse_model_verifier_result_rejects_unsupported_or_under_evidenced_results() -> None:
    try:
        parse_model_verifier_result({"verdict": "completed"})
    except ValueError as exc:
        assert "requires summary or evidence" in str(exc)
    else:
        raise AssertionError("completed without evidence should fail")

    try:
        parse_model_verifier_result({"verdict": "needs_repair", "summary": "bad"})
    except ValueError as exc:
        assert "requires at least one finding" in str(exc)
    else:
        raise AssertionError("needs_repair without finding should fail")

    try:
        parse_model_verifier_result({"verdict": "maybe"})
    except ValueError as exc:
        assert "unknown verifier verdict" in str(exc)
    else:
        raise AssertionError("unknown verdict should fail")


def test_verifier_packet_keeps_findings_and_state_handles_not_artifact_history() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    finding = VerifierFinding(
        finding_id="vf-artifact", created_step=2, verdict="needs_repair", priority="blocking",
        summary="out.txt needs a repair", applies_to=("out.txt",),
    )
    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=2)
    ledger.record(Receipt(
        "read-out", 3, "read_file", True, "read out.txt", state_change=False,
        payload={"path": "out.txt", "content_hash": "hash1", "bytes": 12, "excerpt": "hello world", "file_handle": "file:out.txt"},
    ))
    ledger.record(Receipt(
        "write-out", 4, "write_file", True, "rewrote out.txt", state_change=True,
        payload={"path": "out.txt", "modified_paths": ("out.txt",), "artifact_paths": ("out.txt",), "file_handle": "file:out.txt"},
    ))

    packet = build_verifier_packet(compiled, ledger, step=5, reason="deterministic_success_candidate", envmap=_env())

    assert packet["active_findings"][0]["finding_id"] == "vf-artifact"
    assert any(item.get("handle") == "file:out.txt" for item in packet["state_inspection_handles"])
    assert "artifact_evidence" not in packet
    assert "changes_since_active_findings" not in packet
    assert "recent_actions" not in packet


def test_verifier_packet_excludes_secret_observation_payloads_from_model_visible_packet() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    ledger.record(Receipt(
        "obs-secret", 1, "record_observation", True, "observed config",
        payload={
            "observation": "config observed",
            "path": "out.txt",
            "api_key": "sk-live-secret",
            "nested": {"access_token": "token-secret", "safe": "visible"},
        },
    ))

    packet = build_verifier_packet(compiled, ledger, step=2, reason="deterministic_failure", envmap=_env())

    assert "observations" not in packet
    assert "sk-live-secret" not in json.dumps(packet)
    assert "token-secret" not in json.dumps(packet)
    assert "config observed" not in json.dumps(packet)


def test_compiled_visible_smoke_is_compiled_but_not_put_in_state_only_verifier_packet() -> None:
    env = _env()
    raw = json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "Create out.txt.",
        "success_definition": "out.txt contains OK.",
        "solver_system_prompt": {
            "role": "verification-first solver",
            "workflow": ["inspect", "write", "self-check"],
            "self_verification": ["verify out.txt contains OK"],
            "memory_use": ["query_memory before repeats"],
        },
        "verifier_system_prompt": {
            "role": "Read-only verifier for out.txt content",
            "success_criteria": ["out.txt contains OK"],
            "required_evidence": ["content assertion or file inspection confirms OK"],
            "false_positive_traps": ["file existence without OK is insufficient"],
            "verdict_guidance": ["completed requires content evidence"],
            "feedback_guidance": ["name missing or wrong content"],
        },
        "evidence_requirements": ["out.txt contains OK"],
        "false_positive_risks": ["file existence without OK is insufficient"],
        "minimum_completion_evidence": ["content assertion or file inspection confirms OK"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "query_memory"]},
        "context_policy": {"mode": "retrieval_augmented"},
        "verification_policy": {
            "visible_smoke_tests": [
                {"type": "content_assertion", "path": "out.txt", "contains": "OK"}
            ],
            "solver_callable_checks": True,
        },
        "model_verifier_policy": {"enabled": True},
        "local_verification_limits": ["visible smoke is internal evidence only"],
    })
    config = parse_workbench_architect_output(raw).config
    assert config is not None
    ir = harness_config_to_runtime_ir(config, env)
    realization = config_realization_audit(config, env)
    compiled_smoke_ids = realization["dispositions"]["verification_policy"]["compiled_visible_smoke_checks"]
    assert compiled_smoke_ids
    smoke_result = compile_visible_smoke_tests(config, env)
    assert [check.check_id for check in smoke_result.checks] == compiled_smoke_ids
    ir = replace(ir, check_plan=ir.check_plan + tuple(compiled_smoke_ids))
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(
        ir,
        env,
        objective_graph=_objective(),
        eval_index=EvalIndex(checks=_eval_index().checks + smoke_result.checks),
    )
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective())
    ledger.record(Receipt("cfg", 0, "config_realization", True, "realized", payload={"config_realization": realization}))

    packet = build_verifier_packet(compiled, ledger, step=1, reason="deterministic_failure", envmap=env)

    assert "deterministic_checks" not in packet
    assert "config_realization" not in packet
    assert compiled_smoke_ids[0] not in json.dumps(packet)
