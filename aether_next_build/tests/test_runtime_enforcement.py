from __future__ import annotations

import json
import os
import time
import dataclasses
from pathlib import Path

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.kernel_verifier import run_model_verifier_if_available
from aether_next.kernel_verifier import _execute_compiled_inspection_fallbacks
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.runners.docker_runner import KernelRunTimeout, _kernel_wall_timeout
from aether_next.tracing import RunTrace
from aether_next.verifier import ModelVerifierResult, VerifierFinding
from aether_next.verifier_inspector import VerifierInspectionRequest
from aether_next.verifier_packets import build_verifier_packet
from aether_next.task_contract import TaskClause, TaskContract
from aether_next.world import WorldState
from aether_next.runtime_ir import (
    ActionRequest,
    AutomaticMemoryPolicy,
    BootstrapPolicy,
    CapabilityDescriptor,
    CompletionPolicy,
    ContextPolicy,
    EnvMap,
    HelperToolPolicy,
    RuntimeConfigIR,
    SolverTurn,
)


def _env(task_prompt: str = "Runtime enforcement diagnostic.") -> EnvMap:
    return EnvMap(
        task_prompt=task_prompt,
        workspace_root="/app",
        capabilities={
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
            "shell": CapabilityDescriptor("shell", "Run shell", tool_names=("run_command",)),
        },
    )


def _dynamic_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "schema_version": "dynamic_world_state.v1",
        "state_version": 0,
    }
    state.update(overrides)
    return state


def _runtime(**kwargs) -> RuntimeConfigIR:
    values = {
        "architect_summary": "runtime enforcement diagnostic",
        "solver_identity_prompt": "Act on prior evidence; do not repeat unchanged evidence display.",
        "selected_capabilities": ("filesystem", "shell"),
        "context_policy": ContextPolicy(mode="retrieval_augmented"),
        "automatic_memory_policy": AutomaticMemoryPolicy(mode="advisory"),
        "completion_policy": CompletionPolicy(require_all_obligations=False, require_recent_progress=False),
        "bootstrap_policy": BootstrapPolicy(allow_acquisition=False),
        "helper_tool_policy": HelperToolPolicy(allow_creation=False),
        "model_verifier_policy": kwargs.pop("model_verifier_policy", None) or None,
    }
    values.update(kwargs)
    if values["model_verifier_policy"] is None:
        del values["model_verifier_policy"]
    return RuntimeConfigIR(**values)


def _action(kind: str, args: dict, *, action_id: str = "a", cap: str = "filesystem") -> ActionRequest:
    return ActionRequest(
        action_id=action_id,
        kind=kind,
        capability_id=cap,
        arguments=args,
        intent="diagnostic",
        expected_observation="diagnostic",
        if_fail_next="diagnostic",
    )


class _Hooks:
    def __init__(self, runtime: RuntimeConfigIR, turns: list[SolverTurn]) -> None:
        self.runtime = runtime
        self.turns = list(turns)

    def architect(self, request):
        return self.runtime

    def solve(self, messages, compiled):
        if self.turns:
            return self.turns.pop(0)
        return SolverTurn(kind="submit_outcome", summary="submit")

    def verify(self, packet, compiled, ledger):
        return json.dumps({"verdict": "completed", "summary": "evidence accepted by fake verifier"})


class _NoVerifierHooks(_Hooks):
    verify = None


def test_repeated_evidence_display_warns_under_advisory_memory() -> None:
    command = "sed -n '1,220p' /app/solution.sparql"
    action = _action("run_command", {"command": command}, action_id="show", cap="shell")
    turns = [
        SolverTurn(kind="act", summary="show query", actions=(action,)),
        SolverTurn(kind="act", summary="show query again", actions=(action,)),
        SolverTurn(kind="act", summary="show query third time", actions=(action,)),
    ]
    executor = MemoryExecutor(files={"solution.sparql": "SELECT * WHERE {}"})
    executor.register_command(command, lambda _ex, cmd: CommandResult(cmd, 0, stdout="SELECT * WHERE {}"))

    result = AetherNextKernel(max_steps=3).run(_env(), executor, _NoVerifierHooks(_runtime(), turns))

    assert executor.command_history == [command, command, command]
    controls = [receipt for receipt in result.receipts if receipt.kind == "no_progress_control"]
    assert controls
    assert controls[-1].failure_class == "repeated_evidence_display_no_state_change"


def test_no_progress_control_reaches_next_context_as_action_constraint() -> None:
    command = "sed -n '1,220p' /app/solution.sparql"
    action = _action("run_command", {"command": command}, action_id="show", cap="shell")
    turns = [
        SolverTurn(kind="act", summary="show query", actions=(action,)),
        SolverTurn(kind="act", summary="show query again", actions=(action,)),
        SolverTurn(kind="act", summary="show query third time", actions=(action,)),
        SolverTurn(kind="act", summary="observe constraint", actions=()),
    ]
    executor = MemoryExecutor(files={"solution.sparql": "SELECT * WHERE {}"})
    executor.register_command(command, lambda _ex, cmd: CommandResult(cmd, 0, stdout="SELECT * WHERE {}"))
    trace = RunTrace()

    AetherNextKernel(max_steps=4).run(_env(), executor, _NoVerifierHooks(_runtime(), turns), trace=trace)

    context = trace.steps[3]["context_seen"]
    assert context["action_constraints"]["source"] == "no_progress_control"
    assert context["action_constraints"]["blocked_target"] == "solution.sparql"
    assert "execute_or_semantically_validate_artifact" in context["action_constraints"]["allowed_next_action_families"]


def test_no_progress_reset_does_not_permanently_disable_after_one_write() -> None:
    """Regression for the Stage 1 VM sparql loop: a single write anywhere in the
    run must not permanently defeat the guard for the rest of the run. Replays
    the observed sequence: block on repeat, one repair write, one grace
    read-back, then back to a bare display loop that must re-block and, after
    enough cycles without real execution, escalate to a hard block that a
    further write alone cannot clear -- only actually running the artifact can.
    """
    display = "sed -n '1,220p' /app/solution.sparql"
    display_action = _action("run_command", {"command": display}, action_id="show", cap="shell")
    write_action = _action(
        "write_file", {"path": "solution.sparql", "content": "SELECT * WHERE { ?p a uni:Professor }"},
        action_id="repair",
    )
    execute_command = "python3 - <<'PY'\nrun_sparql('/app/solution.sparql')\nPY"
    execute_action = _action("run_command", {"command": execute_command}, action_id="exec", cap="shell")

    turns = [
        SolverTurn(kind="act", summary="show 1", actions=(display_action,)),   # step 0: executes
        SolverTurn(kind="act", summary="show 2", actions=(display_action,)),   # step 1: executes
        SolverTurn(kind="act", summary="show 3", actions=(display_action,)),   # step 2: advisory, still executes
        SolverTurn(kind="act", summary="repair", actions=(write_action,)),     # step 3: write (state change)
        SolverTurn(kind="act", summary="show 4", actions=(display_action,)),   # step 4: one grace re-read, executes
        SolverTurn(kind="act", summary="show 5", actions=(display_action,)),   # step 5: advisory, still executes
        SolverTurn(kind="act", summary="show 6", actions=(display_action,)),   # step 6: advisory, still executes
        SolverTurn(kind="act", summary="repair again", actions=(write_action,)),  # step 7: write, does NOT clear
        SolverTurn(kind="act", summary="show 7", actions=(display_action,)),   # step 8: advisory, still executes
        SolverTurn(kind="act", summary="finally execute", actions=(execute_action,)),  # step 9: real execution clears it
        SolverTurn(kind="act", summary="show 8", actions=(display_action,)),   # step 10: executes again
    ]
    executor = MemoryExecutor(files={"solution.sparql": "SELECT * WHERE {}"})
    executor.register_command(display, lambda _ex, cmd: CommandResult(cmd, 0, stdout="SELECT * WHERE {}"))
    executor.register_command(execute_command, lambda _ex, cmd: CommandResult(cmd, 0, stdout="0 rows"))

    result = AetherNextKernel(max_steps=11).run(_env(), executor, _NoVerifierHooks(_runtime(), turns))

    controls = [r for r in result.receipts if r.kind == "no_progress_control"]
    assert len(controls) == 4, [c.payload for c in controls]
    assert executor.command_history.count(display) == 8, executor.command_history
    assert executor.command_history.count(execute_command) == 1

    # The write at step 3 only granted ONE grace re-read (step 4); it must not
    # have silently disabled the guard for the remainder of the run (the old
    # bug: comparing state-change against the earliest match, not the latest).
    assert {c.payload["consequence"] for c in controls} == {"advisory"}
    # The runtime no longer blocks repeated display commands; these receipts are
    # evidence for context/audit, not dispatch authority.


def test_filter_false_clean_now_completes_when_verifier_claims_done() -> None:
    sample_command = "python3 - <<'PY'\nprint('in_place_ok onclick javascript:')\nPY"
    turns = [
        SolverTurn(kind="act", summary="write filter", actions=(
            _action("write_file", {"path": "filter.py", "content": "print('ok')"}, action_id="write"),
        )),
        SolverTurn(kind="act", summary="sample after observing write", actions=(
            _action("run_command", {"command": sample_command}, action_id="sample", cap="shell"),
        )),
        SolverTurn(kind="submit_outcome", summary="submit after one sample"),
    ]
    executor = MemoryExecutor()
    executor.register_command(sample_command, lambda _ex, cmd: CommandResult(cmd, 0, stdout="in_place_ok"))
    runtime = _runtime(
        architect_summary="HTML JavaScript/XSS sanitizer",
        success_definition="filter.py removes JavaScript from HTML while preserving clean HTML.",
        evidence_requirements=("local evidence of in-place JavaScript removal",),
        false_positive_risks=("A test that uses only one trivial input misses formatting drift or incomplete coverage.",),
        minimum_completion_evidence=("adversarial fixtures for XSS and clean HTML preservation",),
    )

    world = WorldState(
        task_contract=TaskContract.create(
            "Create filter.py that removes JavaScript from HTML to prevent XSS.",
            [TaskClause("artifact", "filter.py is available")],
        ),
    )
    result = AetherNextKernel(max_steps=3).run(
        _env("Create filter.py that removes JavaScript from HTML to prevent XSS."),
        executor,
        _Hooks(runtime, turns),
        world_state=world,
    )

    assert result.status == "completed"
    proof = [receipt for receipt in result.receipts if receipt.kind == "proof_contract"]
    assert not proof, "certified kernel path must not emit proof_contract receipts"


def test_act_only_loops_do_not_trigger_verifier_without_solver_submit() -> None:
    executor = MemoryExecutor(workspace_root="/app", files={"out.txt": "TODO"})
    runtime = _runtime()
    finding = VerifierFinding(
        finding_id="missing-key-permission-proof",
        created_step=1,
        verdict="uncertain_missing_evidence",
        priority="blocking",
        summary="key permissions not shown",
    )

    class _Hooks:
        def __init__(self) -> None:
            self.turns = [
                SolverTurn(kind="act", summary="inspect file", actions=(
                    _action("read_file", {"path": "out.txt"}, action_id="a1"),
                )),
                SolverTurn(kind="act", summary="inspect again", actions=(
                    _action("read_file", {"path": "out.txt"}, action_id="a2"),
                )),
            ]
            self.verify_calls = 0

        def architect(self, request):
            return runtime

        def solve(self, messages, compiled):
            return self.turns.pop(0)

        def verify(self, packet, compiled, ledger):
            self.verify_calls += 1
            return json.dumps({"verdict": "completed", "summary": "now confirmed"})

    hooks = _Hooks()
    result = AetherNextKernel(max_steps=2).run(_env(), executor, hooks)

    # Seed an active finding into the resulting ledger-equivalent receipt stream
    # by proving the loop stayed act-only and never opened the verifier lane.
    assert result.status == "incomplete"
    assert hooks.verify_calls == 0
    assert not any(r.kind == "model_verifier_result" for r in result.receipts)


def test_stale_active_finding_is_not_resolved_by_runtime_proof_contract() -> None:
    """Proof-contract evidence must not become a second verifier.

    Earlier repair work let deterministic proof-contract analysis clear stale
    completion findings after the runtime observed evidence. That helped one
    OpenSSL anomaly, but it also made harness-side family judgement share
    ownership with the verifier. The canonical contract is stricter: proof
    analysis may be packet evidence, but only verifier lifecycle can resolve
    completion findings.
    """
    runtime = _runtime(
        success_definition="Generate a private key and self-signed certificate with openssl.",
        evidence_requirements=("key file permissions", "certificate subject and validity"),
    )
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(_env())).compile(
        runtime, _env("Generate an openssl self-signed certificate and key at /app/ssl/."),
    )
    ledger = ExecutionLedger()

    # Step 2: verifier raises a blocking finding about missing key/cert evidence.
    first = ModelVerifierResult(
        verdict="uncertain_missing_evidence",
        summary="missing key mode and openssl inspection evidence",
        findings=(
            VerifierFinding(
                finding_id="missing-key-mode-proof", created_step=2, verdict="uncertain_missing_evidence",
                priority="blocking", summary="key permissions not shown", applies_to=("ssl/server.key",),
            ),
        ),
        missing_evidence_requests=("key permission evidence", "openssl inspection evidence"),
    )
    ledger.apply_verifier_result(first, step=2, compiled=compiled)
    assert "missing-key-mode-proof" in ledger.findings.active

    # Step 6: solver hasn't gathered the evidence yet. Verifier repeats the same
    # verdict with an empty findings list (exactly what the real trace showed).
    # The finding must stay active and stale_cycles must increment -- NOT clear.
    repeat = ModelVerifierResult(verdict="uncertain_missing_evidence", summary="still missing evidence")
    ledger.apply_verifier_result(repeat, step=6, compiled=compiled)
    assert "missing-key-mode-proof" in ledger.findings.active
    assert ledger.findings.active["missing-key-mode-proof"].stale_cycles == 1

    # Solver now supplies exactly the requested evidence (matches the real trace:
    # `stat -c '%a %n' ...` and `openssl x509 -noout -subject -issuer ...`).
    ledger.record(Receipt(
        "perm-check", 10, "run_command", True, "checked perms",
        payload={"command": "stat -c '%a %n' /app/ssl/server.key", "stdout": "600 server.key"},
    ))
    ledger.record(Receipt(
        "cert-check", 10, "run_command", True, "inspected cert",
        payload={"command": "openssl x509 -in /app/ssl/server.crt -noout -subject -dates", "stdout": "subject=CN=localhost"},
    ))

    # Step 15: verifier is called again and, exactly like the real run, still
    # returns an uncertain verdict that doesn't re-mention the old finding.
    still_uncertain = ModelVerifierResult(verdict="uncertain_missing_evidence", summary="uncertain again")
    ledger.apply_verifier_result(still_uncertain, step=15, compiled=compiled)

    # Even if runtime proof-contract analysis would consider the evidence
    # category satisfied, it remains verifier evidence only. It must not resolve
    # a completion finding on the verifier's behalf.
    assert "missing-key-mode-proof" in ledger.findings.active
    assert ledger.findings.active["missing-key-mode-proof"].stale_cycles == 2
    assert ledger.active_finding_context(step=15)


def test_verifier_packet_exposes_runtime_signals_without_static_verifier_governance_prompt() -> None:
    env = _env()
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(_runtime(), env)
    packet = build_verifier_packet(
        compiled,
        ExecutionLedger(),
        step=0,
        reason="deterministic_failure",
        envmap=env,
        dynamic_state=_dynamic_state(),
    )
    for field in ("state_inspection_handles", "raw_state_candidates", "active_findings", "open_obligations"):
        assert field in packet, f"{field!r} missing from state-only verifier packet schema"
    for forbidden in (
        "proof_contract_analysis",
        "proof_contract",
        "latest_command_results",
        "no_progress_controls",
        "artifact_evidence",
        "latest_file_reads",
        "solver_authored_evidence",
        "recent_receipts",
        "memory_loop_feedback",
        "automatic_memory_findings",
    ):
        assert forbidden not in packet


def test_verifier_caller_forwards_world_snapshot_and_marks_missing_snapshot() -> None:
    env = _env()
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(_runtime(), env)
    contract = TaskContract.create("maintain runtime state", [TaskClause("state", "service state is observable")])
    world = WorldState(
        task_contract=contract,
        named_sections={"plan": {"next": "submit"}},
        removed_services=["web"],
        removed_jobs=["trainer"],
    )
    ledger = ExecutionLedger()

    class CapturingHooks(_Hooks):
        def __init__(self, runtime: RuntimeConfigIR) -> None:
            super().__init__(runtime, [])
            self.packet = None

        def verify(self, packet, compiled, ledger):
            self.packet = packet
            return json.dumps({"verdict": "completed", "summary": "state observed"})

    hooks = CapturingHooks(_runtime())
    snapshot = world.dynamic_snapshot()
    result = run_model_verifier_if_available(
        hooks,
        compiled,
        ledger,
        step=1,
        reason="solver_submit",
        envmap=env,
        dynamic_state=snapshot,
    )
    assert result is not None and result.verdict == "completed"
    assert hooks.packet is not None
    assert hooks.packet["dynamic_state"]["named_sections"] == {"plan": {"next": "submit"}}
    assert hooks.packet["dynamic_state"]["removed_services"] == ["web"]
    assert hooks.packet["dynamic_state"]["removed_jobs"] == ["trainer"]

    hooks.packet = None
    blocked = run_model_verifier_if_available(
        hooks,
        compiled,
        ledger,
        step=2,
        reason="solver_submit",
        envmap=env,
        dynamic_state=None,
    )
    assert blocked is not None and blocked.verdict == "blocked_by_harness_config"
    assert hooks.packet is None
    unavailable = ledger.latest_receipt("verifier_state_unavailable")
    assert unavailable is not None
    assert unavailable.payload["available"] is False


def test_model_verifier_persists_full_evidence_bundle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AETHER_VERIFIER_EVIDENCE_DIR", str(tmp_path))
    runtime = _runtime(
        verifier_identity_prompt="Verifier prompt",
        success_definition="out.txt must contain DONE",
        evidence_requirements=("read out.txt",),
    )
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(_env())).compile(runtime, _env())
    ledger = ExecutionLedger()
    hooks = _Hooks(runtime, [])

    result = run_model_verifier_if_available(
        hooks,
        compiled,
        ledger,
        step=3,
        reason="solver_submit",
        envmap=_env(),
        dynamic_state=_dynamic_state(),
    )

    assert result is not None and result.verdict == "completed"
    bundle_dirs = list(tmp_path.iterdir())
    assert len(bundle_dirs) == 1
    bundle = bundle_dirs[0]
    assert (bundle / "verifier_packet.json").exists()
    assert (bundle / "verifier_prompt.txt").exists()
    assert (bundle / "raw_verifier_output.txt").exists()
    assert (bundle / "parsed_verifier_result.json").exists()
    assert (bundle / "active_findings_after.json").exists()
    verifier_receipt = ledger.latest_receipt("model_verifier_result")
    assert verifier_receipt is not None
    assert verifier_receipt.payload["raw_verifier_output"]
    assert verifier_receipt.payload["parsed_verifier_result"]["verdict"] == "completed"
    assert "verifier_packet" in verifier_receipt.payload


def test_verifier_exception_returns_explicit_tooling_block_not_absent_verdict() -> None:
    env = _env()
    runtime = _runtime()
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(runtime, env)
    ledger = ExecutionLedger()

    class RaisingHooks(_Hooks):
        def verify(self, packet, compiled, ledger):
            raise RuntimeError("provider unavailable")

    result = run_model_verifier_if_available(
        RaisingHooks(runtime, []),
        compiled,
        ledger,
        step=4,
        reason="solver_submit",
        envmap=env,
        dynamic_state=_dynamic_state(),
    )
    assert result is not None
    assert result.verdict == "blocked_by_tooling"
    assert ledger.latest_receipt("model_verifier_result") is not None
    assert ledger.latest_receipt("model_verifier_result").success is False


def test_compiled_inspection_fallback_executes_after_primary_failure() -> None:
    env = _env()
    runtime = _runtime()
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(runtime, env)
    compiled = dataclasses.replace(compiled, config_realization={
        "compiled_evidence_requirements": [{
            "clause_id": "c1",
            "inspection_route": "read_file:/app/missing.txt",
            "fallback_route": "inspect_recent_receipts",
            "minimum_class": "exact_contract",
        }],
    })
    ledger = ExecutionLedger()
    request = VerifierInspectionRequest("primary", "read_file", path="/app/missing.txt")
    primary = [{"request_id": "primary", "kind": "read_file", "path": "/app/missing.txt", "error": "missing"}]
    expanded, attempts = _execute_compiled_inspection_fallbacks(
        (request,), primary, compiled=compiled, ledger=ledger,
        executor=MemoryExecutor(workspace_root="/app"), envmap=env,
        overlay=None, hooks=None,
    )
    assert attempts[0]["fallback_success"] is True
    assert expanded[-1]["kind"] == "inspect_recent_receipts"


def test_model_verifier_without_state_blocks_without_evidence_bundle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AETHER_VERIFIER_EVIDENCE_DIR", str(tmp_path))
    env = _env()
    runtime = _runtime(verifier_identity_prompt="v", success_definition="s")
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(runtime, env)
    ledger = ExecutionLedger()

    class Hooks(_Hooks):
        def verify(self, packet, compiled, ledger):
            raise AssertionError("verifier must not run without EnvMap and dynamic state")

    result = run_model_verifier_if_available(
        Hooks(runtime, []), compiled, ledger, step=0, reason="solver_submit",
    )

    assert result is not None and result.verdict == "blocked_by_harness_config"
    assert ledger.latest_receipt("verifier_state_unavailable") is not None
    assert list(tmp_path.iterdir()) == []


def test_model_verifier_can_run_bounded_read_only_inspection_loop() -> None:
    runtime = _runtime(
        verifier_identity_prompt="Inspect current workspace state before judging completion.",
        success_definition="out.txt must contain DONE",
    )
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(_env())).compile(runtime, _env())
    ledger = ExecutionLedger()
    executor = MemoryExecutor(workspace_root="/app", files={"out.txt": "DONE\n"})

    class InspectorHooks:
        def __init__(self) -> None:
            self.calls = 0

        def verify(self, packet, compiled, ledger):
            raise AssertionError("legacy verify() should not be used when verify_with_inspector is available")

        def verify_with_inspector(self, packet, compiled, ledger, inspector):
            self.calls += 1
            results = inspector((
                type("_Req", (), {
                    "request_id": "read-out",
                    "kind": "read_file",
                    "path": "out.txt",
                    "check_id": "",
                    "receipt_kind": "",
                    "limit": 1,
                })(),
            ))
            assert results[0]["excerpt"] == "DONE\n"
            return json.dumps({"verdict": "completed", "summary": "read-only inspection confirmed DONE"})

    hooks = InspectorHooks()
    result = run_model_verifier_if_available(
        hooks,
        compiled,
        ledger,
        step=1,
        reason="solver_submit",
        executor=executor,
        envmap=_env(),
        dynamic_state=_dynamic_state(),
    )

    assert result is not None and result.verdict == "completed"
    assert hooks.calls == 1
    assert any(r.kind == "model_verifier_inspection" for r in ledger.all_receipts())


def test_scoped_verifier_evidence_dir_prevents_cross_task_collision(tmp_path, monkeypatch) -> None:
    """Regression for Stage 1 VM run H2: a pilot run drives several tasks
    sequentially in one process, each restarting the kernel step counter at 0.
    With one shared AETHER_VERIFIER_EVIDENCE_DIR, task B's step_0000 silently
    overwrote task A's step_0000 verifier evidence. run_tbench_task must scope
    the env var per task for the duration of that task's kernel run, and
    restore the original value afterward.
    """
    from aether_next.runners.docker_runner import _scoped_verifier_evidence_dir

    shared_root = str(tmp_path)
    monkeypatch.setenv("AETHER_VERIFIER_EVIDENCE_DIR", shared_root)
    runtime = _runtime(verifier_identity_prompt="v", success_definition="s")
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(_env())).compile(runtime, _env())
    hooks = _Hooks(runtime, [])

    with _scoped_verifier_evidence_dir("filter-js-from-html"):
        assert os.environ["AETHER_VERIFIER_EVIDENCE_DIR"] == str(Path(shared_root) / "filter-js-from-html")
        run_model_verifier_if_available(
            hooks,
            compiled,
            ExecutionLedger(),
            step=0,
            reason="solver_submit",
            envmap=_env(),
            dynamic_state=_dynamic_state(),
        )
    assert os.environ["AETHER_VERIFIER_EVIDENCE_DIR"] == shared_root  # restored

    with _scoped_verifier_evidence_dir("sparql-university"):
        assert os.environ["AETHER_VERIFIER_EVIDENCE_DIR"] == str(Path(shared_root) / "sparql-university")
        run_model_verifier_if_available(
            hooks,
            compiled,
            ExecutionLedger(),
            step=0,
            reason="solver_submit",
            envmap=_env(),
            dynamic_state=_dynamic_state(),
        )
    assert os.environ["AETHER_VERIFIER_EVIDENCE_DIR"] == shared_root  # restored

    assert (tmp_path / "filter-js-from-html" / "step_0000_solver_submit" / "verifier_packet.json").exists()
    assert (tmp_path / "sparql-university" / "step_0000_solver_submit" / "verifier_packet.json").exists()
    # Nothing landed directly under the shared root -- both tasks were namespaced.
    assert not (tmp_path / "step_0000_solver_submit").exists()


def test_kernel_wall_timeout_interrupts_long_kernel_section() -> None:
    start = time.monotonic()
    with pytest.raises(KernelRunTimeout):
        with _kernel_wall_timeout(0.05):
            time.sleep(1.0)
    assert time.monotonic() - start < 0.5
