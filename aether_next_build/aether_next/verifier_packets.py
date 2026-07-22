"""Evidence packet construction for model-led verification."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from hashlib import sha256
from typing import Any, Mapping

from .runtime_ir import stable_json

from .ledger import ExecutionLedger
from .runtime_ir import CompiledRuntime
from .task_contract import TaskContract


class _FrozenDict(dict):
    """JSON-serialisable immutable mapping used for model-facing packets."""

    def _blocked(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("verifier packet is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _blocked


class _FrozenList(list):
    """JSON-serialisable immutable sequence used inside a verifier packet."""

    def _blocked(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("verifier packet is immutable")

    __setitem__ = __delitem__ = append = extend = insert = pop = remove = reverse = sort = _blocked


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenDict({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


def _merged_config_realization(compiled: CompiledRuntime, ledger: ExecutionLedger) -> dict[str, Any]:
    realization = dict(compiled.config_realization)
    latest = ledger.latest_receipt("config_realization")
    if latest is not None:
        payload = latest.payload.get("config_realization", {})
        if isinstance(payload, dict):
            realization.update(payload)
    return realization


def _local_verification_limits(compiled: CompiledRuntime, realization: dict[str, Any]) -> list[dict[str, str]]:
    structured = realization.get("local_verification_limits")
    if isinstance(structured, list):
        normalized: list[dict[str, str]] = []
        for item in structured:
            if not isinstance(item, dict):
                continue
            statement = str(item.get("statement", "")).strip()
            if not statement:
                continue
            normalized.append({
                "source": str(item.get("source", "runtime_config")).strip() or "runtime_config",
                "statement": statement,
            })
        if normalized:
            return normalized
    return [
        {"source": "runtime_config", "statement": item}
        for item in compiled.local_verification_limits
        if str(item).strip()
    ]


def _config_realization_summary(realization: dict[str, Any]) -> dict[str, Any]:
    context_policy = realization.get("configured_context_policy")
    if not isinstance(context_policy, dict):
        context_policy = {
            "mode": realization.get("context_policy_mode", ""),
            "include_sections": realization.get("context_sections_declared", []),
            "compression_trigger_ratio": realization.get("context_compression_ratio"),
        }
    verification_policy = realization.get("configured_verification_policy")
    if not isinstance(verification_policy, dict):
        verification_policy = {
            "check_plan_ids": realization.get("checks_compiled", []),
        }
    verification_authority = realization.get("verification_authority")
    if isinstance(verification_authority, dict):
        official_grader = str(verification_authority.get("official_grader", "")).strip()
        if official_grader and "official_grader_authority" not in verification_policy:
            verification_policy = dict(verification_policy)
            verification_policy["official_grader_authority"] = official_grader

    summary = {
        "architect_path": realization.get("architect_path", ""),
        "tools_visible_to_solver": realization.get("tools_visible_to_solver", []),
        "tools_runtime_allowed": realization.get("tools_runtime_allowed", []),
        "context_policy": context_policy,
        "verification_policy": verification_policy,
    }
    optional_keys = (
        "harness_config_schema_version",
        "harness_config_realization_audit",
        "workbench_repair_warning_codes",
        "workbench_repair_warnings",
        "workbench_rejected_config_items",
        "verification_authority",
    )
    for key in optional_keys:
        if key in realization:
            summary[key] = realization[key]
    return summary


def _raw_state_candidates(realization: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    raw = realization.get("verifier_raw_state_candidates", ())
    if not isinstance(raw, list):
        return rows
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        rows.append({
            "path": path,
            "source": str(item.get("source", "config_realization")).strip() or "config_realization",
            "authority": "candidate_only",
        })
    return rows[:8]


def _task_contract_payload(compiled: CompiledRuntime, contract: TaskContract | None) -> dict[str, Any]:
    """Return immutable task truth in one explicitly scoped payload.

    The production kernel currently compiles from ``EnvMap`` and therefore
    does not always have a clause extractor result at this boundary.  When a
    typed :class:`TaskContract` is available it is the sole authority.  The
    compatibility fallback keeps the prompt inside the contract envelope and
    never exposes it as an independent model-facing field.
    """
    if contract is not None:
        return deepcopy(contract.as_payload())
    clauses = []
    for obligation in compiled.objective_graph.obligations:
        clauses.append({
            "clause_id": str(obligation.obligation_id),
            "text": str(obligation.description),
            "exact_atoms": [str(obligation.target)] if str(obligation.target).strip() else [],
        })
    if not clauses:
        clauses.append({
            "clause_id": "compiled:objective",
            "text": str(compiled.success_definition or "compiled objective"),
            "exact_atoms": [],
        })
    return {"raw_task_prompt": str(compiled.task_prompt), "clauses": clauses}


def _stable_envmap_payload(envmap: Any | None, compiled: CompiledRuntime) -> dict[str, Any]:
    if envmap is not None and hasattr(envmap, "to_payload"):
        return deepcopy(envmap.to_payload())
    if envmap is not None and isinstance(envmap, Mapping):
        facts = deepcopy(dict(envmap))
    elif envmap is not None and hasattr(envmap, "workspace_root"):
        capabilities: dict[str, Any] = {}
        for key, value in sorted(dict(getattr(envmap, "capabilities", {}) or {}).items()):
            capabilities[str(key)] = asdict(value) if is_dataclass(value) else deepcopy(value)
        facts = {
            "workspace_root": str(getattr(envmap, "workspace_root")),
            "visible_files": sorted(str(item) for item in getattr(envmap, "visible_files", ()) or ()),
            "visible_dirs": sorted(str(item) for item in getattr(envmap, "visible_dirs", ()) or ()),
            "capabilities": capabilities,
            "services": deepcopy(dict(getattr(envmap, "services", {}) or {})),
            "resource_limits": deepcopy(dict(getattr(envmap, "resource_limits", {}) or {})),
            "permissions": deepcopy(dict(getattr(envmap, "permissions", {}) or {})),
            "interactive_features": deepcopy(dict(getattr(envmap, "interactive_features", {}) or {})),
            "network_scope": str(getattr(envmap, "network_scope", "unknown")),
            "file_tree": str(getattr(envmap, "file_tree", "")),
            "file_map_summary": deepcopy(dict(getattr(envmap, "file_map_summary", {}) or {})),
        }
    else:
        raise ValueError("stable EnvMap payload unavailable; refusing digest-only verifier state")
    from .world import StableEnvMap
    return StableEnvMap.create(facts).to_payload()


_STATE_TOP_LEVEL_KEYS = frozenset({
    "schema_version", "state_version", "installed_packages", "runtime_facts",
    "files", "services", "jobs", "artifacts", "processes", "named_sections",
    "latest_result", "active_findings", "removed_services", "removed_jobs",
})
_STATE_FORBIDDEN_KEYS = frozenset({
    "architect_verifier_prompt", "architect_prompt", "verifier_prompt", "verifier_system_prompt",
    "solver_prompt", "solver_system_prompt", "task_prompt", "architect_strategy",
    "solver_journey", "journey", "strategy", "verifier_strategy", "recent_actions",
    "recent_receipts", "recent_command_receipts", "command_results", "stdout", "stderr",
    "content", "raw_output", "raw_log", "command", "solver_reported_blockers",
})
_NORMALIZED_FORBIDDEN_KEYS = frozenset(
    "".join(character for character in key.lower() if character.isalnum())
    for key in _STATE_FORBIDDEN_KEYS
) | frozenset({
    "solverjourney", "solvernotes", "commandresult", "commandresults",
    "commandoutput", "inspectionstrategy", "rawcommand", "rawcommands",
    "stdouttext", "stderrtext", "stdoutoutput", "stderroutput",
    "solverprompt", "verifierprompt", "architectprompt", "modelprompt",
    "transcript", "conversation", "prompt", "strategy", "journey",
})


def _normalized_key(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _state_only_projection(value: Any, *, key: str = "") -> Any:
    """Project supplied dynamic state to compact, non-journey metadata.

    The production world already emits compact snapshots, but this boundary is
    also called by compatibility adapters.  Do not trust an adapter to omit
    raw command output or model-authored journey fields.
    """
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            name = str(raw_key)
            if _normalized_key(name) in _NORMALIZED_FORBIDDEN_KEYS:
                continue
            result[name] = _state_only_projection(item, key=name)
        return result
    if isinstance(value, list):
        return [_state_only_projection(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_state_only_projection(item, key=key) for item in value]
    # Tool outputs may be captured as bytes (for example binary artifact or
    # subprocess streams).  Keep the packet JSON-serialisable and compact in
    # exactly the same way as large text values; the exact bytes remain
    # available through the receipt/output handle rather than being replayed
    # inline into the Verifier context.
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        return {
            "status": "present",
            "bytes": len(raw),
            "sha256": sha256(raw).hexdigest(),
        }
    if isinstance(value, str) and len(value) > 512:
        return {
            "status": "present",
            "chars": len(value),
            "sha256": sha256(value.encode("utf-8", "surrogateescape")).hexdigest(),
        }
    return deepcopy(value)


def _dynamic_state_payload(ledger: ExecutionLedger, dynamic_state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build compact current state without replaying receipt payloads."""
    if dynamic_state is not None:
        projected = _state_only_projection(dynamic_state)
        # Keep the state envelope explicit and reject adapter-only journey keys.
        return {
            str(key): value
            for key, value in projected.items()
            if str(key) in _STATE_TOP_LEVEL_KEYS
        }
    return {
        "schema_version": "dynamic_world_state.v1",
        "artifacts": {path: {"status": "present"} for path in sorted(ledger.current_artifacts())},
        "processes": ledger.live_processes(),
        "state_version": len(ledger.all_receipts()),
    }


def _collect_state_handles(ledger: ExecutionLedger) -> list[dict[str, Any]]:
    """Collect stable navigation handles, deduplicated by kind/handle."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for receipt in ledger.all_receipts():
        payload = receipt.payload or {}
        if payload.get("file_handle"):
            handle = str(payload["file_handle"])
            by_key.setdefault(("file", handle), {
                "kind": "file", "handle": handle,
                "path": str(payload.get("path", "")),
                "bytes": payload.get("bytes"),
                "content_hash": str(payload.get("content_hash", "")),
            })
        for key, stream in (("stdout_handle", "stdout"), ("stderr_handle", "stderr")):
            if payload.get(key):
                handle = str(payload[key])
                by_key.setdefault(("output", handle), {
                    "kind": "output", "handle": handle, "stream": stream,
                    "bytes": payload.get(f"{stream}_bytes", 0),
                    "content_hash": str(payload.get(f"{stream}_hash", "")),
                })
    for path in sorted(ledger.current_artifacts()):
        by_key.setdefault(("file", path), {"kind": "file", "handle": path, "path": path})
    return list(by_key.values())[-32:]


def build_verifier_packet(
    compiled: CompiledRuntime | None = None,
    ledger: ExecutionLedger | None = None,
    *,
    step: int = 0,
    reason: str = "",
    contract: TaskContract | None = None,
    envmap: Any | None = None,
    dynamic_state: Mapping[str, Any] | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Build an immutable, neutral state-only verifier packet.

    A submit action is only a trigger.  Solver prompts, journey narrative,
    command history/receipts, solver claims/blockers, verifier strategy/traps
    and model-authored proof are intentionally absent.  The packet contains
    only immutable task truth, stable environment facts, compact dynamic
    state, open obligations/findings, exact retrieval handles and compiled
    evidence requirements.
    """
    if compiled is None or ledger is None:
        raise TypeError("compiled and ledger are required")
    realization = _merged_config_realization(compiled, ledger)
    compiled_requirements = realization.get("compiled_evidence_requirements", ())
    if not isinstance(compiled_requirements, (list, tuple)):
        compiled_requirements = ()
    compiled_requirements = [
        deepcopy(item) for item in compiled_requirements if isinstance(item, Mapping)
    ]
    inspection_ceilings = realization.get("inspection_evidence_ceilings", {})
    if not isinstance(inspection_ceilings, Mapping):
        inspection_ceilings = {}
    packet = {
        "schema_version": "verifier_packet.v2",
        "snapshot_id": str(snapshot_id or f"step-{step}"),
        "reason": reason,
        "step": step,
        "task_contract": _task_contract_payload(compiled, contract),
        "stable_envmap": _stable_envmap_payload(envmap, compiled),
        "dynamic_state": _dynamic_state_payload(ledger, dynamic_state),
        "open_obligations": [item.as_dict() for item in ledger.open_obligations()],
        "active_findings": ledger.active_finding_context(step),
        "state_inspection_handles": _collect_state_handles(ledger),
        # These fields are present only when the compiler supplied a structured
        # semantic contract.  Plain prose evidence requirements remain
        # advisory; the verifier gate must never infer clause thresholds from
        # model-authored text.
        "compiled_evidence_requirements": compiled_requirements,
        "inspection_evidence_ceilings": deepcopy(dict(inspection_ceilings)),
        "evidence_requirements": {
            "required": list(compiled.evidence_requirements) or list(realization.get("evidence_requirements", []) or []),
            "minimum_completion": list(compiled.minimum_completion_evidence) or list(realization.get("minimum_completion_evidence", []) or []),
            "false_positive_risks": list(compiled.false_positive_risks) or list(realization.get("false_positive_risks", []) or []),
            "re_derivable_claims": list(compiled.re_derivable_claims) or list(realization.get("re_derivable_claims", []) or []),
            "local_limits": _local_verification_limits(compiled, realization),
        },
        "raw_state_candidates": _raw_state_candidates(realization),
    }
    forbidden = {
        "solver_claim",
        "submit_summary",
        "privileged_solver_proof",
        "solver_proof",
        "proof_contract",
        "proof_contract_analysis",
        "solver_authored_evidence",
        "recent_actions",
        "recent_receipts",
        "latest_file_reads",
        "command_results",
        "memory_loop_feedback",
        "automatic_memory_findings",
        "no_progress_controls",
        "artifact_history",
        "memory_events",
        "observations",
        "solver_system_prompt",
    }
    leaked = forbidden.intersection(packet)
    if leaked:
        raise AssertionError(f"verifier packet leaked solver journey fields: {sorted(leaked)}")
    return _freeze(packet)


def packet_state_signature(packet: Mapping[str, Any]) -> str:
    """Stable signature of the packet's MATERIAL state.

    Volatile bookkeeping (step counters, finding ages, handle ids embedding
    step numbers) is excluded: two packets with the same signature describe
    the same world, so re-judging the second is pure waste.
    """
    handles = []
    for handle in packet.get("state_inspection_handles") or ():
        if isinstance(handle, Mapping):
            handles.append({
                "kind": handle.get("kind"),
                "path": handle.get("path", ""),
                "stream": handle.get("stream", ""),
                "bytes": handle.get("bytes"),
                "content_hash": handle.get("content_hash", ""),
            })
    def _material(value: Any) -> Any:
        """Normalize packet content while dropping only volatile counters.

        Finding and obligation IDs are not sufficient identity: a verifier can
        revise the summary, evidence, status, or target while retaining the
        same ID.  Keep every material field in the signature, but ignore age
        bookkeeping so repeated judgments of unchanged state still coalesce.
        """
        if isinstance(value, Mapping):
            return {
                str(key): _material(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(key) not in {"age_steps", "stale_cycles"}
            }
        if isinstance(value, (list, tuple)):
            return [_material(item) for item in value]
        return deepcopy(value)

    material = {
        "task_contract": packet.get("task_contract", {}),
        "stable_envmap": packet.get("stable_envmap", {}),
        "dynamic_state": packet.get("dynamic_state", {}),
        "handles": handles,
        "obligations": sorted(
            (_material(o) for o in (packet.get("open_obligations") or ()) if isinstance(o, Mapping)),
            key=stable_json,
        ),
        "findings": sorted(
            (_material(f) for f in (packet.get("active_findings") or ()) if isinstance(f, Mapping)),
            key=stable_json,
        ),
        "evidence_requirements": packet.get("evidence_requirements", {}),
    }
    return sha256(stable_json(material).encode("utf-8")).hexdigest()[:16]
