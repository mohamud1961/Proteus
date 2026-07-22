from __future__ import annotations

import json
from dataclasses import replace

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.kernel_verifier import run_model_verifier_if_available
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    CompletionPolicy,
    DeliverableSpec,
    EnvMap,
    EvalIndex,
    ObjectiveGraph,
    ProofObligation,
    RuntimeConfigIR,
    ModelVerifierPolicy,
)
from aether_next.task_contract import TaskClause, TaskContract
from aether_next.verifier_packets import build_verifier_packet
from aether_next.world import StableEnvMap, WorldState


def _compiled():
    env = EnvMap(
        task_prompt="Create the required output.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "run commands", ("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "read and write files", ("read_file", "write_file")),
        },
    )
    objective = ObjectiveGraph(
        deliverables=(DeliverableSpec(path="out.txt"),),
        obligations=(ProofObligation("artifact:out.txt", "artifact", "out exists", "out.txt"),),
    )
    ir = RuntimeConfigIR(
        architect_summary="architect summary",
        solver_identity_prompt="solver prompt that must stay out of verifier requests",
        selected_capabilities=("shell", "filesystem"),
        completion_policy=CompletionPolicy(require_all_obligations=True, require_recent_progress=False),
        success_definition="out.txt exists",
        evidence_requirements=("inspect the output path",),
    )
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(
        ir, env, objective_graph=objective, eval_index=EvalIndex()
    )


def test_verifier_packet_is_neutral_and_excludes_journey_or_prompts():
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    secret = "JOURNEY_SECRET_7f4b"
    ledger.record(Receipt(
        "cmd-1", 1, "run_command", True, "command completed",
        payload={
            "command": "cat out.txt",
            "stdout": secret,
            "stdout_handle": "output:cmd-1",
            "solver_reported_blocker": secret,
        },
    ))
    packet = build_verifier_packet(
        compiled,
        ledger,
        contract=None,
        envmap={"workspace": "/app"},
        dynamic_state={
            "schema_version": "dynamic_world_state.v1",
            "state_version": 1,
            "solver_journey": secret,
            "artifacts": {"out.txt": {"content": secret, "handle": "file:out.txt"}},
        },
    )
    encoded = json.dumps(packet, sort_keys=True)
    assert secret not in encoded
    assert "solver_system_prompt" not in packet
    assert "architect_verifier_prompt" not in encoded
    assert "verifier_strategy" not in encoded
    assert packet["task_contract"]["raw_task_prompt"] == compiled.task_prompt
    assert packet["state_inspection_handles"] == [
        {"kind": "output", "handle": "output:cmd-1", "stream": "stdout", "bytes": 0, "content_hash": ""},
    ]


def test_verifier_packet_is_frozen_and_large_values_are_handle_only():
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    large = "X" * 50_000
    ledger.record(Receipt(
        "out-1", 2, "write_file", True, "output written",
        payload={
            "artifact_paths": ("out.txt",),
            "file_handle": "file:out.txt",
            "bytes": len(large),
            "content_hash": "hash-out",
        },
    ))
    packet = build_verifier_packet(
        compiled,
        ledger,
        envmap={"workspace": "/app"},
        dynamic_state={"artifacts": {"out.txt": {"content": large, "handle": "file:out.txt"}}},
    )
    encoded = json.dumps(packet, sort_keys=True)
    assert large not in encoded
    assert any(item.get("handle") == "file:out.txt" for item in packet["state_inspection_handles"])
    with pytest.raises(TypeError):
        packet["dynamic_state"]["artifacts"]["out.txt"]["handle"] = "mutated"
    with pytest.raises(TypeError):
        packet["evidence_requirements"]["required"].append("journey")


def test_dynamic_state_denylist_normalizes_journey_and_output_key_variants() -> None:
    compiled = _compiled()
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    secret = "DO_NOT_LEAK_VARIANT"
    packet = build_verifier_packet(
        compiled,
        ledger,
        envmap={"workspace": "/app"},
        dynamic_state={
            "named_sections": {"plan": {"next": "inspect"}},
            "removed_services": ["web"],
            "removed_jobs": ["trainer"],
            "solverJourney": secret,
            "command_result": secret,
            "rawCommand": secret,
            "stdout_text": secret,
            "nested": {
                "solver_journey": secret,
                "commandResult": secret,
                "raw_command": secret,
                "stderrText": secret,
            },
        },
    )
    encoded = json.dumps(packet, sort_keys=True)
    assert secret not in encoded
    assert packet["dynamic_state"]["named_sections"] == {"plan": {"next": "inspect"}}
    assert packet["dynamic_state"]["removed_services"] == ["web"]
    assert packet["dynamic_state"]["removed_jobs"] == ["trainer"]


def test_live_world_snapshot_reaches_packet_without_raw_journey():
    compiled = _compiled()
    contract = TaskContract.create(
        "Create the required output.",
        [TaskClause("artifact:out.txt", "out exists", ("out.txt",))],
    )
    world = WorldState(
        task_contract=contract,
        stable_envmap=StableEnvMap.create({"workspace": "/app"}),
    )
    world.apply_delta(
        {
            "named_sections": {"plan": {"next": "submit"}},
            "services": {"web": {"state": "listening", "port": 8080}},
            "jobs": {"trainer": {"state": "running"}},
        }
    )
    world.apply_delta({"removed_services": ["web"], "removed_jobs": ["trainer"]})
    packet = build_verifier_packet(
        compiled,
        ExecutionLedger(),
        envmap=EnvMap(task_prompt="Create the required output.", workspace_root="/app"),
        dynamic_state=world.dynamic_snapshot(),
    )
    encoded = json.dumps(packet, sort_keys=True)
    assert packet["dynamic_state"]["named_sections"] == {"plan": {"next": "submit"}}
    assert packet["dynamic_state"]["removed_services"] == ["web"]
    assert packet["dynamic_state"]["removed_jobs"] == ["trainer"]
    assert "solver_journey" not in encoded
    assert "command" not in encoded


def test_verifier_fails_closed_when_state_or_stable_envmap_is_missing():
    compiled = replace(
        _compiled(),
        model_verifier_policy=ModelVerifierPolicy(enabled=True, runs_on=("solver_submit",)),
    )

    class Hooks:
        def __init__(self):
            self.called = False

        def verify(self, *_args):
            self.called = True
            raise AssertionError("model verifier must not run without complete state")

    for envmap, dynamic_state, missing in (
        (None, {"schema_version": "dynamic_world_state.v1"}, "stable_envmap"),
        ({"workspace": "/app"}, None, "dynamic_state"),
    ):
        hooks = Hooks()
        ledger = ExecutionLedger()
        result = run_model_verifier_if_available(
            hooks,
            compiled,
            ledger,
            step=4,
            reason="solver_submit",
            envmap=envmap,
            dynamic_state=dynamic_state,
        )
        assert result is not None
        assert result.verdict == "blocked_by_harness_config"
        assert not hooks.called
        unavailable = ledger.latest_receipt("verifier_state_unavailable")
        assert unavailable is not None
        assert missing in unavailable.payload["missing"]
