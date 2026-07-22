"""Standing regression sentinel: a correct-but-differently-worded solution
must never trip a deterministic gate.

The harness owns structure (paths, hashes, exit codes, state changes); it must
not own wording. These tests pin that boundary: benign text containing scary
vocabulary ("failed", "error", "mismatch", benchmark-ish words) in summaries,
file contents, or successful command output must not produce blockers, blocks,
or failure classifications.
"""
from __future__ import annotations

from typing import Mapping

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.completion import CompletionGate, FailureParser
from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.ledger import ExecutionLedger
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CompiledRuntime,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)

_SCARY_TEXT = (
    "Historical notes: earlier attempts failed with a mismatch error and a "
    "traceback; this document describes why the final approach avoids the "
    "timeout and the permission denied issue seen before. assert nothing."
)


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Write notes.md describing the fix history.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands"),
            "filesystem": CapabilityDescriptor("filesystem", "Files"),
        },
    )


def _ir() -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="sentinel",
        solver_identity_prompt="solver prompt",
        verifier_identity_prompt="verifier prompt",
        selected_capabilities=("shell", "filesystem"),
    )


class _WordyButCorrectHooks:
    """Solver whose wording is unusual but whose actions are correct."""

    def __init__(self) -> None:
        self.calls = 0

    def architect(self, request: Mapping[str, object]) -> RuntimeConfigIR:
        return _ir()

    def solve(self, messages: list[dict[str, str]], compiled: CompiledRuntime) -> SolverTurn:
        self.calls += 1
        if self.calls == 1:
            return SolverTurn(
                kind="act",
                summary="previous run failed with an error; writing corrected notes now",
                actions=(ActionRequest(
                    action_id="a-write",
                    kind="write_file",
                    capability_id="filesystem",
                    arguments={"path": "notes.md", "content": _SCARY_TEXT},
                    intent="write the deliverable with unusual wording",
                    expected_observation="notes.md exists",
                    if_fail_next="report blocker",
                ),),
            )
        if self.calls == 2:
            return SolverTurn(
                kind="act",
                summary="display the written notes after observing the write",
                actions=(ActionRequest(
                    action_id="a-show",
                    kind="run_command",
                    capability_id="shell",
                    arguments={"command": "cat notes.md"},
                    intent="display mismatch/error wording without failing",
                    expected_observation="scary words on stdout, exit 0",
                    if_fail_next="report blocker",
                ),),
            )
        return SolverTurn(kind="submit_outcome", summary="error-free despite the word failed appearing")


def test_wording_never_trips_deterministic_gates() -> None:
    executor = MemoryExecutor(workspace_root="/app")
    executor.register_command(
        "cat notes.md",
        lambda ex, cmd: CommandResult(command=cmd, exit_code=0, stdout=_SCARY_TEXT),
    )
    result = AetherNextKernel(max_steps=3).run(_env(), executor, _WordyButCorrectHooks())

    blocking_kinds = {
        "safety_block",
        "integrity_block",
        "action_validation",
        "turn_validation",
        "no_progress_control",
    }
    tripped = [r for r in result.receipts if r.kind in blocking_kinds and not r.success]
    assert not tripped, [r.summary for r in tripped]

    # The successful display command must carry no failure classification.
    cmd = next(r for r in result.receipts if r.kind == "run_command")
    assert cmd.success
    assert cmd.failure_class == ""

    # The write of scary-worded content must succeed and register state change.
    write = next(r for r in result.receipts if r.kind == "write_file")
    assert write.success and write.state_change


def test_failure_parser_only_consulted_on_failures_and_gate_is_state_based() -> None:
    # FailureParser is wording-sensitive by design, but only failures are fed
    # to it (kernel calls classify() only when result.success is False).  The
    # completion gate itself must judge state, not wording.
    parser = FailureParser()
    assert parser.classify(_SCARY_TEXT, exit_code=0)  # wording alone classifies...

    env = _env()
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(_ir(), env)
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    decision = CompletionGate().evaluate(compiled, ledger, [])
    wording_codes = {"test_failure", "check_broken", "schema_mismatch", "timeout"}
    assert not wording_codes.intersection({b.code for b in decision.blockers})
