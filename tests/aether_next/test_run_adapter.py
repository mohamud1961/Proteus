"""End-to-end test for run_adapter with stub models against a real temp workspace."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from proteus.aether_next.run_adapter import (
    ensure_certified_architect_mode,
    run_task,
    workbench_architect_for,
)


class _StubArchitectModel:
    """Architect: selects shell + filesystem, disables checks and progress requirement."""

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        return json.dumps({
            "architect_summary": "test architect: direct_build with shell+filesystem",
            "solver_identity_prompt": "You are a test solver.",
            "selected_capabilities": ["shell", "filesystem"],
            "workflow_policy": {"mode": "direct_build"},
            "process_policy": {"mode": "stateless_shell"},
            "completion_policy": {
                "require_authoritative_check": False,
                "allow_evidence_fallback": True,
                "require_all_obligations": False,
                "require_recent_progress": False,
                "require_clean_integrity": False,
            },
        })


class _StubWorkbenchArchitectModel:
    """Workbench architect: configures filesystem-only tools."""

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        return json.dumps({
            "schema_version": "harness_config.v1",
            "task_understanding": "Create out.txt.",
            "success_definition": "out.txt exists.",
            "solver_system_prompt": {
                "role": "Workbench run adapter solver",
                "workflow": ["write out.txt", "submit"],
                "self_verification": ["read or check visible output before submitting"],
                "memory_use": ["query_memory before repeating work"],
            },
            "verifier_system_prompt": {
                "role": "Read-only verifier for out.txt",
                "success_criteria": ["out.txt exists and satisfies the task request"],
                "required_evidence": ["current file state confirms completion"],
                "false_positive_traps": ["submitting before checking the file"],
                "verdict_guidance": ["completed requires current evidence"],
                "feedback_guidance": ["name the missing file or evidence"],
            },
            "evidence_requirements": ["out.txt exists in the current workspace"],
            "false_positive_risks": ["out.txt may exist with irrelevant content"],
            "minimum_completion_evidence": ["current out.txt file evidence"],
            "tool_policy": {"enabled_tools": ["read_file", "write_file", "query_memory"]},
            "context_policy": {"mode": "retrieval_augmented"},
            "model_verifier_policy": {"enabled": True},
        })


class _StubSolverModel:
    """Scripted solver: step 0 writes out.txt + runs check, step 1 submits outcome."""

    def __init__(self) -> None:
        self._call_count = 0

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        self._call_count += 1
        if self._call_count == 1:
            return json.dumps({
                "kind": "act",
                "summary": "Write out.txt and verify it exists",
                "actions": [
                    {
                        "action_id": "write-out",
                        "kind": "write_file",
                        "capability_id": "filesystem",
                        "arguments": {
                            "path": "out.txt",
                            "content": "hello from solver",
                        },
                        "intent": "create the required output file",
                        "expected_observation": "file written",
                        "if_fail_next": "retry write",
                    },
                    {
                        "action_id": "check-out",
                        "kind": "run_command",
                        "capability_id": "shell",
                        "arguments": {"command": "test -f out.txt"},
                        "intent": "verify out.txt exists",
                        "expected_observation": "exit 0",
                        "if_fail_next": "rewrite file",
                    },
                ],
            })
        # All subsequent calls: submit outcome.
        return json.dumps({
            "kind": "submit_outcome",
            "summary": "out.txt written and verified",
        })


class _WorkbenchSolverModel:
    """Scripted solver that only uses workbench-enabled filesystem tools."""

    def __init__(self) -> None:
        self._call_count = 0

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        self._call_count += 1
        if self._call_count == 1:
            return json.dumps({
                "kind": "act",
                "summary": "Write out.txt",
                "actions": [
                    {
                        "action_id": "write-out",
                        "kind": "write_file",
                        "capability_id": "filesystem",
                        "arguments": {"path": "out.txt", "content": "hello from workbench"},
                        "intent": "create the required output file",
                        "expected_observation": "file written",
                        "if_fail_next": "retry write",
                    }
                ],
            })
        return json.dumps({
            "kind": "submit_outcome",
            "summary": "out.txt written",
        })


class _StubVerifierModel:
    """Simulates a well-behaved verifier: it inspects the actual workspace at
    least once before returning completed, matching the runtime's requirement
    that a completed verdict be backed by real independent inspection rather
    than accepted straight from the packet's narrative. A fresh verifier
    invocation always starts with exactly [system, user] messages; anything
    longer means an inspection round already happened in this conversation --
    checking message count (not a persistent counter) correctly re-arms this
    per verifier call, since a single run can invoke the verifier several
    times across different steps."""

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 8000,
    ) -> str:
        if len(messages) <= 2:
            return json.dumps({
                "kind": "inspect",
                "requests": [{"request_id": "r1", "kind": "read_file", "path": "out.txt"}],
            })
        inspection_ids: list[str] = []
        for message in reversed(messages):
            try:
                payload = json.loads(message.get("content", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            rows = payload.get("verifier_inspection_results") if isinstance(payload, dict) else None
            if isinstance(rows, list):
                inspection_ids = [
                    str(row.get("inspection_id", "")).strip()
                    for row in rows
                    if isinstance(row, dict) and str(row.get("inspection_id", "")).strip()
                ]
                if inspection_ids:
                    break
        return json.dumps({
            "verdict": "completed",
            "confidence": "high",
            "summary": "stub verifier saw enough evidence",
            "completion_evidence": [{
                "requirement": "out.txt contains the requested result",
                "observed": "read_file inspection of out.txt showed the requested content",
                "inspection_refs": inspection_ids,
                "falsification_check": "differing file content would have contradicted completion",
            }],
        })


class TestRunAdapter:
    def test_workbench_is_the_only_certified_architect_mode(self) -> None:
        ensure_certified_architect_mode("workbench")
        for mode in ("ir", "contract", "bogus"):
            with pytest.raises(ValueError, match="quarantined in reference_legacy"):
                ensure_certified_architect_mode(mode)

    def test_workbench_architect_for_builds_workbench_architect(self) -> None:
        architect = workbench_architect_for(_StubWorkbenchArchitectModel())
        assert type(architect).__name__ == "WorkbenchArchitect"

    def test_end_to_end_stub(self) -> None:
        with tempfile.TemporaryDirectory() as task_dir, \
             tempfile.TemporaryDirectory() as workspace:
            # Create a minimal task file so envmap has something to scan.
            with open(os.path.join(task_dir, "README.md"), "w") as fh:
                fh.write("Test task")

            architect = _StubWorkbenchArchitectModel()
            solver = _WorkbenchSolverModel()

            record = run_task(
                task_dir=task_dir,
                instruction_text="Create out.txt containing any text.",
                architect_model=architect,
                solver_model=solver,
                verifier_model=_StubVerifierModel(),
                workspace_root=workspace,
                max_steps=6,
            )

            # Verify the run record shape.
            assert "status" in record
            assert "step" in record
            assert "classifier_label" in record
            assert "classifier_confidence" in record
            assert "classifier_evidence" in record
            assert "classifier_detail" in record
            assert "receipt_summary" in record
            assert isinstance(record["receipt_summary"], list)
            assert record["architect_mode"] == "workbench"

            # The run should complete successfully.
            assert record["status"] == "completed", (
                f"Expected completed but got {record['status']}; "
                f"receipts: {json.dumps(record['receipt_summary'], indent=2)}"
            )
            assert record["classifier_label"] == "none"
            realization = [
                r for r in record["receipt_summary"]
                if r["kind"] == "config_realization"
            ]
            assert realization

            # The file should actually exist in the workspace.
            assert os.path.isfile(os.path.join(workspace, "out.txt"))

    def test_workbench_mode_runs_through_offline_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as task_dir, \
             tempfile.TemporaryDirectory() as workspace:
            with open(os.path.join(task_dir, "README.md"), "w") as fh:
                fh.write("Test task")

            record = run_task(
                task_dir=task_dir,
                instruction_text="Create out.txt containing any text.",
                architect_model=_StubWorkbenchArchitectModel(),
                solver_model=_WorkbenchSolverModel(),
                verifier_model=_StubVerifierModel(),
                workspace_root=workspace,
                max_steps=3,
                architect_mode="workbench",
            )

            assert record["status"] in {"completed", "incomplete"}
            assert os.path.isfile(os.path.join(workspace, "out.txt"))
            realization = [
                r for r in record["receipt_summary"]
                if r["kind"] == "config_realization"
            ]
            assert realization

    def test_receipt_summary_structure(self) -> None:
        with tempfile.TemporaryDirectory() as task_dir, \
             tempfile.TemporaryDirectory() as workspace:
            with open(os.path.join(task_dir, "prompt.txt"), "w") as fh:
                fh.write("Prompt text")

            architect = _StubWorkbenchArchitectModel()
            solver = _WorkbenchSolverModel()

            record = run_task(
                task_dir=task_dir,
                instruction_text="Create out.txt.",
                architect_model=architect,
                solver_model=solver,
                verifier_model=_StubVerifierModel(),
                workspace_root=workspace,
                max_steps=6,
            )

            for entry in record["receipt_summary"]:
                assert "receipt_id" in entry
                assert "kind" in entry
                assert "success" in entry
                assert "failure_class" in entry
                assert "summary" in entry
