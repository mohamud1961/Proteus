"""Prompt-cache stability: the solver prompt's stable prefix must be
byte-identical across every step of a run; only the single trailing
``[context_packet]`` message may vary.

Provider prompt caches key on exact byte prefixes.  Any drift in the prefix
(reordered keys, embedded step counters, timestamps, refreshed envmaps)
silently destroys cache hits and is also a protocol-stability bug: the solver
should see one fixed workbench, not a subtly shifting one.
"""
from __future__ import annotations

import json
from typing import Mapping

from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CompiledRuntime,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Create out.txt containing OK.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands"),
            "filesystem": CapabilityDescriptor("filesystem", "Files"),
        },
    )


def _ir() -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="stability test",
        solver_identity_prompt="You are a careful solver.",
        verifier_identity_prompt="You are a state verifier.",
        selected_capabilities=("shell", "filesystem"),
    )


class _CapturingHooks:
    """Records the exact message list of every solve() call."""

    def __init__(self) -> None:
        self.captured: list[list[dict[str, str]]] = []
        self._step = 0

    def architect(self, request: Mapping[str, object]) -> RuntimeConfigIR:
        return _ir()

    def solve(self, messages: list[dict[str, str]], compiled: CompiledRuntime) -> SolverTurn:
        self.captured.append([dict(m) for m in messages])
        self._step += 1
        if self._step <= 3:
            return SolverTurn(
                kind="act",
                summary=f"act step {self._step}",
                actions=(ActionRequest(
                    action_id=f"a-{self._step}",
                    kind="write_file",
                    capability_id="filesystem",
                    arguments={"path": f"file_{self._step}.txt", "content": f"content {self._step}"},
                    intent="mutate state so context changes",
                    expected_observation="file written",
                    if_fail_next="report blocker",
                ),),
            )
        return SolverTurn(kind="submit_outcome", summary="done")


def test_prefix_is_byte_stable_across_all_steps_and_only_context_packet_varies() -> None:
    hooks = _CapturingHooks()
    AetherNextKernel(max_steps=4).run(_env(), MemoryExecutor(workspace_root="/app"), hooks)

    assert len(hooks.captured) >= 4
    first = hooks.captured[0]
    prefix_len = len(first) - 1
    reference_prefix = json.dumps(first[:prefix_len], sort_keys=True)

    for step, messages in enumerate(hooks.captured):
        assert len(messages) == prefix_len + 1, (
            f"step {step}: expected exactly one volatile message appended to the "
            f"stable prefix, got {len(messages)} messages"
        )
        assert json.dumps(messages[:prefix_len], sort_keys=True) == reference_prefix, (
            f"step {step}: stable prefix drifted"
        )
        assert messages[-1]["content"].startswith("[context_packet]\n"), (
            f"step {step}: volatile section must be the trailing [context_packet]"
        )

    # State changed every step, so the volatile packet must actually vary --
    # otherwise this test could pass against a frozen (broken) context.
    packets = {m[-1]["content"] for m in hooks.captured}
    assert len(packets) > 1, "context packet never changed despite state changes"


def test_prefix_contains_required_workbench_sections() -> None:
    hooks = _CapturingHooks()
    AetherNextKernel(max_steps=1).run(_env(), MemoryExecutor(workspace_root="/app"), hooks)
    sections = {
        m["content"].split("]", 1)[0].lstrip("[")
        for m in hooks.captured[0][:-1]
    }
    for required in (
        "kernel_contract",        # protocol card
        "solver_turn_contract",   # emit format + turn kinds
        "action_schema",          # tool schema
        "solver_identity",        # architect-authored solver prompt
        "task_prompt",            # task facts
        "envmap",                 # world facts
    ):
        assert required in sections, f"missing stable-prefix section: {required}"


def test_context_packet_serialization_is_deterministic() -> None:
    hooks = _CapturingHooks()
    AetherNextKernel(max_steps=1).run(_env(), MemoryExecutor(workspace_root="/app"), hooks)
    payload = hooks.captured[0][-1]["content"].split("\n", 1)[1]
    parsed = json.loads(payload)
    assert payload == json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ), "context packet must serialize deterministically (sorted keys, fixed separators)"
