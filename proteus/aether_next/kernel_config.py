"""Config resolution logic extracted from kernel.run().

Pure function that resolves a RuntimeConfigIR into a fully-compiled runtime,
handling the architect/validate/repair/fail-closed pipeline.
The kernel records trace/receipts from the returned ``ResolvedRuntime``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, TYPE_CHECKING

from .compiler import ConfigCompiler
from .kernel_messages import build_architect_request
from .repair import repair_config
from .runtime_ir import (
    CompiledRuntime,
    EnvMap,
    EvalIndex,
    ObjectiveGraph,
    RuntimeConfigIR,
)
from .smoke_compile import compile_visible_smoke_tests
from .workbench_compile import harness_config_to_runtime_ir

if TYPE_CHECKING:
    from .workbench_hooks import WorkbenchArchitect


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedRuntime:
    """Outcome of config resolution: everything the kernel needs to run."""

    runtime_ir: RuntimeConfigIR
    compiled: CompiledRuntime | None
    objective_graph: ObjectiveGraph
    eval_index: EvalIndex
    repair_codes: tuple[str, ...]
    fallback_codes: tuple[str, ...]
    config_invalid_blockers: tuple[str, ...]
    workbench_config: Any | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _baseline_resolve(
    envmap: EnvMap,
    compiler: ConfigCompiler,
    hooks: Any,
) -> ResolvedRuntime:
    """Resolve runtime using the standard IR architect path."""
    objective_graph, eval_index = compiler.analyze_envmap(envmap)
    request = build_architect_request(envmap, compiler)
    try:
        runtime_ir = hooks.architect(request)
    except Exception as exc:
        blockers = ("architect_config_parse_failed", str(exc))
        return ResolvedRuntime(
            runtime_ir=_invalid_runtime_ir("architect parse failed"),
            compiled=None,
            objective_graph=objective_graph,
            eval_index=eval_index,
            repair_codes=(),
            fallback_codes=blockers,
            config_invalid_blockers=blockers,
        )

    issues = compiler.validate(
        runtime_ir, envmap,
        objective_graph=objective_graph, eval_index=eval_index,
    )
    fatal_issues = [issue for issue in issues if issue.fatal]
    fallback_codes: tuple[str, ...] = ()
    repair_codes: tuple[str, ...] = ()

    if fatal_issues:
        repaired_ir, rep_codes = repair_config(
            runtime_ir, compiler, envmap,
            objective_graph=objective_graph, eval_index=eval_index,
        )
        post_repair_issues = compiler.validate(
            repaired_ir, envmap,
            objective_graph=objective_graph, eval_index=eval_index,
        )
        post_repair_fatal = [i for i in post_repair_issues if i.fatal]
        if not post_repair_fatal:
            runtime_ir = repaired_ir
            repair_codes = rep_codes
        else:
            fallback_codes = tuple(issue.code for issue in fatal_issues)
            runtime_ir = _invalid_runtime_ir("baseline config validation failed")
            return ResolvedRuntime(
                runtime_ir=runtime_ir,
                compiled=None,
                objective_graph=objective_graph,
                eval_index=eval_index,
                repair_codes=repair_codes,
                fallback_codes=fallback_codes,
                config_invalid_blockers=fallback_codes,
            )

    compiled = compiler.compile(
        runtime_ir, envmap,
        objective_graph=objective_graph, eval_index=eval_index,
    )
    semantic_status = str(compiled.config_realization.get("semantic_evidence_status", ""))
    if semantic_status.startswith("invalid_"):
        blockers = (f"semantic_evidence_{semantic_status}",)
        return ResolvedRuntime(
            runtime_ir=_invalid_runtime_ir("semantic evidence contract invalid"),
            compiled=None,
            objective_graph=objective_graph,
            eval_index=eval_index,
            repair_codes=repair_codes,
            fallback_codes=blockers,
            config_invalid_blockers=blockers,
            workbench_config=None,
        )
    return ResolvedRuntime(
        runtime_ir=runtime_ir,
        compiled=compiled,
        objective_graph=objective_graph,
        eval_index=eval_index,
        repair_codes=repair_codes,
        fallback_codes=fallback_codes,
        config_invalid_blockers=(),
    )


def _invalid_runtime_ir(reason: str) -> RuntimeConfigIR:
    """Non-executable placeholder for failed initialization.

    Failed architect/config output must never be represented as a generic safe
    default. ``ResolvedRuntime`` still carries a RuntimeConfigIR for trace/result
    shape compatibility, but callers must see ``compiled=None`` and
    ``config_invalid_blockers`` and stop before the solver.
    """
    return RuntimeConfigIR(
        architect_summary=f"[invalid config -- {reason}]",
        solver_identity_prompt="",
        selected_capabilities=(),
    )


def _workbench_resolve(
    envmap: EnvMap,
    compiler: ConfigCompiler,
    hooks: Any,
    workbench_architect: "WorkbenchArchitect",
    *,
    reconfigure_context: Mapping[str, Any] | None = None,
) -> ResolvedRuntime:
    """Resolve runtime from vNext HarnessConfigIR.

    ``reconfigure_context`` (reason/failure_clusters/open_obligations from the
    kernel's mid-run reconfigure request) is folded into the same architect
    request used for the initial config, so a reconfiguration re-invokes the
    real workbench architect with full task context plus what went wrong --
    instead of falling through to the separate legacy ``hooks.reconfigure()``
    interface (a much thinner prompt and parser that silently collapses a rich
    architect contract to a generic "careful software engineer" default).
    """
    objective_graph, eval_index = compiler.analyze_envmap(envmap)
    request = build_architect_request(envmap, compiler)
    if reconfigure_context is not None:
        request = dict(request)
        request["reconfigure_context"] = dict(reconfigure_context)
    config, errors = workbench_architect.configure(request)

    if config is None:
        blockers = ("workbench_architect_configure_failed",) + tuple(str(err) for err in errors)
        runtime_ir = _invalid_runtime_ir("workbench architect configure failed")
        return ResolvedRuntime(
            runtime_ir=runtime_ir,
            compiled=None,
            objective_graph=objective_graph,
            eval_index=eval_index,
            repair_codes=(),
            fallback_codes=blockers,
            config_invalid_blockers=blockers,
            workbench_config=None,
        )

    smoke_result = compile_visible_smoke_tests(config, envmap)
    if smoke_result.checks:
        eval_index = EvalIndex(checks=eval_index.checks + smoke_result.checks)
    runtime_ir = harness_config_to_runtime_ir(config, envmap)
    if smoke_result.checks:
        runtime_ir = replace(
            runtime_ir,
            check_plan=runtime_ir.check_plan + tuple(check.check_id for check in smoke_result.checks),
            advisory_notes=runtime_ir.advisory_notes + (
                f"compiled_visible_smoke_checks={len(smoke_result.checks)}",
            ),
        )
    if smoke_result.rejected:
        runtime_ir = replace(
            runtime_ir,
            advisory_notes=runtime_ir.advisory_notes + (
                "visible_smoke_compile_rejections=" + str([dict(item) for item in smoke_result.rejected]),
            ),
        )
    issues = compiler.validate(
        runtime_ir, envmap,
        objective_graph=objective_graph, eval_index=eval_index,
    )
    fatal_issues = [issue for issue in issues if issue.fatal]
    fallback_codes: tuple[str, ...] = ()
    repair_codes: tuple[str, ...] = ()

    if fatal_issues:
        repaired_ir, rep_codes = repair_config(
            runtime_ir, compiler, envmap,
            objective_graph=objective_graph, eval_index=eval_index,
        )
        post_repair_issues = compiler.validate(
            repaired_ir, envmap,
            objective_graph=objective_graph, eval_index=eval_index,
        )
        post_repair_fatal = [i for i in post_repair_issues if i.fatal]
        if not post_repair_fatal:
            runtime_ir = repaired_ir
            repair_codes = rep_codes
        else:
            fallback_codes = tuple(issue.code for issue in fatal_issues)
            runtime_ir = _invalid_runtime_ir("workbench compiled config validation failed")
            return ResolvedRuntime(
                runtime_ir=runtime_ir,
                compiled=None,
                objective_graph=objective_graph,
                eval_index=eval_index,
                repair_codes=repair_codes,
                fallback_codes=fallback_codes,
                config_invalid_blockers=fallback_codes,
                workbench_config=config,
            )

    compiled = compiler.compile(
        runtime_ir, envmap,
        objective_graph=objective_graph, eval_index=eval_index,
    )
    return ResolvedRuntime(
        runtime_ir=runtime_ir,
        compiled=compiled,
        objective_graph=objective_graph,
        eval_index=eval_index,
        repair_codes=repair_codes,
        fallback_codes=fallback_codes,
        config_invalid_blockers=(),
        workbench_config=config,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_runtime(
    envmap: EnvMap,
    compiler: ConfigCompiler,
    hooks: Any,
    *,
    workbench_architect: "WorkbenchArchitect | None" = None,
    reconfigure_context: Mapping[str, Any] | None = None,
) -> ResolvedRuntime:
    """Resolve a full runtime configuration from an envmap.

    When no architect override is provided, reproduces the original kernel
    baseline path exactly.  ``workbench_architect`` enables the vNext
    HarnessConfigIR path and fails closed on configuration failure.
    ``reconfigure_context``, when set, marks this as a mid-run reconfiguration
    and is folded into the workbench architect request (see
    ``_workbench_resolve``); it is a no-op for the other two paths.
    """
    if workbench_architect is not None:
        return _workbench_resolve(
            envmap, compiler, hooks, workbench_architect,
            reconfigure_context=reconfigure_context,
        )
    return _baseline_resolve(envmap, compiler, hooks)
