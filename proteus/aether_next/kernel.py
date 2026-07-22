"""Aether-Next kernel: the solver/verifier control loop.

The solver-turn parse-error handling (same-step retry) lives in
kernel_solver_turn.py; the verifier-disagreement stalemate check lives in
kernel_stalemate.py. Both were extracted from this module to hold it under
the 500-LOC cap; each is a pure move of the corresponding branch out of
``AetherNextKernel.run``, called back in unchanged.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, TYPE_CHECKING

from .compiler import CapabilityRegistry, ConfigCompiler
from .completion import CompletionGate, FailureParser
from .execution import (
    BootstrapEngine, Executor, ExperimentEngine, PerceptionLane, ProcessOrchestratorV2,
)
from .kernel_config import ResolvedRuntime, resolve_runtime
from .kernel_messages import build_architect_request, build_solver_messages
from .kernel_verifier import run_model_verifier_if_available
from .context_compiler import ContextCompiler
from .ledger import ExecutionLedger, Receipt
from .monitors import IntegrityGuards, LocalOnlySafetyGuard, MonitorRunner
from .no_progress import NoProgressController
from .kernel_dispatch import dispatch_action
from .kernel_solver_turn import handle_solver_parse_error
from .kernel_stalemate import check_verifier_stalemate
from .kernel_turns import run_act_turn, run_submit_turn
from .kernel_reconfigure import verifier_triggered_reconfigure
from .model_hooks import ModelOutputError
from .route_preflight import preflight_proof_routes
from .runtime_ir import (
    CompiledRuntime, EnvMap, RuntimeConfigIR, SolverTurn,
)
from .world import WorldState
from .task_contract import TaskClause, TaskContract

if TYPE_CHECKING:
    from .tracing import RunTrace

@dataclass(frozen=True)
class KernelResult:
    status: str
    step: int
    reconfigurations: int
    used_check_ids: tuple[str, ...] = ()
    used_check_commands: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    env_digest: str = ""
    receipts: tuple[Receipt, ...] = ()
    # An architect defect is recorded even when the task subsequently passes:
    # the initial config needed repair, or the verifier had to trigger a
    # reconfiguration.  Result rows must never launder architect defects into
    # clean passes.
    architect_defect: bool = False
    architect_defect_reasons: tuple[str, ...] = ()
    accounting: Mapping[str, int] | None = None

def _completed_result(
    step: int, reconfigurations: int, decision: Any,
    compiled: CompiledRuntime, ledger: ExecutionLedger,
    architect_defect_reasons: tuple[str, ...] = (),
) -> KernelResult:
    """Build a completed ``KernelResult`` only from a ready gate decision.

    This helper is the final mechanical completion boundary.  A model verdict
    may contribute semantic evidence, but it can never override current gate
    blockers.  Keep the guard here as defence in depth for every caller.
    """
    if not bool(getattr(decision, "ready", False)):
        raise ValueError("completed result requires a ready completion decision")
    used_ids = set(decision.used_check_ids)
    return KernelResult(
        status="completed", step=step, reconfigurations=reconfigurations,
        used_check_ids=decision.used_check_ids,
        used_check_commands=tuple(
            c.command for c in compiled.planned_checks() if c.check_id in used_ids
        ),
        blockers=(), env_digest=compiled.env_digest, receipts=ledger.all_receipts(),
        architect_defect=bool(architect_defect_reasons),
        architect_defect_reasons=architect_defect_reasons,
        accounting=ledger.accounting_snapshot(),
    )


class KernelHooks(Protocol):
    def architect(self, request: Mapping[str, Any]) -> RuntimeConfigIR:
        ...

    def solve(self, messages: list[dict[str, str]], compiled: CompiledRuntime) -> SolverTurn:
        ...

class AetherNextKernel:
    # Bounded verifier-disagreement protocol: after this many consecutive
    # verification rounds in which the identical non-empty finding set
    # survives despite intervening solver evidence, the run terminates with
    # status ``verifier_stalemate``.  The harness records the disagreement;
    # it never adjudicates it.
    STALEMATE_ROUNDS = 3
    SUBMIT_STALEMATE_ROUNDS = 3

    def __init__(
        self,
        *,
        max_steps: int = 24,
        max_solver_turns: int | None = None,
        max_accepted_task_actions: int | None = None,
        certified_production: bool = False,
        workbench_architect: Any | None = None,
        snapshot_callback: Callable[[int], None] | None = None,
        snapshot_steps: tuple[int, ...] = (),
    ) -> None:
        self.max_steps = max(1, int(max_steps))
        self.max_solver_turns = max(1, int(max_solver_turns)) if max_solver_turns is not None else self.max_steps
        self.max_accepted_task_actions = (
            max(1, int(max_accepted_task_actions))
            if max_accepted_task_actions is not None else None
        )
        self.certified_production = bool(certified_production)
        self.workbench_architect = workbench_architect
        self.snapshot_callback, self._snapshot_steps = snapshot_callback, frozenset(snapshot_steps)
        self.monitor_runner = MonitorRunner()
        self.context_compiler = ContextCompiler()
        self.completion_gate = CompletionGate()
        self.failure_parser = FailureParser()
        self.bootstrap_engine = BootstrapEngine()
        self.process_orchestrator = ProcessOrchestratorV2()
        self.perception_lane = PerceptionLane()
        self.experiment_engine = ExperimentEngine()
        self.integrity_guards = IntegrityGuards()
        self.safety_guard = LocalOnlySafetyGuard()
        self.no_progress_controller = NoProgressController()

    def build_architect_request(
        self,
        envmap: EnvMap,
        compiler: ConfigCompiler,
    ) -> dict[str, Any]:
        return build_architect_request(envmap, compiler)

    def build_solver_messages(
        self,
        compiled: CompiledRuntime,
        context_packet: Mapping[str, Any],
    ) -> list[dict[str, str]]:
        return build_solver_messages(compiled, context_packet)

    def run(
        self,
        envmap: EnvMap,
        executor: Executor,
        hooks: KernelHooks,
        *,
        world_state: WorldState | None = None,
        trace: RunTrace | None = None,
        run_timeout_s: float | None = None,
    ) -> KernelResult:
        # Perception (e.g. vision transcription) needs the model hooks at
        # dispatch time; scoped to this run.
        self.active_hooks = hooks
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        resolved = resolve_runtime(
            envmap, compiler, hooks,
            workbench_architect=self.workbench_architect,
        )
        if resolved.config_invalid_blockers:
            return KernelResult(
                status="config_invalid", step=0, reconfigurations=0,
                blockers=resolved.config_invalid_blockers,
                env_digest=envmap.digest(), receipts=(),
            )
        runtime_ir = resolved.runtime_ir
        compiled = resolved.compiled  # type: ignore[assignment]
        objective_graph = resolved.objective_graph
        eval_index = resolved.eval_index
        architect_repair_codes = resolved.repair_codes
        ledger = ExecutionLedger()
        ledger.seed_capabilities(compiled.selected_capability_ids())
        ledger.ensure_objective(compiled.objective_graph)
        realization = dict(compiled.config_realization)
        if resolved.workbench_config is not None:
            from .workbench_compile import config_realization_audit
            realization["architect_path"] = "workbench"
            realization["harness_config_schema_version"] = resolved.workbench_config.schema_version
            realization["expected_steps"] = int(getattr(resolved.workbench_config, "expected_steps", 0) or 0)
            realization["workbench_repair_warning_codes"] = list(resolved.workbench_config.repair_warning_codes)
            realization["workbench_repair_warnings"] = list(resolved.workbench_config.repair_warnings)
            realization["workbench_rejected_config_items"] = [dict(item) for item in resolved.workbench_config.rejected_config_items]
            realization["legacy_tool_selection_paths"] = list(getattr(resolved.workbench_config, "legacy_tool_selection_paths", ()))
            realization["legacy_tool_selection_warning"] = str(getattr(resolved.workbench_config, "legacy_tool_selection_warning", ""))
            realization["harness_config_realization_audit"] = config_realization_audit(
                resolved.workbench_config, envmap,
            )
        else:
            realization["architect_path"] = "ir"
        realization["certified_production_reconfiguration"] = {
            "enabled": not self.certified_production,
            "policy": (
                "legacy_test_mode" if not self.certified_production
                else "suspended_model_authored_reconfiguration"
            ),
        }
        ledger.record_config_realization(realization)
        route_rows, route_issues = preflight_proof_routes(compiled, executor, envmap)
        ledger.record(Receipt(
            receipt_id="config:route_preflight",
            step=0,
            kind="route_preflight",
            success=not bool(route_issues),
            summary=(
                "compiled proof routes available"
                if not route_issues else "compiled proof routes unavailable"
            ),
            failure_class="config_invalid" if route_issues else "",
            payload={
                "rows": [row.as_dict() for row in route_rows],
                "issues": [issue.as_dict() for issue in route_issues],
            },
        ))
        if route_issues:
            return KernelResult(
                status="config_invalid", step=0, reconfigurations=0,
                blockers=tuple(f"{issue.code}:{issue.clause_id}" for issue in route_issues),
                env_digest=compiled.env_digest, receipts=ledger.all_receipts(),
                accounting=ledger.accounting_snapshot(),
            )
        if world_state is None:
            # The compiled task truth becomes the initial immutable contract;
            # subsequent solver actions only update dynamic state.  This keeps
            # direct callers safe while allowing adapters to inject a richer
            # pre-existing WorldState when they have one.
            world_state = WorldState(
                task_contract=TaskContract.create(
                    compiled.task_prompt,
                    (TaskClause(
                        "compiled:objective",
                        compiled.success_definition or compiled.task_prompt,
                    ),),
                ),
                env_facts={
                    "workspace_root": envmap.workspace_root,
                    "network_scope": envmap.network_scope,
                    "visible_file_count": len(envmap.visible_files),
                    "visible_dir_count": len(envmap.visible_dirs),
                },
            )
        if architect_repair_codes:
            ledger.record(Receipt(
                receipt_id="config:architect_repair", step=0,
                kind="config_repair", success=True,
                summary=f"Architect config repaired: {', '.join(architect_repair_codes)}",
            ))
        if trace is not None:
            trace.set_architect(
                runtime_ir, (),
                compiled.prefix_messages(),
                repair_codes=architect_repair_codes,
            )
        reconfigurations = 0
        step = 0
        context_packet: Mapping[str, Any] | None = None
        turn: SolverTurn | None = None
        architect_defect_reasons: list[str] = [
            f"initial_config_repaired:{code}" for code in architect_repair_codes
        ]
        verifier_reconfigure_used = False
        verifier_round_finding_sets: list[frozenset[str]] = []
        verifier_memo: dict[str, Any] = {}
        submit_without_evidence_rounds = 0
        # Wall-clock backstop. The docker runner also arms a one-shot SIGALRM,
        # but that alarm can be swallowed if it fires inside a broad except (the
        # RC5 failure mode). This monotonic deadline is checked every iteration,
        # so the loop terminates even if an interrupt was absorbed somewhere the
        # re-raise guards did not cover -- belt to the SIGALRM suspenders.
        deadline = (
            time.monotonic() + run_timeout_s
            if run_timeout_s is not None and run_timeout_s > 0
            else None
        )
        while step < self.max_steps:
            if ledger.accounting_value("solver_provider_turns") >= self.max_solver_turns:
                ledger.record(Receipt(
                    receipt_id=f"step-{step}:solver_turn_budget_exhausted",
                    step=step, kind="solver_turn_budget_exhausted", success=False,
                    summary=(
                        f"maximum solver provider turns reached: {self.max_solver_turns}; "
                        "accepted task-action budget is tracked separately"
                    ),
                    failure_class="solver_turn_budget_exhausted",
                    payload={"max_solver_turns": self.max_solver_turns,
                             "accounting": ledger.accounting_snapshot()},
                ))
                return KernelResult(
                    status="solver_turn_budget_exhausted", step=step,
                    reconfigurations=reconfigurations,
                    blockers=("solver_turn_budget_exhausted",),
                    env_digest=compiled.env_digest, receipts=ledger.all_receipts(),
                    architect_defect=bool(architect_defect_reasons),
                    architect_defect_reasons=tuple(architect_defect_reasons),
                    accounting=ledger.accounting_snapshot(),
                )
            if deadline is not None and time.monotonic() >= deadline:
                ledger.record(Receipt(
                    receipt_id=f"step-{step}:kernel_deadline_exceeded",
                    step=step,
                    kind="kernel_deadline_exceeded",
                    success=False,
                    summary=(
                        f"kernel loop deadline reached ({run_timeout_s:g}s); terminating "
                        "so the run can be graded instead of spinning past its budget"
                    ),
                    failure_class="timeout",
                ))
                return KernelResult(
                    status="timeout",
                    step=step,
                    reconfigurations=reconfigurations,
                    blockers=(f"kernel_deadline_exceeded_{run_timeout_s:g}s",),
                    env_digest=compiled.env_digest,
                    receipts=ledger.all_receipts(),
                    architect_defect=bool(architect_defect_reasons),
                    architect_defect_reasons=tuple(architect_defect_reasons),
                )
            alerts = self.monitor_runner.run(compiled, ledger)
            context_packet = self.context_compiler.compile(compiled, ledger, alerts)
            messages = self.build_solver_messages(compiled, context_packet)
            before_count = len(ledger.all_receipts())
            try:
                ledger.record_accounting(
                    receipt_id=f"step-{step}:solver_provider_turn:{ledger.accounting_value('solver_provider_turns') + 1}",
                    step=step, counter="solver_provider_turns", event="primary_solver_call",
                )
                turn = hooks.solve(messages, compiled)
            except ModelOutputError as exc:
                turn = handle_solver_parse_error(
                    hooks, exc, step, compiled, messages, ledger,
                    context_packet, trace, before_count,
                )
                if turn is None:
                    self._fire_snapshot(step); step += 1; continue
            turn_errors = turn.validate(compiled.action_schema)
            if turn_errors:
                ledger.record(Receipt(
                    receipt_id=f"step-{step}:turn_invalid", step=step, kind="turn_validation",
                    success=False, summary=f"invalid turn: {'; '.join(turn_errors)}",
                    failure_class="turn_validation",
                ))
                if trace is not None:
                    trace.add_step(step, context_packet, turn, ledger.all_receipts()[before_count:])
                self._fire_snapshot(step); step += 1; continue
            if turn.kind == "act":
                submit_without_evidence_rounds = 0
                run_act_turn(self, turn, step, compiled, executor, envmap, ledger, world_state)
            elif turn.kind == "submit_outcome":
                ledger.record_accounting(
                    receipt_id=f"step-{step}:solver_submission_turn:{ledger.accounting_value('solver_submission_turns') + 1}",
                    step=step, counter="solver_submission_turns", event="submit_outcome",
                )
                run_submit_turn(
                    self, step, compiled, executor, envmap, ledger, trace,
                )
                decision = self._last_gate_decision
                canonical_workbench = self.workbench_architect is not None
                if canonical_workbench and self._active_findings_need_intervening_evidence(ledger):
                    verdict = None
                    submit_without_evidence_rounds += 1
                    active_findings = ledger.active_finding_context(step)
                    ledger.record(Receipt(
                        receipt_id=f"step-{step}:model_verifier_skipped:active_findings",
                        step=step,
                        kind="model_verifier_skipped",
                        success=True,
                        summary=(
                            "model verifier skipped: active completion findings require "
                            "an intervening solver action or evidence before another submit"
                        ),
                        failure_class="",
                        payload={
                            "reason": "active_findings_without_intervening_evidence",
                            "active_findings": active_findings,
                            "submit_without_evidence_rounds": submit_without_evidence_rounds,
                        },
                    ))
                    if submit_without_evidence_rounds >= self.SUBMIT_STALEMATE_ROUNDS:
                        active_ids = tuple(sorted(
                            str(item.get("finding_id", ""))
                            for item in active_findings
                            if str(item.get("finding_id", "")).strip()
                        ))
                        ledger.record(Receipt(
                            receipt_id=f"step-{step}:solver_submit_stalemate",
                            step=step,
                            kind="solver_submit_stalemate",
                            success=False,
                            summary=(
                                "solver submit stalemate: active completion findings "
                                "required intervening evidence, but the solver kept "
                                "submitting without adding evidence"
                            ),
                            failure_class="solver_submit_stalemate",
                            payload={
                                "rounds": submit_without_evidence_rounds,
                                "finding_ids": active_ids,
                                "active_findings": active_findings,
                            },
                        ))
                        if trace is not None:
                            trace.add_step(step, context_packet, turn, ledger.all_receipts()[before_count:])
                        return KernelResult(
                            status="solver_submit_stalemate",
                            step=step,
                            reconfigurations=reconfigurations,
                            blockers=active_ids,
                            env_digest=compiled.env_digest,
                            receipts=ledger.all_receipts(),
                            architect_defect=bool(architect_defect_reasons),
                            architect_defect_reasons=tuple(architect_defect_reasons),
                            accounting=ledger.accounting_snapshot(),
                        )
                else:
                    submit_without_evidence_rounds = 0
                    verdict = run_model_verifier_if_available(
                        hooks,
                        compiled,
                        ledger,
                        step=step,
                        reason="solver_submit",
                        executor=executor,
                        envmap=envmap,
                        dynamic_state=(world_state.dynamic_snapshot() if world_state is not None else None),
                        memo=verifier_memo,
                    )
                if verdict is not None and verdict.verdict == "completed":
                    # Verifier completion is semantic evidence, not completion
                    # authority.  Re-evaluate every mechanical blocker after
                    # the Verifier/proof bridge has recorded its current-round
                    # evidence.  Never reuse the pre-Verifier submit decision.
                    ready_decision = self.completion_gate.evaluate(
                        compiled, ledger, self.monitor_runner.run(compiled, ledger),
                    )
                    if not ready_decision.ready:
                        ledger.record(Receipt(
                            receipt_id=f"step-{step}:verifier_completed_gate_not_ready",
                            step=step,
                            kind="verifier_completed_gate_not_ready",
                            success=False,
                            summary=(
                                "Verifier returned completed but the fresh deterministic "
                                "completion gate still has blockers"
                            ),
                            failure_class="completion_gate_not_ready",
                            payload={
                                "blockers": [
                                    {"code": blocker.code, "detail": blocker.detail,
                                     "source": blocker.source}
                                    for blocker in ready_decision.blockers
                                ],
                            },
                        ))
                        if trace is not None:
                            trace.add_step(
                                step, context_packet, turn,
                                ledger.all_receipts()[before_count:],
                            )
                        self._fire_snapshot(step)
                        step += 1
                        continue
                    if trace is not None:
                        trace.add_step(
                            step, context_packet, turn,
                            ledger.all_receipts()[before_count:],
                        )
                    return _completed_result(
                        step, reconfigurations, ready_decision, compiled, ledger,
                        tuple(architect_defect_reasons),
                    )
                if decision is not None and decision.ready and verdict is None and not canonical_workbench:
                    if trace is not None:
                        trace.add_step(step, context_packet, turn, ledger.all_receipts()[before_count:])
                    return _completed_result(
                        step, reconfigurations, decision, compiled, ledger,
                        tuple(architect_defect_reasons),
                    )
                if trace is not None:
                    trace.add_step(step, context_packet, turn, ledger.all_receipts()[before_count:])
                if verdict is not None and verdict.verdict != "completed":
                    stalemate_result = check_verifier_stalemate(
                        self, verdict, step, reconfigurations, compiled, ledger,
                        architect_defect_reasons, verifier_round_finding_sets,
                    )
                    if stalemate_result is not None:
                        return stalemate_result
                if (
                    verdict is not None
                    and verdict.verdict == "blocked_by_harness_config"
                    and canonical_workbench
                    and not self.certified_production
                ):
                    if not verifier_reconfigure_used:
                        # Reconfiguration is an exceptional owner transfer, not
                        # a generic response to a blocked verdict.  It requires
                        # a verifier recovery receipt that explicitly proves
                        # the blocker owner and evidence gate; otherwise the
                        # verifier lane remains fail-closed.
                        recovery_receipt = next(
                            (
                                receipt for receipt in reversed(ledger.all_receipts())
                                if receipt.kind == "verifier_recovery_route"
                            ),
                            None,
                        )
                        recovery_payload = (
                            recovery_receipt.payload
                            if recovery_receipt is not None and isinstance(recovery_receipt.payload, dict)
                            else {}
                        )
                        verified_owner = str(recovery_payload.get("blocker_owner", "")).strip()
                        verified = bool(recovery_payload.get("blocker_verified", False))
                        reconfigure_policy = compiled.config_realization.get("reconfigure_policy", {})
                        allowed_owners = {
                            str(item).strip()
                            for item in (reconfigure_policy.get("allowed_owners", ()) if isinstance(reconfigure_policy, dict) else ())
                            if str(item).strip()
                        } or {"harness_config"}
                        if (
                            recovery_payload.get("action") == "reconfigure"
                            and verified
                            and verified_owner in allowed_owners
                        ):
                            verifier_reconfigure_used = True
                            compiled, reconfigured = verifier_triggered_reconfigure(
                                self, hooks, compiler, envmap, compiled, ledger, verdict,
                                current_step=step,
                            )
                            if reconfigured:
                                reconfigurations += 1
                                architect_defect_reasons.append("verifier_triggered_reconfigure")
                        else:
                            ledger.record(Receipt(
                                receipt_id=f"step-{step}:verifier_reconfigure_denied",
                                step=step,
                                kind="verifier_reconfigure_denied",
                                success=False,
                                summary=(
                                    "blocked_by_harness_config lacked a verified allowed-owner "
                                    "recovery receipt; reconfiguration denied"
                                ),
                                failure_class="config_invalid",
                                payload={
                                    "architect_defect": True,
                                    "recovery_action": recovery_payload.get("action", ""),
                                    "blocker_owner": verified_owner,
                                    "blocker_verified": verified,
                                },
                            ))
                    else:
                        ledger.record(Receipt(
                            receipt_id=f"step-{step}:verifier_reconfigure_exhausted",
                            step=step,
                            kind="verifier_reconfigure_exhausted",
                            success=False,
                            summary=(
                                "verifier reported blocked_by_harness_config again but the "
                                "single-shot reconfiguration was already used"
                            ),
                            failure_class="config_invalid",
                            payload={"architect_defect": True},
                        ))
                elif (
                    verdict is not None
                    and verdict.verdict == "blocked_by_harness_config"
                    and canonical_workbench
                    and self.certified_production
                ):
                    ledger.record(Receipt(
                        receipt_id=f"step-{step}:verifier_reconfigure_suspended",
                        step=step,
                        kind="verifier_reconfigure_suspended",
                        success=True,
                        summary="production certified policy suspended model-authored reconfiguration",
                        payload={"policy": "certified_production_no_reconfigure"},
                    ))
                if (
                    decision is not None
                    and canonical_workbench
                    and verdict is None
                ):
                    ledger.record(Receipt(
                        receipt_id=f"step-{step}:verifier_required_for_completion",
                        step=step,
                        kind="verifier_required_for_completion",
                        success=False,
                        summary="canonical workbench submit did not receive a verifier verdict; completion remains solver-driven until verifier runs",
                        failure_class="verifier_missing",
                    ))
                self._fire_snapshot(step); step += 1; continue
            if trace is not None:
                trace.add_step(step, context_packet, turn, ledger.all_receipts()[before_count:])
            self._fire_snapshot(step); step += 1
        return KernelResult(
            status="incomplete", step=step, reconfigurations=reconfigurations,
            blockers=tuple(ob.obligation_id for ob in ledger.open_obligations()),
            env_digest=compiled.env_digest, receipts=ledger.all_receipts(),
            architect_defect=bool(architect_defect_reasons),
            architect_defect_reasons=tuple(architect_defect_reasons),
            accounting=ledger.accounting_snapshot(),
        )

    @staticmethod
    def _active_findings_need_intervening_evidence(ledger: ExecutionLedger) -> bool:
        from .finding_evidence import active_findings_need_relevant_evidence

        return active_findings_need_relevant_evidence(ledger)

    def _fire_snapshot(self, step: int) -> None:
        if self.snapshot_callback is not None and step in self._snapshot_steps:
            self.snapshot_callback(step)
