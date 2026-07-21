from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from aether_next.classifier import HarnessLimiterClassifier
from aether_next.envmap_builder import build_envmap_from_task
from aether_next.execution import CommandResult, MemoryExecutor
from aether_next.kernel import AetherNextKernel, KernelResult
from aether_next.ledger import Receipt
from aether_next.runtime_ir import (
    ActionRequest,
    BootstrapPolicy,
    CapabilityDescriptor,
    CompletionPolicy,
    ContextPolicy,
    EnvMap,
    HelperToolPolicy,
    RuntimeConfigIR,
    SolverTurn,
)
from aether_next.verifier import parse_model_verifier_result


_BUILD_ROOT = Path(__file__).resolve().parents[1]


def _action(kind: str, args: dict, *, action_id: str = "a", cap: str = "shell") -> ActionRequest:
    return ActionRequest(
        action_id=action_id,
        kind=kind,
        capability_id=cap,
        arguments=args,
        intent="diagnostic",
        expected_observation="diagnostic",
        if_fail_next="diagnostic",
    )


def _runtime() -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="local vision delta",
        solver_identity_prompt="Use generic tools and state evidence.",
        selected_capabilities=("shell", "filesystem"),
        context_policy=ContextPolicy(mode="retrieval_augmented"),
        completion_policy=CompletionPolicy(require_all_obligations=False, require_recent_progress=False),
        bootstrap_policy=BootstrapPolicy(allow_acquisition=False),
        helper_tool_policy=HelperToolPolicy(allow_creation=False),
    )


def _env(task_metadata: dict | None = None) -> EnvMap:
    return EnvMap(
        task_prompt="Build the project.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
        },
        task_metadata=task_metadata or {},
    )


class _Hooks:
    def __init__(self, turns: list[SolverTurn]) -> None:
        self.turns = list(turns)

    def architect(self, request):
        return _runtime()

    def solve(self, messages, compiled):
        if self.turns:
            return self.turns.pop(0)
        return SolverTurn(kind="submit_outcome", summary="done")


def test_verifier_parser_accepts_fenced_or_prose_wrapped_json() -> None:
    raw = """Here is my verdict:\n```json\n{"verdict":"completed","summary":"state inspected"}\n```\n"""
    assert parse_model_verifier_result(raw).verdict == "completed"
    prose = 'Verifier result: {"verdict":"uncertain_missing_evidence","missing_evidence_requests":["read output"]}'
    assert parse_model_verifier_result(prose).verdict == "uncertain_missing_evidence"


def test_model_limit_disqualified_by_protocol_or_context_failures() -> None:
    classifier = HarnessLimiterClassifier()
    clean_failed_check = Receipt("check", 1, "check_result", False, "bad", state_change=False, failure_class="test_failure")
    write = Receipt("write", 0, "write_file", True, "wrote", state_change=True)
    result = KernelResult(status="incomplete", step=2, reconfigurations=0, receipts=(write, clean_failed_check))
    assert classifier.classify(result).label == "model_limit"

    parse_error = Receipt("parse", 0, "solver_parse_error", False, "bad json", failure_class="solver_protocol_error")
    result_with_harness_fault = KernelResult(status="incomplete", step=2, reconfigurations=0, receipts=(parse_error, write, clean_failed_check))
    assert classifier.classify(result_with_harness_fault).label != "model_limit"


def test_run_command_timeout_argument_is_bounded_and_recorded() -> None:
    action = _action("run_command", {"command": "make all", "timeout_s": 1200}, action_id="build")
    turn = SolverTurn(kind="act", summary="build", actions=(action,))
    executor = MemoryExecutor(workspace_root="/app")
    executor.register_command("make all", lambda _ex, cmd: CommandResult(cmd, 0, stdout="ok"))
    metadata = {"resource_budget": {"agent_timeout_sec": 1800, "verifier_timeout_sec": 1800}}

    result = AetherNextKernel(max_steps=1).run(_env(metadata), executor, _Hooks([turn]))

    receipt = next(r for r in result.receipts if r.kind == "run_command")
    assert receipt.payload["timeout_s"] == 1200
    assert "requested=1200" in receipt.payload["timeout_policy"]


def test_envmap_ingests_public_task_metadata_as_hints_not_facts(tmp_path: Path) -> None:
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    task_toml = {
        "metadata": {"category": "video-processing", "difficulty": "hard", "tags": ["video-processing"]},
        "agent": {"timeout_sec": 3600},
        "verifier": {"timeout_sec": 3600},
        "environment": {"build_timeout_sec": 600, "docker_image": "example/video", "memory": "2G"},
    }
    envmap = build_envmap_from_task(str(tmp_path), "Process the input video with ffmpeg.", task_toml=task_toml)

    assert envmap.task_metadata["internal_task_metadata"]["category"] == "video-processing"
    assert envmap.task_metadata["resource_budget"]["agent_timeout_sec"] == 3600
    assert "docker_image" not in envmap.task_metadata["model_facing_resource_budget"]
    assert "capability_requirements" not in envmap.task_metadata
    assert "task_capability_requirements" not in envmap.task_metadata
    assert envmap.task_metadata["env_fact_policy"]["semantic_task_classification_present"] is False
    assert envmap.task_metadata["model_facing_resource_budget"]["agent_timeout_sec"] == 3600
    assert "required_tool_hints" not in envmap.task_metadata


def test_official_capability_audit_script_ignores_solution_and_tests(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    task = root / "generic-video"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "solution").mkdir()
    (task / "instruction.md").write_text("Process a video and produce an output file.", encoding="utf-8")
    (task / "task.toml").write_text('''version="1.0"\n[metadata]\ncategory="video-processing"\ntags=["video-processing"]\ndifficulty="hard"\n[agent]\ntimeout_sec=3600\n[verifier]\ntimeout_sec=3600\n[environment]\ndocker_image="x"\nbuild_timeout_sec=600\n''', encoding="utf-8")
    (task / "environment" / "sample.mp4").write_text("fake", encoding="utf-8")
    (task / "tests" / "hidden.py").write_text("SHOULD_NOT_APPEAR", encoding="utf-8")
    (task / "solution" / "solve.sh").write_text("SHOULD_NOT_APPEAR", encoding="utf-8")
    csv_path = tmp_path / "audit.csv"
    md_path = tmp_path / "audit.md"
    subprocess.check_call([
        sys.executable,
        str(_BUILD_ROOT / "scripts" / "audit_official_task_capabilities.py"),
        str(root),
        "--csv", str(csv_path),
        "--md", str(md_path),
    ])
    csv_text = csv_path.read_text(encoding="utf-8")
    assert "video_processing" in csv_text
    assert "SHOULD_NOT_APPEAR" not in csv_text
