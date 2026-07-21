from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aether_next import TaskClause, TaskContract, WorldState


@pytest.fixture
def contract() -> TaskContract:
    return TaskContract.create(
        "Write /app/out.txt with the exact value alpha and leave the required runtime state healthy.",
        (
            TaskClause("c_file", "The output file exists at /app/out.txt.", ("/app/out.txt",)),
            TaskClause("c_value", "The output content is exactly alpha.", ("alpha",)),
        ),
    )


@pytest.fixture
def world(contract: TaskContract) -> WorldState:
    return WorldState(
        task_contract=contract,
        env_facts={"python": {"available": True, "version": "3.13"}, "workspace": "/app"},
        files={
            "/app/input.txt": "source-data\n" * 50,
            "/app/out.txt": "alpha",
            "/app/large.log": "HEAD\n" + "noise\n" * 2000 + "TARGET failure line\nTAIL\n",
        },
        artifacts={"/app/frame.png": {"type": "image", "width": 640, "height": 480}},
        services={"web": {"state": "ready", "host": "127.0.0.1", "port": 8080}},
        jobs={"trainer": {"state": "running", "progress": 0.42, "heartbeat": 7}},
        active_findings=[{"finding_id": "f1", "clause_id": "c_value", "summary": "recheck exact content"}],
        latest_result={"action_id": "a0", "stdout": "alpha", "exit_code": 0},
        named_sections={"plan": {"next": "inspect then submit"}},
    )


def base_config(*, mode: str = "interactive", selectors: list[dict] | None = None) -> dict:
    if selectors is None:
        selectors = [
            {"kind": "task_contract", "representation": "full", "required": True},
            {"kind": "file", "target": "/app/out.txt", "representation": "full", "required": True},
            {"kind": "latest_result", "representation": "structured_summary", "required": False},
            {"kind": "active_findings", "representation": "full", "required": False},
        ]
    readiness: list[str] = []
    if mode == "service":
        readiness = ["wait_for_port", "probe_http"]
    elif mode == "batch_job":
        readiness = ["wait_for_process_state", "wait_for_log_pattern"]
    return {
        "schema_version": "workbench_config.v4",
        "task_understanding": "Produce the exact required output and verify the observable current state.",
        "success_definition": "Every immutable clause is directly satisfied and independently verifiable.",
        "clause_coverage": [
            {
                "clause_id": "c_file",
                "solver_handling": "write and read the required path",
                "verifier_check": "read the exact path",
            },
            {
                "clause_id": "c_value",
                "solver_handling": "derive and write the exact value",
                "verifier_check": "compare the current bytes to the immutable value",
            },
        ],
        "solver_system_prompt": "Inspect decisive state, implement once, self-check every clause, then submit.",
        "verifier_system_prompt": "Judge only immutable clauses against observable frozen state; ignore journey narrative.",
        "verifier_strategy": {
            "clause_checks": [
                {
                    "clause_id": "c_file",
                    "inspection_route": "read_file:/app/out.txt",
                    "fallback_route": "inspect_artifact:/app/out.txt",
                    "falsification_check": "fail if the exact path is absent",
                    "required_evidence_class": "exact_contract",
                },
                {
                    "clause_id": "c_value",
                    "inspection_route": "read_file:/app/out.txt",
                    "fallback_route": "run_command:python-byte-compare",
                    "falsification_check": "fail if bytes differ from alpha",
                    "required_evidence_class": "independent_semantic",
                },
            ],
            "false_positive_traps": [
                "file existence without correct content",
                "solver-authored self-test agreeing with its own mistake",
            ],
            "return_all_findings": True,
        },
        "context_policy": {
            "selectors": selectors,
            "max_events_before_compaction": 6,
            "max_dynamic_bytes": 12000,
        },
        "memory_policy": {
            "repeat_mode": "soft_block_exact_repeat",
            "require_query_before_repeat": True,
            "require_query_before_overwrite": True,
            "index_by": ["path", "action_kind", "failure_kind"],
        },
        "process_policy": {
            "mode": mode,
            "readiness": readiness,
            "heartbeat_interval_s": 0.1,
            "allow_equivalent_overlap": False,
        },
        "resource_policy": {
            "max_steps": 40 if mode != "batch_job" else 120,
            "total_timeout_s": 600,
            "command_timeout_s": 60,
            "verifier_timeout_s": 120,
        },
        "reconfigure_policy": {
            "enabled": True,
            "max_versions": 2,
            "allowed_owners": ["harness_config"],
        },
        "local_verification_limits": ["official grader remains external"],
    }


@pytest.fixture
def config_factory():
    def factory(*, mode: str = "interactive", selectors: list[dict] | None = None, mutate=None):
        raw = base_config(mode=mode, selectors=selectors)
        if mutate:
            mutate(raw)
        return deepcopy(raw)
    return factory
