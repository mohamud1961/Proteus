"""Deterministic integration scenarios for the configurable harness.

These scenarios are intentionally model-free and Docker-free. They exercise the
real kernel path with a static WorkbenchArchitect, scripted solver turns, the
compiler-realized tool/context/check policy, memory tools, and verifier gating.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .analysis import _check_id
from .compiler import CapabilityRegistry, ConfigCompiler
from .execution import CommandResult, MemoryExecutor
from .kernel import AetherNextKernel, KernelResult
from .kernel_config import resolve_runtime
from .inspection_registry import register_inspection_results
from .runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    CompletionPolicy,
    ContextPolicy,
    ContextRecipe,
    ContextRecipeRecent,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)
from .verifier import CompletionEvidenceEntry, ModelVerifierResult, VerifierFinding
from .verifier_inspector import VerifierInspectionRequest
from .workbench_config import HarnessConfigIR, parse_harness_config_ir


@dataclass(frozen=True)
class IntegrationScenarioResult:
    scenario_id: str
    status: str
    final_files: dict[str, str]
    receipts: list[dict[str, Any]]
    context_packets: list[dict[str, Any]]
    verifier_packets: list[dict[str, Any]]
    checks: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status,
            "final_files": dict(self.final_files),
            "receipts": list(self.receipts),
            "context_packets": list(self.context_packets),
            "verifier_packets": list(self.verifier_packets),
            "checks": dict(self.checks),
        }


class StaticWorkbenchArchitect:
    def __init__(self, config: HarnessConfigIR) -> None:
        self.config = config
        self.requests: list[dict[str, Any]] = []

    def configure(self, request: dict[str, Any]) -> tuple[HarnessConfigIR, tuple[str, ...]]:
        self.requests.append(dict(request))
        return self.config, ()


class ScriptedHooks:
    def __init__(
        self,
        *,
        fallback_ir: RuntimeConfigIR,
        turns: Iterable[SolverTurn],
        verifier_outputs: Iterable[Any],
        executor: MemoryExecutor | None = None,
    ) -> None:
        self.fallback_ir = fallback_ir
        self.executor = executor
        self.turns = list(turns)
        self.verifier_outputs = list(verifier_outputs)
        self.context_packets: list[dict[str, Any]] = []
        self.verifier_packets: list[dict[str, Any]] = []

    def architect(self, request: dict[str, Any]) -> RuntimeConfigIR:
        return self.fallback_ir

    def reconfigure(self, request: dict[str, Any], compiled: Any, ledger: Any) -> RuntimeConfigIR:
        return self.fallback_ir

    def solve(self, messages: list[dict[str, str]], compiled: Any) -> SolverTurn:
        self.context_packets.append(_extract_context_packet(messages))
        if self.turns:
            return self.turns.pop(0)
        return SolverTurn(kind="submit_outcome", summary="no more scripted turns")

    def verify(self, packet: dict[str, Any], compiled: Any, ledger: Any) -> Any:
        self.verifier_packets.append(packet)
        output: Any = (
            self.verifier_outputs.pop(0)
            if self.verifier_outputs
            else {"verdict": "completed", "confidence": "medium", "summary": "No blocking findings in packet evidence."}
        )
        if (
            isinstance(output, dict)
            and output.get("verdict") == "completed"
            and self.executor is not None
            and ledger.findings.active
        ):
            finding = next(iter(ledger.findings.active.values()))
            path = next((target for target in finding.applies_to if self.executor.exists(target)), "out.txt")
            content = self.executor.read_file(path)
            request = VerifierInspectionRequest(
                request_id=f"repair-read-{len(self.verifier_packets)}",
                kind="read_file",
                path=path,
            )
            rows = register_inspection_results(
                (request,),
                ({
                    "request_id": request.request_id,
                    "kind": "read_file",
                    "path": path,
                    "excerpt": content,
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "read_only": True,
                },),
                ledger=ledger,
                step=max((receipt.step for receipt in ledger.all_receipts()), default=0),
                requester="scripted_verifier",
                executor=self.executor,
                overlay=None,
                packet_signature="scripted-current-state",
            )
            output = dict(output)
            output["completion_evidence"] = [{
                "requirement": finding.summary,
                "observed": f"current {path} contains {content.strip()}",
                "inspection_refs": [rows[0]["inspection_id"]],
                "falsification_check": "different current content would preserve the finding",
            }]
        return output


class _ExistsHooks:
    def architect(self, request: dict[str, Any]) -> RuntimeConfigIR:
        del request
        return RuntimeConfigIR(
            architect_summary="fallback",
            solver_identity_prompt="fallback solver",
            selected_capabilities=("filesystem",),
            completion_policy=CompletionPolicy(require_authoritative_check=False, require_all_obligations=False),
        )


def run_workbench_verifier_repair_scenario() -> IntegrationScenarioResult:
    """A realistic model-free loop: weak smoke passes, verifier blocks, solver repairs.

    The visible smoke only checks that ``out.txt`` contains ``PASS``. The first
    solver artifact contains ``PASS-124`` so the smoke passes, but the verifier
    packet has enough success-definition/artifact evidence for the verifier hook
    to reject it. The second solver turn uses active finding context and repairs
    to ``PASS-123``.
    """
    env = _env(task="Create out.txt containing the exact token PASS-123.")
    config = _workbench_config(
        success="out.txt must contain the exact token PASS-123.",
        smoke_contains="PASS",
        recipe=True,
    )
    workbench = StaticWorkbenchArchitect(config)
    executor = MemoryExecutor(files={"input.txt": "expected token: PASS-123\n"})
    _register_smoke_handlers(env, config, executor, expected_token="PASS")
    fallback_ir = RuntimeConfigIR(
        architect_summary="fallback",
        solver_identity_prompt="fallback",
        selected_capabilities=("filesystem",),
    )
    turns = [
        SolverTurn(kind="act", summary="inspect input", actions=(
            _act("read-input", "read_file", {"path": "input.txt"}),
        )),
        SolverTurn(kind="act", summary="first implementation", actions=(
            _act("write-wrong", "write_file", {"path": "out.txt", "content": "PASS-124\n"}),
        )),
        SolverTurn(kind="submit_outcome", summary="submit first attempt"),
        SolverTurn(kind="act", summary="inspect repair evidence", actions=(
            _act("repair-observations", "observe_batch", {"operations": [
                {"request_id": "hist", "kind": "query_artifact_history", "arguments": {"path": "out.txt"}},
                {"request_id": "diff", "kind": "inspect_diff", "arguments": {"path": "out.txt"}},
            ]}, capability="kernel"),
        )),
        SolverTurn(kind="act", summary="record verifier requirement", actions=(
            _act("obs", "record_observation", {"observation": "Verifier requires exact PASS-123 token", "path": "out.txt"}, capability="kernel"),
        )),
        SolverTurn(kind="act", summary="repair after completion finding", actions=(
            _act("write-fixed", "write_file", {"path": "out.txt", "content": "PASS-123\n"}),
        )),
        SolverTurn(kind="submit_outcome", summary="submit repaired artifact"),
    ]
    verifier_outputs = [
        {
            "verdict": "needs_repair",
            "confidence": "high",
            "summary": "Visible smoke is too weak for the success definition.",
            "findings": [{
                "finding_id": "vf-exact-token",
                "summary": "out.txt contains PASS-124 but success requires exact PASS-123.",
                "evidence": ["artifact excerpt PASS-124", "success_definition exact PASS-123"],
                "repair_instruction": "Rewrite out.txt with exact token PASS-123 and rerun smoke evidence.",
                "applies_to": ["out.txt"],
            }],
        },
        {"verdict": "completed", "confidence": "high", "summary": "out.txt now contains exact PASS-123 and smoke evidence passed."},
    ]
    hooks = ScriptedHooks(fallback_ir=fallback_ir, turns=turns, verifier_outputs=verifier_outputs, executor=executor)
    result = AetherNextKernel(max_steps=8, workbench_architect=workbench).run(env, executor, hooks)
    receipts = [_receipt_view(receipt) for receipt in result.receipts]
    return IntegrationScenarioResult(
        scenario_id="workbench_verifier_repair_loop",
        status=result.status,
        final_files=dict(executor.files),
        receipts=receipts,
        context_packets=hooks.context_packets,
        verifier_packets=hooks.verifier_packets,
        checks={
            "completed": result.status == "completed",
            "verifier_blocked_first_submit": any(r["kind"] == "model_verifier_result" and r.get("verdict") == "needs_repair" for r in receipts),
            "active_finding_reached_context": any("active_completion_findings" in packet for packet in hooks.context_packets[1:]),
            "artifact_changed_after_finding": _artifact_changed_after_verifier_repair(receipts, "out.txt"),
            "final_content_exact": executor.files.get("out.txt") == "PASS-123\n",
        },
    )


def _artifact_changed_after_verifier_repair(receipts: list[dict[str, Any]], path: str) -> bool:
    """Audit-helper only: not part of the verifier packet.

    The verifier packet is state-only and must not include solver journey/history.
    Integration scenarios may still use the trace/receipt ledger to confirm that
    the solver repaired an artifact after completion feedback.
    """
    first_repair_step = None
    for receipt in receipts:
        if receipt.get("kind") == "model_verifier_result" and receipt.get("verdict") == "needs_repair":
            first_repair_step = receipt.get("step")
            break
    if first_repair_step is None:
        return False
    for receipt in receipts:
        if receipt.get("step", -1) <= first_repair_step:
            continue
        if receipt.get("kind") != "write_file":
            continue
        payload = receipt.get("payload", {}) or {}
        paths = {str(receipt.get("path", "")), str(payload.get("path", ""))}
        for key in ("modified_paths", "artifact_paths"):
            value = payload.get(key, ()) or ()
            if isinstance(value, str):
                paths.add(value)
            else:
                paths.update(str(item) for item in value)
        if path in paths:
            return True
    return False


def run_disabled_tool_guard_scenario() -> IntegrationScenarioResult:
    """Workbench omits shell guidance; stable-core tools still expose shell."""
    env = _env(task="Create out.txt without shell.")
    config = _workbench_config(success="out.txt exists.", smoke_contains="OK", recipe=False, enabled_tools=("read_file", "write_file", "query_memory"))
    workbench = StaticWorkbenchArchitect(config)
    executor = MemoryExecutor()
    _register_smoke_handlers(env, config, executor, expected_token="OK")
    hooks = ScriptedHooks(
        fallback_ir=RuntimeConfigIR(architect_summary="fallback", solver_identity_prompt="fallback", selected_capabilities=("filesystem",)),
        turns=[
            SolverTurn(kind="act", summary="exercise stable-core shell", actions=(
                _act("bad-shell", "run_command", {"command": "echo OK > out.txt"}, capability="shell"),
            )),
            SolverTurn(kind="act", summary="write artifact after observing shell result", actions=(
                _act("good-write", "write_file", {"path": "out.txt", "content": "OK\n"}),
            )),
            SolverTurn(kind="submit_outcome", summary="submit written artifact"),
        ],
        verifier_outputs=[{"verdict": "completed", "summary": "out.txt exists and smoke evidence passed."}],
    )
    result = AetherNextKernel(max_steps=3, workbench_architect=workbench).run(env, executor, hooks)
    receipts = [_receipt_view(receipt) for receipt in result.receipts]
    return IntegrationScenarioResult(
        scenario_id="stable_core_tool_guard",
        status=result.status,
        final_files=dict(executor.files),
        receipts=receipts,
        context_packets=hooks.context_packets,
        verifier_packets=hooks.verifier_packets,
        checks={
            "stable_core_shell_visible": any(r["kind"] == "run_command" for r in receipts),
            "mixed_dispatch_allowed_for_core_tools": executor.files.get("out.txt") == "OK\n",
            "status_completed_with_stable_core_tools": result.status == "completed",
        },
    )


def run_all_integration_scenarios() -> list[IntegrationScenarioResult]:
    return [run_workbench_verifier_repair_scenario(), run_disabled_tool_guard_scenario()]


def _env(*, task: str) -> EnvMap:
    return EnvMap(
        task_prompt=task,
        workspace_root="/app",
        visible_files=("input.txt",),
        file_tree="/app/input.txt\n",
        capabilities={
            "filesystem": CapabilityDescriptor("filesystem", "Read/write files", tool_names=("read_file", "write_file")),
            "shell": CapabilityDescriptor("shell", "Run shell", tool_names=("run_command",)),
        },
    )


def _workbench_config(*, success: str, smoke_contains: str, recipe: bool, enabled_tools: tuple[str, ...] = ("read_file", "write_file", "query_memory", "query_artifact_history", "inspect_diff", "record_observation")) -> HarnessConfigIR:
    payload: dict[str, Any] = {
        "schema_version": "harness_config.v1",
        "task_understanding": "Synthetic deterministic integration task.",
        "success_definition": success,
        "solver_system_prompt": {
            "role": "verification-first solver",
            "workflow": ["inspect inputs", "write artifact", "run/check visible evidence", "repair completion findings before resubmitting"],
            "self_verification": ["Read or inspect out.txt before submit readiness.", "Treat verifier needs_repair as active blocker."],
            "memory_use": ["Use query_memory/query_artifact_history/inspect_diff before repeating reads or checks, not as a ritual first action."],
            "avoid": ["Do not call task_done repeatedly without new evidence."],
        },
        "verifier_system_prompt": {
            "role": "Read-only current-state verifier for the synthetic integration artifact",
            "success_criteria": [success],
            "required_evidence": ["current out.txt state and visible check evidence support completion"],
            "false_positive_traps": ["out.txt can exist while containing the wrong token"],
            "verdict_guidance": ["completed requires current evidence; needs_repair names the observed gap"],
            "feedback_guidance": ["tell the solver which artifact or check to repair"],
        },
        "evidence_requirements": ["current out.txt state supports completion", "visible check evidence supports completion"],
        "false_positive_risks": ["out.txt can exist while containing the wrong token"],
        "minimum_completion_evidence": ["current out.txt state and visible check evidence"],
        "tool_policy": {"enabled_tools": list(enabled_tools)},
        "context_policy": {"mode": "retrieval_augmented"},
        "verification_policy": {"visible_smoke_tests": [{"type": "content_assertion", "path": "out.txt", "contains": smoke_contains}]},
        "model_verifier_policy": {"enabled": True, "runs_on": ["solver_submit"]},
        "local_verification_limits": ["Visible smoke tests are internal evidence only and may be semantically weak."],
    }
    if recipe:
        payload["context_policy"]["recipe"] = {
            "always_include": ["pending_checks", "active_completion_findings", "artifact_history", "observations"],
            "include_recent": [{"selector": "file_writes", "count": 4}, {"selector": "check_results", "count": 4}],
            "preserve_exact": ["active_completion_findings", "pending_checks"],
            "make_queryable_not_inline": ["memory_events"],
        }
    return parse_harness_config_ir(json.dumps(payload))


def _register_smoke_handlers(env: EnvMap, config: HarnessConfigIR, executor: MemoryExecutor, *, expected_token: str) -> None:
    resolved = resolve_runtime(env, ConfigCompiler(CapabilityRegistry.from_envmap(env)), _ExistsHooks(), workbench_architect=StaticWorkbenchArchitect(config))
    assert resolved.compiled is not None
    for check in resolved.compiled.planned_checks():
        def handler(exec_: MemoryExecutor, command: str, token: str = expected_token) -> CommandResult:
            try:
                content = exec_.read_file("out.txt")
            except FileNotFoundError:
                return CommandResult(command, 1, stderr="out.txt missing")
            ok = token in content
            return CommandResult(command, 0 if ok else 1, stdout="contains token" if ok else "", stderr="missing token" if not ok else "")
        executor.register_command(check.command, handler)


def _act(action_id: str, kind: str, args: dict[str, Any], *, capability: str = "filesystem") -> ActionRequest:
    return ActionRequest(action_id, kind, capability, args, "integration test", "receipt", "repair")


def _extract_context_packet(messages: list[dict[str, str]]) -> dict[str, Any]:
    if not messages:
        return {}
    content = messages[-1].get("content", "")
    marker = "[context_packet]\n"
    if not content.startswith(marker):
        return {}
    return json.loads(content[len(marker):])


def _receipt_view(receipt: Any) -> dict[str, Any]:
    payload = dict(receipt.payload)
    view = {
        "receipt_id": receipt.receipt_id,
        "step": receipt.step,
        "kind": receipt.kind,
        "success": receipt.success,
        "summary": receipt.summary,
        "failure_class": receipt.failure_class,
        "state_change": receipt.state_change,
    }
    for key in ("check_id", "path", "command", "passed", "verdict"):
        if key in payload:
            view[key] = payload[key]
    if receipt.kind == "model_verifier_result":
        view["verdict"] = payload.get("verdict")
    return view
