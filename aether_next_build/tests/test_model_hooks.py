"""Tests for aether_next.model_hooks — stub ModelCallable, NO network."""
from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.model_hooks import (
    ModelHooks,
    ModelOutputError,
    _completed_inspection_is_semantically_grounded,
    _default_completion_inspection_requests,
    _extract_json_object,
    parse_runtime_config_ir,
    parse_solver_turn,
)
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)
from aether_next.verifier_inspector import VerifierInspectionRequest
from aether_next.verifier import parse_model_verifier_result
from aether_next.workbench_config import parse_harness_config_ir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envmap(**overrides: Any) -> EnvMap:
    defaults: dict[str, Any] = {
        "task_prompt": "Write hello.py that prints hello.",
        "workspace_root": "/app",
        "visible_files": ("hello.py",),
        "capabilities": {
            "shell": CapabilityDescriptor(
                capability_id="shell", summary="run commands"
            ),
            "filesystem": CapabilityDescriptor(
                capability_id="filesystem", summary="read/write files"
            ),
        },
    }
    defaults.update(overrides)
    return EnvMap(**defaults)


def _stub_model(response: str):
    """Return a ModelCallable that always returns *response*."""

    def model(
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        return response

    return model


def _valid_architect_json(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "architect_summary": "Build a hello-world script.",
        "solver_identity_prompt": "You are a Python developer.",
        "selected_capabilities": ["shell", "filesystem"],
        "workflow_policy": {"mode": "direct_build"},
        "process_policy": {"mode": "stateless_shell"},
        "solver_model_tier": "mini",
        "verifier_model_tier": "mini",
        "perception_model_tier": "vision",
        "architect_model_tier": "strong",
        "inspection_plan": ["hello.py"],
        "proof_plan": ["run hello.py and check output"],
        "check_plan": [],
        "forbidden_paths": [],
    }
    data.update(overrides)
    return json.dumps(data)


def _valid_submit_json() -> str:
    return json.dumps({
        "kind": "submit_outcome",
        "summary": "All deliverables ready, submitting.",
    })


def _valid_verifier_json(refs: tuple[str, ...] | None = None) -> str:
    payload: dict[str, Any] = {
        "verdict": "completed",
        "confidence": "high",
        "summary": "Packet evidence supports internal completion.",
    }
    if refs:
        payload["completion_evidence"] = [{
            "requirement": "the required deliverable matches the visible task",
            "observed": "inspected deliverable content matches the requirement",
            "inspection_refs": list(refs),
            "falsification_check": "differing inspected content would have contradicted completion",
        }]
    return json.dumps(payload)


def _inspection_ids_from_messages(messages: list[dict[str, str]]) -> tuple[str, ...]:
    for message in reversed(messages):
        try:
            payload = json.loads(message.get("content", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        rows = payload.get("verifier_inspection_results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        ids = tuple(
            str(row.get("inspection_id", "")).strip()
            for row in rows
            if isinstance(row, dict) and str(row.get("inspection_id", "")).strip()
        )
        if ids:
            return ids
    return ()


def _valid_act_json(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "kind": "act",
        "summary": "Run the build command.",
        "actions": [
            {
                "action_id": "a1",
                "kind": "run_command",
                "capability_id": "shell",
                "arguments": {"command": "python hello.py"},
                "intent": "execute the script",
                "expected_observation": "hello printed",
                "if_fail_next": "check syntax",
            }
        ],
    }
    data.update(overrides)
    return json.dumps(data)


def _canonical_workbench_architect_config():
    config = parse_harness_config_ir(json.dumps({
        "schema_version": "harness_config.v1",
        "task_understanding": "Write results.json with fit results.",
        "success_definition": "results.json contains correct fit values.",
        "solver_system_prompt": {
            "role": "Fit result solver",
            "workflow": ["inspect inputs", "write results.json", "self-verify", "submit"],
            "self_verification": ["read results.json and validate values before submitting"],
            "memory_use": ["automatic memory surfaces repeated reads/checks; use prior evidence or narrow the action"],
            "stop_conditions": ["Ready to submit only when results.json has been checked against the task evidence"],
            "avoid": ["Do not submit if values were guessed or only file shape was checked"],
        },
        "verifier_system_prompt": {
            "role": "Read-only current-state verifier for fit result JSON",
            "success_criteria": ["results.json contains correct task-specific fit values"],
            "required_evidence": ["current results.json content and validation evidence support completion"],
            "false_positive_traps": ["well-shaped JSON with wrong numeric values"],
            "verdict_guidance": ["completed requires inspecting current deliverable evidence"],
            "feedback_guidance": ["name the wrong or missing values and evidence needed"],
        },
        "evidence_requirements": ["results.json exists", "current results.json values are supported by task evidence"],
        "false_positive_risks": ["well-shaped JSON can contain wrong numeric values"],
        "minimum_completion_evidence": ["current results.json content and validation evidence"],
        "tool_policy": {"enabled_tools": ["read_file", "write_file", "query_memory"]},
        "context_policy": {"mode": "retrieval_augmented"},
        "model_verifier_policy": {"enabled": True, "runs_on": ["solver_submit"]},
    }))

    class _StaticWorkbenchArchitect:
        def configure(self, request: Mapping[str, Any]):
            return config, []

    return _StaticWorkbenchArchitect()


# ---------------------------------------------------------------------------
# 1. Valid architect IR -> RuntimeConfigIR
# ---------------------------------------------------------------------------

class TestArchitectValid:
    def test_returns_runtime_config_ir(self) -> None:
        envmap = _make_envmap()
        registry = CapabilityRegistry.from_envmap(envmap)
        compiler = ConfigCompiler(registry)

        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=_stub_model(_valid_submit_json()),
        )
        request = AetherNextKernel().build_architect_request(envmap, compiler)
        ir = hooks.architect(request)

        assert isinstance(ir, RuntimeConfigIR)
        assert ir.architect_summary == "Build a hello-world script."
        assert "shell" in ir.selected_capabilities
        assert ir.workflow_policy.mode == "direct_build"

    def test_passes_compiler_validate_no_fatal(self) -> None:
        envmap = _make_envmap()
        registry = CapabilityRegistry.from_envmap(envmap)
        compiler = ConfigCompiler(registry)

        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=_stub_model(_valid_submit_json()),
        )
        request = AetherNextKernel().build_architect_request(envmap, compiler)
        ir = hooks.architect(request)

        issues = compiler.validate(ir, envmap)
        fatal = [i for i in issues if i.fatal]
        assert not fatal, f"fatal issues: {[i.code for i in fatal]}"


# ---------------------------------------------------------------------------
# 2. Garbage architect output -> visible model-output failure, no safe default
# ---------------------------------------------------------------------------

class TestArchitectGarbage:
    def test_garbage_raises_model_output_error(self) -> None:
        envmap = _make_envmap()
        registry = CapabilityRegistry.from_envmap(envmap)
        compiler = ConfigCompiler(registry)

        hooks = ModelHooks(
            architect_model=_stub_model("I am not JSON at all!!!"),
            solver_model=_stub_model(_valid_submit_json()),
        )
        request = AetherNextKernel().build_architect_request(envmap, compiler)

        with pytest.raises(ModelOutputError, match="architect output could not be parsed"):
            hooks.architect(request)
        assert hooks.last_parse_errors, "expected parse errors to be recorded"
    def test_garbage_does_not_return_runtime_config_ir(self) -> None:
        envmap = _make_envmap()
        registry = CapabilityRegistry.from_envmap(envmap)
        compiler = ConfigCompiler(registry)

        hooks = ModelHooks(
            architect_model=_stub_model("{not valid json"),
            solver_model=_stub_model(_valid_submit_json()),
        )
        request = AetherNextKernel().build_architect_request(envmap, compiler)
        with pytest.raises(ModelOutputError):
            hooks.architect(request)
        assert hooks.last_parse_errors


def test_completed_inspection_requires_result_bearing_evidence_for_semantic_tasks() -> None:
    packet = {
        "false_positive_risks": ["shape-valid artifact may still be semantically wrong"],
        "local_verification_limits": [{"source": "runtime_config", "statement": "shape-only checks are insufficient"}],
    }
    shape_only = [
        {"kind": "read_file", "path": "jump_analyzer.py", "excerpt": "print('ok')"},
        {"kind": "inspect_recent_receipts", "rows": []},
    ]
    grounded = [
        {"kind": "read_output", "handle": "9:a-1:stdout", "excerpt": "takeoff=87 landing=119 sampled_scores=[...]"},
    ]

    assert _completed_inspection_is_semantically_grounded(packet, shape_only) is False
    assert _completed_inspection_is_semantically_grounded(packet, grounded) is True


# ---------------------------------------------------------------------------
# 3. Valid submit_outcome solver JSON -> SolverTurn .validate()
# ---------------------------------------------------------------------------

class TestSolverSubmit:
    def test_submit_outcome_valid(self) -> None:
        turn = parse_solver_turn(_valid_submit_json())
        assert isinstance(turn, SolverTurn)
        assert turn.kind == "submit_outcome"
        assert not turn.validate(), f"validation errors: {turn.validate()}"


# ---------------------------------------------------------------------------
# 4. Valid act turn with run_command -> parsed action, .validate()
# ---------------------------------------------------------------------------

class TestSolverAct:
    def test_act_turn_valid(self) -> None:
        turn = parse_solver_turn(_valid_act_json())
        assert isinstance(turn, SolverTurn)
        assert turn.kind == "act"
        assert len(turn.actions) == 1
        action = turn.actions[0]
        assert action.kind == "run_command"
        assert action.arguments["command"] == "python hello.py"
        assert not turn.validate(), f"validation errors: {turn.validate()}"


# ---------------------------------------------------------------------------
# 5. Garbage solver output -> loud parse failure, no fallback turn
# ---------------------------------------------------------------------------

class TestSolverGarbage:
    def test_garbage_raises_and_preserves_raw_output(self) -> None:
        envmap = _make_envmap()
        registry = CapabilityRegistry.from_envmap(envmap)
        compiler = ConfigCompiler(registry)

        ir = RuntimeConfigIR(
            architect_summary="test",
            solver_identity_prompt="test",
            selected_capabilities=("shell", "filesystem"),
        )
        compiled = compiler.compile(ir, envmap)

        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=_stub_model("totally broken output 💥"),
        )
        with pytest.raises(ModelOutputError, match="solver output could not be parsed"):
            hooks.solve([], compiled)

        assert hooks.last_parse_errors
        assert getattr(hooks, "last_raw_solver_output") == "totally broken output 💥"

    def test_valid_turn_preserves_selected_raw_output(self) -> None:
        envmap = _make_envmap()
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(
            RuntimeConfigIR(
                architect_summary="test",
                solver_identity_prompt="test",
                selected_capabilities=("shell", "filesystem"),
            ),
            envmap,
        )
        raw = json.dumps({
            "kind": "act",
            "summary": "inspect current state",
            "actions": [{
                "action_id": "read-1",
                "kind": "read_file",
                "capability_id": "filesystem",
                "arguments": {"path": "config.json"},
            }],
        })
        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=_stub_model(raw),
        )

        hooks.solve([], compiled)

        assert getattr(hooks, "last_raw_solver_output") == raw


# ---------------------------------------------------------------------------
# 6. Verifier hook uses verifier prompt and returns parseable JSON
# ---------------------------------------------------------------------------

class TestVerifierHook:
    def test_verify_uses_verifier_prompt_and_packet(self) -> None:
        envmap = _make_envmap()
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(
            RuntimeConfigIR(
                architect_summary="summary",
                solver_identity_prompt="solver",
                selected_capabilities=("shell", "filesystem"),
                verifier_identity_prompt="Task-specific verifier prompt.",
            ),
            envmap,
        )
        seen: dict[str, Any] = {}

        def verifier_model(messages, *, max_output_tokens=8000):
            seen["messages"] = messages
            seen["max_output_tokens"] = max_output_tokens
            return _valid_verifier_json()

        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=_stub_model(_valid_submit_json()),
            verifier_model=verifier_model,
        )

        raw = hooks.verify({"reason": "max_steps", "task_prompt": "Write hello.py"}, compiled, ledger=type("_L", (), {"all_receipts": lambda self: []})())
        parsed = parse_model_verifier_result(raw)

        assert parsed.verdict == "completed"
        assert seen["max_output_tokens"] == 6000
        # The architect-authored verifier prompt must still reach the verifier
        # verbatim; the runtime now prepends an operational inspection-protocol
        # preamble (so the verifier knows it HAS read-only tools and how to emit
        # inspection requests) instead of leaving it to guess.
        system_prompt = seen["messages"][0]["content"]
        assert system_prompt.endswith("Task-specific verifier prompt.")
        assert "kind\":\"inspect\"" in system_prompt
        assert "blocked_by_tooling" in system_prompt
        assert "verifier_runtime_contract" in seen["messages"][1]["content"]
        assert "verifier_packet" in seen["messages"][1]["content"]

    def test_default_completion_inspection_prioritizes_artifact_and_raw_state(self) -> None:
        packet = {
            "artifacts_present": ["summary.csv"],
            "artifact_evidence": [{"path": "summary.csv"}],
            "latest_file_reads": [
                {
                    "receipt_id": "read-source",
                    "path": "events.log",
                    "excerpt": "today ERROR raw lines...",
                }
            ],
            "solver_authored_evidence": {
                "authority": "audit_trail_only",
                "command_results": [
                    {
                        "command": "python recompute.py",
                        "stdout": "solver recomputation claims summary.csv is correct",
                    }
                ],
            },
        }

        requests = _default_completion_inspection_requests(packet)

        assert [(r.kind, r.path) for r in requests[:2]] == [
            ("read_file", "summary.csv"),
            ("read_file", "events.log"),
        ]
        assert all(r.request_id != "auto-recent-receipts" for r in requests[:2])

    def test_default_completion_inspection_uses_envmap_raw_state_candidates(self) -> None:
        packet = {
            "artifacts_present": ["summary.csv"],
            "artifact_evidence": [{"path": "summary.csv"}],
            "raw_state_candidates": [
                {"path": "events.log", "source": "envmap.likely_inputs", "authority": "candidate_only"}
            ],
            "solver_authored_evidence": {
                "authority": "audit_trail_only",
                "command_results": [{"stdout": "solver recomputation says correct"}],
            },
        }

        requests = _default_completion_inspection_requests(packet)

        assert [(r.kind, r.path) for r in requests[:2]] == [
            ("read_file", "summary.csv"),
            ("read_file", "events.log"),
        ]

    def test_verify_with_inspector_can_request_read_only_probe_then_return_verdict(self) -> None:
        envmap = _make_envmap()
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(
            RuntimeConfigIR(
                architect_summary="summary",
                solver_identity_prompt="solver",
                selected_capabilities=("shell", "filesystem"),
                verifier_identity_prompt="Task-specific verifier prompt.",
            ),
            envmap,
        )
        seen: dict[str, Any] = {"calls": []}

        def verifier_model(messages, *, max_output_tokens=8000):
            seen["calls"].append(messages)
            if len(seen["calls"]) == 1:
                return json.dumps({
                    "kind": "inspect",
                    "summary": "Need to inspect the final artifact.",
                    "requests": [
                        {"request_id": "read-out", "kind": "read_file", "path": "out.txt", "limit": 1}
                    ],
                })
            return _valid_verifier_json(refs=_inspection_ids_from_messages(messages))

        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=_stub_model(_valid_submit_json()),
            verifier_model=verifier_model,
        )

        inspections: list[tuple[Any, ...]] = []

        def inspector(requests):
            inspections.append(requests)
            return [{
                "request_id": "read-out",
                "inspection_id": "inspection:test:read-out",
                "kind": "read_file",
                "path": "out.txt",
                "excerpt": "DONE",
                "evidence_ceiling": "exact_contract",
                "eligible_for_proof": True,
            }]

        raw = hooks.verify_with_inspector(
            {"reason": "solver_submit", "task_prompt": "Write hello.py"},
            compiled,
            ledger=type("_L", (), {"all_receipts": lambda self: []})(),
            inspector=inspector,
        )
        parsed = parse_model_verifier_result(raw)

        assert parsed.verdict == "completed"
        assert len(inspections) == 1
        assert inspections[0][0].kind == "read_file"
        assert inspections[0][0].path == "out.txt"
        assert len(seen["calls"]) == 2
        assert "verifier_inspection_results" in seen["calls"][1][-1]["content"]

    def test_verify_with_inspector_auto_realizes_transcript_missing_evidence_as_read_output(self) -> None:
        envmap = _make_envmap()
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(
            RuntimeConfigIR(
                architect_summary="summary",
                solver_identity_prompt="solver",
                selected_capabilities=("shell", "filesystem"),
                verifier_identity_prompt="Task-specific verifier prompt.",
            ),
            envmap,
        )
        calls: list[list[dict[str, str]]] = []

        def verifier_model(messages, *, max_output_tokens=8000):
            calls.append(messages)
            if len(calls) == 1:
                return json.dumps({
                    "verdict": "uncertain_missing_evidence",
                    "confidence": "0.8",
                    "summary": "Need the actual stdout transcript before I can judge.",
                    "missing_evidence_requests": [
                        "Please surface the actual stdout/stderr or receipt text from the run, including the frame-evidence lines printed by the spot-audit command."
                    ],
                })
            payload = json.loads(messages[-1]["content"])
            inspected = payload.get("verifier_inspection_results", [])
            assert any(row.get("kind") == "read_output" and "SPOT_AUDIT" in row.get("excerpt", "") for row in inspected)
            return json.dumps({
                "verdict": "needs_repair",
                "confidence": "high",
                "summary": "The transcript shows the produced boundary is still wrong.",
                "findings": [{
                    "finding_id": "wrong-boundary",
                    "summary": "The command transcript shows the chosen window does not match the required boundary.",
                    "evidence": ["SPOT_AUDIT transcript disagrees with the claimed completion."],
                    "repair_instruction": "Repair the boundary logic and regenerate the output.",
                    "applies_to": ["output.toml", "jump_analyzer.py"],
                }],
            })

        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=_stub_model(_valid_submit_json()),
            verifier_model=verifier_model,
        )
        inspections: list[tuple[Any, ...]] = []

        def inspector(requests):
            inspections.append(requests)
            results = []
            for request in requests:
                if request.kind == "read_output":
                    results.append({
                        "request_id": request.request_id,
                        "kind": request.kind,
                        "handle": request.handle,
                        "excerpt": "OUTPUT_TOML\\n...\\nSPOT_AUDIT frame 104 score=10.9",
                    })
            return results

        packet = {
            "reason": "solver_submit",
            "task_prompt": "Analyze jump video.",
            "state_inspection_handles": [
                {"kind": "output", "handle": "5:a-1:stdout", "stream": "stdout", "bytes": 860},
                {"kind": "output", "handle": "5:a-1:stderr", "stream": "stderr", "bytes": 0},
            ],
        }

        raw = hooks.verify_with_inspector(
            packet,
            compiled,
            ledger=type("_L", (), {"all_receipts": lambda self: []})(),
            inspector=inspector,
        )
        parsed = parse_model_verifier_result(raw)

        assert parsed.verdict == "needs_repair"
        assert len(inspections) == 1
        assert [r.kind for r in inspections[0]] == ["read_output", "read_output"]
        assert [r.handle for r in inspections[0]] == ["5:a-1:stdout", "5:a-1:stderr"]

    def test_auto_inspection_exposes_raw_state_before_accepting_solver_recomputation(self) -> None:
        envmap = _make_envmap()
        compiled = ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(
            RuntimeConfigIR(
                architect_summary="summary",
                solver_identity_prompt="solver",
                selected_capabilities=("shell", "filesystem"),
                verifier_identity_prompt="Task-specific verifier prompt.",
            ),
            envmap,
        )
        calls: list[list[dict[str, str]]] = []

        def verifier_model(messages, *, max_output_tokens=8000):
            calls.append(messages)
            if len(calls) == 1:
                return _valid_verifier_json()
            payload = json.loads(messages[-1]["content"])
            inspected_paths = {
                row.get("path")
                for row in payload.get("verifier_inspection_results", [])
                if row.get("kind") == "read_file"
            }
            assert {"summary.csv", "events.log"}.issubset(inspected_paths)
            return json.dumps({
                "verdict": "needs_repair",
                "confidence": "high",
                "summary": "Raw source inspection contradicts the solver-authored recomputation.",
                "findings": [{
                    "finding_id": "raw-source-mismatch",
                    "summary": "summary.csv does not match events.log.",
                    "evidence": ["events.log raw count differs from solver recomputation"],
                    "repair_instruction": "Recompute from the raw source file and update summary.csv.",
                    "applies_to": ["summary.csv", "events.log"],
                }],
            })

        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=_stub_model(_valid_submit_json()),
            verifier_model=verifier_model,
        )
        packet = {
            "reason": "solver_submit",
            "task_prompt": "Summarize event log counts.",
            "artifacts_present": ["summary.csv"],
            "artifact_evidence": [{"path": "summary.csv"}],
            "latest_file_reads": [{"path": "events.log", "excerpt": "raw source"}],
            "solver_authored_evidence": {
                "authority": "audit_trail_only",
                "command_results": [{"stdout": "solver recomputation says correct"}],
            },
        }
        inspections: list[tuple[Any, ...]] = []

        def inspector(requests):
            inspections.append(requests)
            return [
                {
                    "request_id": request.request_id,
                    "kind": request.kind,
                    "path": request.path,
                    "excerpt": "raw ERROR count=370" if request.path == "events.log" else "today,ERROR,414",
                }
                for request in requests
                if request.kind == "read_file"
            ]

        raw = hooks.verify_with_inspector(
            packet,
            compiled,
            ledger=type("_L", (), {"all_receipts": lambda self: []})(),
            inspector=inspector,
        )
        parsed = parse_model_verifier_result(raw)

        assert parsed.verdict == "needs_repair"
        assert len(inspections) == 1
        assert [(r.kind, r.path) for r in inspections[0][:2]] == [
            ("read_file", "summary.csv"),
            ("read_file", "events.log"),
        ]


# ---------------------------------------------------------------------------
# 7. _extract_json_object handles fences + leading prose
# ---------------------------------------------------------------------------

class TestExtractJsonObject:
    def test_plain_json(self) -> None:
        result = _extract_json_object('{"a": 1}')
        assert json.loads(result) == {"a": 1}

    def test_fenced_json(self) -> None:
        text = 'Here is the config:\n```json\n{"mode": "direct_build"}\n```\nDone.'
        result = _extract_json_object(text)
        assert json.loads(result) == {"mode": "direct_build"}

    def test_leading_prose(self) -> None:
        text = "Sure! I'll configure this.\n\n{\"key\": \"value\"}"
        result = _extract_json_object(text)
        assert json.loads(result) == {"key": "value"}

    def test_nested_braces(self) -> None:
        text = '{"outer": {"inner": 1}}'
        result = _extract_json_object(text)
        assert json.loads(result) == {"outer": {"inner": 1}}

    def test_no_json_raises(self) -> None:
        with pytest.raises(ModelOutputError):
            _extract_json_object("no json here")

    def test_unbalanced_raises(self) -> None:
        with pytest.raises(ModelOutputError):
            _extract_json_object('{"missing closing')


# ---------------------------------------------------------------------------
# 8. End-to-end with stubs: kernel.run() reaches completed
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_kernel_reaches_completed(self) -> None:
        envmap = EnvMap(
            task_prompt="Write hello.txt with 'hello world'.",
            workspace_root="/app",
            visible_files=(),
            capabilities={
                "shell": CapabilityDescriptor(
                    capability_id="shell", summary="run commands"
                ),
                "filesystem": CapabilityDescriptor(
                    capability_id="filesystem", summary="read/write files"
                ),
            },
            grader_hints={
                "required_artifacts": ["hello.txt"],
            },
        )

        executor = MemoryExecutor(workspace_root="/app")

        # Architect stub
        arch_response = _valid_architect_json()

        # Solver stub: turn 1 = write the file, turn 2 = submit
        call_count = {"n": 0}

        def solver_model(
            messages: list[dict[str, str]],
            *,
            max_output_tokens: int = 8000,
        ) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return json.dumps({
                    "kind": "act",
                    "summary": "Write the required file.",
                    "actions": [
                        {
                            "action_id": "write-1",
                            "kind": "write_file",
                            "capability_id": "filesystem",
                            "arguments": {
                                "path": "hello.txt",
                                "content": "hello world",
                            },
                            "intent": "create required artifact",
                            "expected_observation": "file written",
                            "if_fail_next": "retry write",
                        }
                    ],
                })
            return _valid_submit_json()

        # Verifier stub: a well-behaved verifier (matching the new runtime
        # requirement) inspects the actual deliverable before it may return
        # completed -- round 1 requests an inspection, round 2 (after seeing
        # the inspection result) returns completed. A verifier that just
        # repeats "completed" without ever inspecting is exactly the
        # false-clean failure mode the read-only inspector requirement exists
        # to close, so it is correctly rejected rather than trusted here.
        verifier_call_count = {"n": 0}

        def verifier_model(
            messages: list[dict[str, str]],
            *,
            max_output_tokens: int = 8000,
        ) -> str:
            verifier_call_count["n"] += 1
            if verifier_call_count["n"] == 1:
                return json.dumps({
                    "kind": "inspect",
                    "requests": [{"request_id": "r1", "kind": "read_file", "path": "hello.txt"}],
                })
            return _valid_verifier_json(refs=_inspection_ids_from_messages(messages))

        hooks = ModelHooks(
            architect_model=_stub_model(arch_response),
            solver_model=solver_model,
            verifier_model=verifier_model,
        )

        kernel = AetherNextKernel(max_steps=10, workbench_architect=_canonical_workbench_architect_config())
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "completed", (
            f"expected completed, got {result.status} "
            f"blockers={result.blockers} step={result.step}"
        )
        # The file must actually exist in the executor
        assert executor.exists("hello.txt")
        assert executor.read_file("hello.txt") == "hello world"

    def test_verifier_completed_without_any_inspection_gets_auto_inspected_before_acceptance(self) -> None:
        """Regression for the raman-fitting false-clean on the real 10-task VM
        batch: the solver wrote a well-shaped results.json (right keys, right
        file), deterministic checks passed, and the verifier returned
        "completed" on round 1 having inspected nothing -- the grader then
        failed 2 of 3 assertions because the *content* was wrong. A verifier
        that never independently inspects the actual deliverable before
        saying completed must not be trusted; the runtime must reject the
        verifier protocol failure rather than accept an uninspected completion
        or fabricate a semantic verifier verdict on the verifier's behalf.
        """
        envmap = EnvMap(
            task_prompt="Write results.json with the fit results.",
            workspace_root="/app",
            visible_files=(),
            capabilities={
                "shell": CapabilityDescriptor(capability_id="shell", summary="run commands"),
                "filesystem": CapabilityDescriptor(capability_id="filesystem", summary="read/write files"),
            },
        )
        executor = MemoryExecutor(workspace_root="/app")
        call_count = {"n": 0}
        verifier_calls = {"n": 0}

        def solver_model(messages: list[dict[str, str]], *, max_output_tokens: int = 8000) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return json.dumps({
                    "kind": "act",
                    "summary": "Write results.json.",
                    "actions": [{
                        "action_id": "write-1", "kind": "write_file", "capability_id": "filesystem",
                        "arguments": {"path": "results.json", "content": '{"G_Peak": 999, "D_Peak": 999}'},
                        "intent": "write fit results", "expected_observation": "file written",
                        "if_fail_next": "retry",
                    }],
                })
            return _valid_submit_json()

        # This verifier never REQUESTS inspection -- its first verdict is an
        # uninspected completed, exactly the failure mode observed on the real
        # VM batch. The runtime then auto-inspects; the second verdict must
        # cite that auto-inspection per the completion-evidence protocol.
        def never_inspects_verifier(messages: list[dict[str, str]], *, max_output_tokens: int = 8000) -> str:
            verifier_calls["n"] += 1
            if verifier_calls["n"] == 1:
                return _valid_verifier_json()
            return _valid_verifier_json(refs=_inspection_ids_from_messages(messages))

        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=solver_model,
            verifier_model=never_inspects_verifier,
        )

        kernel = AetherNextKernel(max_steps=10, workbench_architect=_canonical_workbench_architect_config())
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "completed"
        assert verifier_calls["n"] == 2
        assert any(r.kind == "model_verifier_inspection" for r in result.receipts)
        verifier_receipts = [r for r in result.receipts if r.kind == "model_verifier_result"]
        assert verifier_receipts
        assert verifier_receipts[-1].payload["parsed_verifier_result"]["verdict"] == "completed"

    def test_repeated_submit_without_intervening_evidence_skips_verifier(self) -> None:
        envmap = EnvMap(
            task_prompt="Write results.json with the fit results.",
            workspace_root="/app",
            visible_files=(),
            capabilities={
                "shell": CapabilityDescriptor(capability_id="shell", summary="run commands"),
                "filesystem": CapabilityDescriptor(capability_id="filesystem", summary="read/write files"),
            },
        )
        executor = MemoryExecutor(workspace_root="/app")
        call_count = {"n": 0}
        verifier_calls = {"n": 0}

        def solver_model(messages: list[dict[str, str]], *, max_output_tokens: int = 8000) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return json.dumps({
                    "kind": "act",
                    "summary": "Write results.json.",
                    "actions": [{
                        "action_id": "write-1", "kind": "write_file", "capability_id": "filesystem",
                        "arguments": {"path": "results.json", "content": '{"G_Peak": 999, "D_Peak": 999}'},
                        "intent": "write fit results", "expected_observation": "file written",
                        "if_fail_next": "retry",
                    }],
                })
            return _valid_submit_json()

        def verifier_model(messages: list[dict[str, str]], *, max_output_tokens: int = 8000) -> str:
            verifier_calls["n"] += 1
            return json.dumps({
                "verdict": "uncertain_missing_evidence",
                "confidence": "high",
                "summary": "Need independent evidence before completion.",
                "missing_evidence_requests": ["Read or validate results.json before resubmitting."],
                "findings": [{
                    "finding_id": "vf-need-evidence",
                    "verdict": "uncertain_missing_evidence",
                    "priority": "blocking",
                    "summary": "No independent evidence was provided.",
                    "evidence": ["submit packet lacked independent read/check evidence"],
                    "repair_instruction": "Inspect or validate results.json before resubmitting.",
                    "applies_to": ["results.json"],
                }],
            })

        hooks = ModelHooks(
            architect_model=_stub_model(_valid_architect_json()),
            solver_model=solver_model,
            verifier_model=verifier_model,
        )

        kernel = AetherNextKernel(max_steps=10, workbench_architect=_canonical_workbench_architect_config())
        result = kernel.run(envmap, executor, hooks)

        assert result.status == "solver_submit_stalemate"
        assert result.step < 10
        # Round 1 may cost two model calls (judge -> auto-inspect named files
        # -> re-judge); repeated submits after that never re-invoke the model.
        assert verifier_calls["n"] <= 2
        skipped = [r for r in result.receipts if r.kind == "model_verifier_skipped"]
        assert any(
            r.payload.get("reason") == "active_findings_without_intervening_evidence"
            for r in skipped
        )
        stalemate = [r for r in result.receipts if r.kind == "solver_submit_stalemate"]
        assert stalemate
        assert stalemate[-1].payload["finding_ids"] == ("vf-need-evidence",)
