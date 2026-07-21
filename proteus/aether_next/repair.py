"""Deterministic config repair for internally-inconsistent architect configs.

Instead of discarding a fatally-invalid RuntimeConfigIR and falling back to
the guaranteed default, this module iteratively applies targeted fixes for
each known fatal validation issue code.  The repaired IR is re-validated
each pass; if no repair changed the IR, the loop stops.
"""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from .runtime_ir import (
    BootstrapPolicy,
    HelperToolPolicy,
    ProcessPolicy,
    RefusalPolicy,
    RuntimeConfigIR,
)

if TYPE_CHECKING:
    from .compiler import ConfigCompiler
    from .runtime_ir import EnvMap, EvalIndex, ObjectiveGraph

_MAX_REPAIR_PASSES = 6
_MAX_CAPABILITIES = 10


def _dedupe_caps(caps: tuple[str, ...]) -> tuple[str, ...]:
    """De-duplicate capability IDs while preserving insertion order."""
    seen: set[str] = set()
    result: list[str] = []
    for cid in caps:
        if cid not in seen:
            seen.add(cid)
            result.append(cid)
    return tuple(result)


def _cap_available(compiler: ConfigCompiler, cid: str) -> bool:
    """Return True if *cid* is in the registry and marked available."""
    desc = compiler.registry.get(cid)
    return desc is not None and desc.available


def _add_cap(ir: RuntimeConfigIR, cid: str) -> RuntimeConfigIR:
    """Return a new IR with *cid* appended (de-duped) to selected_capabilities."""
    new_caps = _dedupe_caps(ir.selected_capabilities + (cid,))
    return replace(ir, selected_capabilities=new_caps)


def _repair_missing_service_probe(
    ir: RuntimeConfigIR,
    compiler: ConfigCompiler,
    codes: list[str],
) -> RuntimeConfigIR:
    if _cap_available(compiler, "service_probe"):
        codes.append("added:service_probe")
        return _add_cap(ir, "service_probe")
    codes.append("cleared:require_fresh_probe")
    return replace(ir, process_policy=replace(
        ir.process_policy, require_fresh_probe=False,
    ))


def _repair_missing_managed_process(
    ir: RuntimeConfigIR,
    compiler: ConfigCompiler,
    codes: list[str],
) -> RuntimeConfigIR:
    if _cap_available(compiler, "managed_process"):
        codes.append("added:managed_process")
        return _add_cap(ir, "managed_process")
    codes.append("downgraded:process_mode")
    return replace(ir, process_policy=replace(
        ir.process_policy, mode="stateless_shell",
    ))


def _repair_missing_bootstrap_substrate(
    ir: RuntimeConfigIR,
    compiler: ConfigCompiler,
    codes: list[str],
) -> RuntimeConfigIR:
    if _cap_available(compiler, "shell"):
        codes.append("added:shell")
        return _add_cap(ir, "shell")
    codes.append("cleared:allow_acquisition")
    return replace(ir, bootstrap_policy=replace(
        ir.bootstrap_policy, allow_acquisition=False,
    ))


def _repair_missing_helper_tool_substrate(
    ir: RuntimeConfigIR,
    compiler: ConfigCompiler,
    codes: list[str],
) -> RuntimeConfigIR:
    selected = set(ir.selected_capabilities)
    needed = {"filesystem", "shell"}
    any_unavailable = False
    for cid in needed:
        if cid not in selected:
            if _cap_available(compiler, cid):
                codes.append(f"added:{cid}")
                ir = _add_cap(ir, cid)
            else:
                any_unavailable = True
    if any_unavailable:
        codes.append("cleared:allow_creation")
        ir = replace(ir, helper_tool_policy=replace(
            ir.helper_tool_policy, allow_creation=False,
        ))
    return ir


def _repair_unsafe_refusal_boundary(
    ir: RuntimeConfigIR,
    codes: list[str],
) -> RuntimeConfigIR:
    codes.append("cleared:allowed_local_categories")
    return replace(ir, refusal_policy=replace(
        ir.refusal_policy, allowed_local_categories=(),
    ))


def _repair_invalid_process_mode(
    ir: RuntimeConfigIR,
    codes: list[str],
) -> RuntimeConfigIR:
    codes.append("downgraded:process_mode")
    return replace(ir, process_policy=replace(
        ir.process_policy, mode="stateless_shell",
    ))


def _repair_too_many_capabilities(
    ir: RuntimeConfigIR,
    codes: list[str],
) -> RuntimeConfigIR:
    codes.append("trimmed:capabilities")
    return replace(ir, selected_capabilities=ir.selected_capabilities[:_MAX_CAPABILITIES])


def _repair_no_or_all_invalid_capabilities(
    ir: RuntimeConfigIR,
    compiler: ConfigCompiler,
    codes: list[str],
) -> RuntimeConfigIR:
    available = compiler.registry.available_ids()
    preferred = [cid for cid in ("shell", "filesystem") if cid in available]
    if preferred:
        codes.append("reset:capabilities")
        return replace(ir, selected_capabilities=_dedupe_caps(tuple(preferred)))
    fallback = available[:4] if available else ("shell", "filesystem")
    codes.append("reset:capabilities")
    return replace(ir, selected_capabilities=_dedupe_caps(tuple(fallback)))


# Maps fatal issue codes to their repair functions.
# Each function signature: (ir, compiler, codes) -> ir  OR  (ir, codes) -> ir
_REPAIR_DISPATCH: dict[str, str] = {
    "missing_service_probe": "_repair_missing_service_probe",
    "missing_managed_process": "_repair_missing_managed_process",
    "missing_bootstrap_substrate": "_repair_missing_bootstrap_substrate",
    "missing_helper_tool_substrate": "_repair_missing_helper_tool_substrate",
    "unsafe_refusal_boundary": "_repair_unsafe_refusal_boundary",
    "invalid_process_mode": "_repair_invalid_process_mode",
    "too_many_capabilities": "_repair_too_many_capabilities",
    "no_capabilities_selected": "_repair_no_or_all_invalid_capabilities",
    "all_capabilities_invalid": "_repair_no_or_all_invalid_capabilities",
}


def repair_config(
    ir: RuntimeConfigIR,
    compiler: ConfigCompiler,
    envmap: EnvMap,
    *,
    objective_graph: ObjectiveGraph | None = None,
    eval_index: EvalIndex | None = None,
) -> tuple[RuntimeConfigIR, tuple[str, ...]]:
    """Iteratively repair repairable fatal validation issues.

    Returns the repaired IR and the tuple of repair codes applied
    (e.g. ``("added:service_probe",)``).
    """
    all_codes: list[str] = []

    for _ in range(_MAX_REPAIR_PASSES):
        issues = compiler.validate(
            ir, envmap,
            objective_graph=objective_graph,
            eval_index=eval_index,
        )
        fatal_codes = [issue.code for issue in issues if issue.fatal]
        if not fatal_codes:
            break

        changed = False
        for code in fatal_codes:
            fn_name = _REPAIR_DISPATCH.get(code)
            if fn_name is None:
                continue
            before = ir
            codes_before = len(all_codes)
            fn = globals()[fn_name]
            # Dispatch: some repairs need the compiler, some don't.
            if fn_name in (
                "_repair_unsafe_refusal_boundary",
                "_repair_invalid_process_mode",
                "_repair_too_many_capabilities",
            ):
                ir = fn(ir, all_codes)
            else:
                ir = fn(ir, compiler, all_codes)
            if ir is not before or len(all_codes) > codes_before:
                changed = True

        if not changed:
            break

    return ir, tuple(all_codes)
