#!/usr/bin/env python3
"""Verifier replay A/B over prior Aether-Next traces.

This is a trace-derived verifier replay, not a container/task rerun. It rebuilds
ledger state from recorded trace observations, injects current automatic-memory
receipts while replaying historical actions, then compares generic verifier
prompt packets against architect-authored verifier prompt packets.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
import sys
from typing import Any, Mapping

_BUILD_DIR = str(Path(__file__).resolve().parent)
if _BUILD_DIR not in sys.path:
    sys.path.insert(0, _BUILD_DIR)

from aether_next.automatic_memory import automatic_memory_receipt  # noqa: E402
from aether_next.ledger import ExecutionLedger, Receipt  # noqa: E402
from aether_next.model_hooks import DEFAULT_VERIFIER_IDENTITY_PROMPT as VERIFIER_SYSTEM_PROMPT  # noqa: E402
from aether_next.providers.azure_model import make_azure_callable  # noqa: E402
from aether_next.runtime_ir import (  # noqa: E402
    ACTION_SCHEMA,
    CapabilityDescriptor,
    CompiledRuntime,
    ContextPolicy,
    EnvMap,
    EvalIndex,
    ProcessPolicy,
    HelperToolPolicy,
    BootstrapPolicy,
    CompletionPolicy,
    RefusalPolicy,
    ReconfigurePolicy,
    ModelVerifierPolicy,
    ObjectiveGraph,
    ActionRequest,
    CheckSpec,
    DeliverableSpec,
    ProofObligation,
    normalize_relpath,
)
from aether_next.verifier import ModelVerifierResult, parse_model_verifier_result  # noqa: E402
from aether_next.verifier_packets import build_verifier_packet  # noqa: E402
from aether_next.workbench_config import parse_harness_config_ir  # noqa: E402


DEFAULT_CASES = (
    ("filter-js-from-html", str(Path(_BUILD_DIR) / "narrow_real_task_traces_20260630_043152/filter-js-from-html.trace.json")),
    ("sparql-university", str(Path(_BUILD_DIR) / "narrow_real_task_traces_20260630_043152/sparql-university.trace.json")),
    ("openssl-selfsigned-cert", str(Path(_BUILD_DIR) / "narrow_real_task_traces_20260630_043152/openssl-selfsigned-cert.trace.json")),
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _trace_root(path: Path) -> dict[str, Any]:
    data = _load_json(path)
    trace = data.get("trace", data)
    if not isinstance(trace, dict):
        raise ValueError(f"{path} does not contain a trace object")
    return trace


def _section(trace: Mapping[str, Any], name: str) -> str:
    prefix = f"[{name}]"
    for item in trace.get("prefix_messages", []) or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", ""))
        if content.startswith(prefix):
            return content.split("\n", 1)[1] if "\n" in content else ""
    return ""


def _load_architect_configs() -> dict[str, Any]:
    configs: dict[str, Any] = {}
    for rel in (
        "architect_only_eval_architect_skill_15_v5_32k_memory_clean/architect_only_eval.json",
        "architect_only_eval_architect_skill_remaining4_v8_auto_memory/architect_only_eval.json",
        "architect_only_eval_architect_skill_financial_v9_48k/architect_only_eval.json",
    ):
        path = Path(_BUILD_DIR) / rel
        if not path.exists():
            continue
        for row in _load_json(path):
            task = str(row.get("task", "")).strip()
            config = row.get("workbench_config")
            if task and isinstance(config, dict):
                configs[task] = config
    return configs


def _architect_prompt_for(task: str, configs: Mapping[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], str]:
    config = configs.get(task)
    if not isinstance(config, dict):
        return "", (), (), (), "missing_architect_config"
    try:
        parsed = parse_harness_config_ir(json.dumps(_strip_historical_config_metadata(config)))
    except Exception as exc:
        return "", (), (), (), f"unparseable_architect_config:{exc}"
    return (
        parsed.verifier_system_prompt.render(),
        parsed.evidence_requirements,
        parsed.false_positive_risks,
        parsed.minimum_completion_evidence,
        "",
    )


def _strip_historical_config_metadata(config: Mapping[str, Any]) -> dict[str, Any]:
    """Remove recorder-owned metadata from saved architect eval configs.

    Live architect outputs must still fail on unsupported fields. This replay
    script reads historical configs that were saved after parser repair/audit
    decoration, so these fields are evidence metadata, not architect intent.
    """
    cleaned = dict(config)
    for key in ("repair_warnings", "repair_warning_codes", "rejected_config_items"):
        cleaned.pop(key, None)
    model_verifier_policy = cleaned.get("model_verifier_policy")
    if isinstance(model_verifier_policy, dict):
        normalized_policy = dict(model_verifier_policy)
        normalized_policy["runs_on"] = ["solver_submit"]
        cleaned["model_verifier_policy"] = normalized_policy
    return cleaned


def _objective_from_trace(trace: Mapping[str, Any]) -> ObjectiveGraph:
    raw = _section(trace, "objective_graph")
    if raw:
        try:
            data = json.loads(raw)
            deliverables = tuple(
                DeliverableSpec(
                    path=str(item.get("path", "")),
                    required=bool(item.get("required", True)),
                    description=str(item.get("description", "")),
                )
                for item in data.get("deliverables", []) or []
                if isinstance(item, dict)
            )
            obligations = tuple(
                ProofObligation(
                    obligation_id=str(item.get("obligation_id", "")),
                    kind=str(item.get("kind", "")),
                    description=str(item.get("description", "")),
                    target=str(item.get("target", "")),
                )
                for item in data.get("obligations", []) or []
                if isinstance(item, dict) and str(item.get("obligation_id", "")).strip()
            )
            return ObjectiveGraph(
                deliverables=deliverables,
                protected_paths=tuple(data.get("protected_paths", ()) or ()),
                allowed_edit_roots=tuple(data.get("allowed_edit_roots", (".",)) or (".",)),
                obligations=obligations or tuple(
                    ProofObligation(f"artifact:{item.path}", "artifact", f"required artifact {item.path}", item.path)
                    for item in deliverables
                    if item.required
                ),
            )
        except Exception:
            pass
    return ObjectiveGraph()


def _eval_index_from_trace(trace: Mapping[str, Any]) -> EvalIndex:
    raw = _section(trace, "eval_index")
    if not raw:
        return EvalIndex()
    try:
        data = json.loads(raw)
    except Exception:
        return EvalIndex()
    if isinstance(data, dict):
        items = data.get("checks", []) or []
    else:
        items = data if isinstance(data, list) else []
    checks = []
    for item in items:
        if not isinstance(item, dict):
            continue
        check_id = str(item.get("check_id", "")).strip()
        command = str(item.get("command", "")).strip()
        if not check_id or not command:
            continue
        checks.append(CheckSpec(
            check_id=check_id,
            label=str(item.get("label", command)),
            command=command,
            origin=str(item.get("origin", "trace")),
            authoritative=bool(item.get("authoritative", True)),
        ))
    return EvalIndex(tuple(checks))


def _action_schema_from_trace(trace: Mapping[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    raw = _section(trace, "action_schema")
    if not raw:
        return ACTION_SCHEMA
    try:
        data = json.loads(raw)
    except Exception:
        return ACTION_SCHEMA
    if not isinstance(data, dict):
        return ACTION_SCHEMA
    return tuple(
        (str(name), tuple(str(arg) for arg in args))
        for name, args in sorted(data.items())
        if isinstance(args, list)
    )


def _compiled_from_trace(
    *,
    task: str,
    trace: Mapping[str, Any],
    verifier_prompt: str,
    evidence_requirements: tuple[str, ...],
    false_positive_risks: tuple[str, ...],
    minimum_completion_evidence: tuple[str, ...],
) -> CompiledRuntime:
    task_prompt = _section(trace, "task_prompt")
    config = trace.get("architect_config", {})
    if not isinstance(config, dict):
        config = {}
    objective = _objective_from_trace(trace)
    eval_index = _eval_index_from_trace(trace)
    action_schema = _action_schema_from_trace(trace)
    return CompiledRuntime(
        task_prompt=task_prompt,
        env_digest=f"trace:{task}",
        objective_graph=objective,
        eval_index=eval_index,
        selected_capabilities=(
            CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
            CapabilityDescriptor("shell", "Run shell", tool_names=("run_command",)),
        ),
        stable_prefix_sections=(),
        context_policy=ContextPolicy(),
        process_policy=ProcessPolicy(),
        helper_tool_policy=HelperToolPolicy(),
        bootstrap_policy=BootstrapPolicy(),
        completion_policy=CompletionPolicy(),
        refusal_policy=RefusalPolicy(),
        reconfigure_policy=ReconfigurePolicy(),
        enforced_monitors=(),
        check_plan_ids=tuple(check.check_id for check in eval_index.checks),
        forbidden_paths=tuple(config.get("forbidden_paths", ()) or ()),
        model_verifier_policy=ModelVerifierPolicy(enabled=True, runs_on=("solver_submit", "deterministic_success_candidate", "deterministic_failure")),
        action_schema=action_schema,
        solver_identity_prompt=str(config.get("solver_identity_prompt", "")),
        success_definition=str(config.get("success_definition", "")),
        local_verification_limits=tuple(str(item) for item in config.get("local_verification_limits", ()) or ()),
        verifier_identity_prompt=verifier_prompt,
        evidence_requirements=evidence_requirements,
        false_positive_risks=false_positive_risks,
        minimum_completion_evidence=minimum_completion_evidence,
        config_realization={
            "architect_path": "trace_replay",
            "verifier_prompt_inserted": bool(verifier_prompt.strip()),
            "verifier_system_prompt_summary": verifier_prompt[:500],
            "evidence_requirements": list(evidence_requirements),
            "false_positive_risks": list(false_positive_risks),
            "minimum_completion_evidence": list(minimum_completion_evidence),
        },
    )

def _receipt_from_observation(obs: Mapping[str, Any], *, step: int, fallback_id: str) -> Receipt:
    kind = str(obs.get("kind", ""))
    summary = str(obs.get("summary", ""))
    payload: dict[str, Any] = {}
    path = str(obs.get("path", "")).strip()
    if path:
        payload["path"] = normalize_relpath(path, "/app")
    if obs.get("exit_code") is not None:
        payload["exit_code"] = obs.get("exit_code")
    if obs.get("stdout_tail"):
        payload["stdout"] = str(obs.get("stdout_tail", ""))
    if obs.get("stderr_tail"):
        payload["stderr"] = str(obs.get("stderr_tail", ""))
    if kind == "run_command" and "command exit=" in summary:
        _, _, command = summary.partition(":")
        payload["command"] = command.strip()
    if kind == "write_file" and path:
        payload["artifact_paths"] = (payload["path"],)
        payload["modified_paths"] = (payload["path"],)
    return Receipt(
        receipt_id=str(obs.get("receipt_id", "") or fallback_id),
        step=step,
        kind=kind,
        success=bool(obs.get("success")),
        summary=summary,
        state_change=kind in {"write_file", "check_result", "schema_validation"} and bool(obs.get("success")),
        failure_class=str(obs.get("failure_class", "")),
        payload=payload,
    )


def _action_from_trace(action: Mapping[str, Any]) -> ActionRequest:
    return ActionRequest(
        action_id=str(action.get("action_id", "trace-action")),
        kind=str(action.get("kind", "")),
        capability_id=str(action.get("capability_id", "trace")),
        arguments=dict(action.get("arguments", {}) or {}),
        intent=str(action.get("intent", "trace replay")),
        expected_observation=str(action.get("expected_observation", "trace replay")),
        if_fail_next=str(action.get("if_fail_next", "trace replay")),
    )


def _ledger_from_trace(trace: Mapping[str, Any], *, replay_step: int) -> ExecutionLedger:
    ledger = ExecutionLedger()
    ledger.ensure_objective(_objective_from_trace(trace))
    envmap = EnvMap(task_prompt=_section(trace, "task_prompt"), workspace_root="/app")
    for item in trace.get("steps", []) or []:
        if not isinstance(item, dict):
            continue
        step = int(item.get("step", -1))
        if step >= replay_step:
            continue
        turn = item.get("turn", {})
        actions = turn.get("actions", []) if isinstance(turn, dict) else []
        for action_data in actions or []:
            if not isinstance(action_data, dict):
                continue
            action = _action_from_trace(action_data)
            automatic = automatic_memory_receipt(action, step=step, envmap=envmap, ledger=ledger)
            if automatic is not None:
                ledger.record(automatic)
        for idx, obs in enumerate(item.get("observations", []) or []):
            if isinstance(obs, dict):
                ledger.record(_receipt_from_observation(obs, step=step, fallback_id=f"trace-{step}-{idx}"))
    return ledger


def _fake_output(
    packet: Mapping[str, Any], *, variant: str, architect_prompt: str = "",
) -> dict[str, Any]:
    text = json.dumps(packet, sort_keys=True).lower()
    has_arch = variant == "architect_prompt" and bool(architect_prompt.strip())
    has_query_loop = "memory_loop_feedback" in packet and packet.get("memory_loop_feedback")
    has_auto = "automatic_memory_findings" in packet and packet.get("automatic_memory_findings")
    failed_checks = packet.get("failed_or_empty_checks") or packet.get("deterministic_checks") or []
    if has_arch and (has_query_loop or has_auto or "no_progress" in text):
        evidence = []
        if has_query_loop:
            evidence.append("memory_loop_feedback shows repeated memory queries without new evidence")
        if has_auto:
            evidence.append("automatic_memory_findings show repeated target evidence")
        if failed_checks:
            evidence.append("deterministic checks are failed, missing, or empty")
        return {
            "verdict": "needs_repair",
            "confidence": "high",
            "summary": "Trace replay evidence shows no-progress behavior that should block completion.",
            "findings": [{
                "finding_id": "vf-trace-replay-no-progress",
                "summary": "The solver is repeating prior work or memory lookups instead of producing new task evidence.",
                "evidence": evidence or ["trace replay packet shows no-progress state"],
                "repair_instruction": "Use the surfaced evidence to change strategy: write or repair the target artifact, run a relevant check once, or request reconfiguration if a real capability is missing.",
                "applies_to": ["solver_loop", "task_artifact"],
            }],
        }
    if has_query_loop or has_auto:
        return {
            "verdict": "uncertain_missing_evidence",
            "confidence": "medium",
            "summary": "The packet suggests repeated work, but the generic verifier prompt does not provide a task-specific repair bar.",
            "missing_evidence_requests": ["Provide task-specific artifact/check evidence after the repeated action."],
        }
    return {
        "verdict": "uncertain_missing_evidence",
        "confidence": "medium",
        "summary": "Trace replay packet lacks enough verifier evidence for a completion verdict.",
        "missing_evidence_requests": ["Provide final artifact evidence and check results."],
    }


def _model_messages(packet: Mapping[str, Any], *, architect_prompt: str) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": VERIFIER_SYSTEM_PROMPT}]
    if architect_prompt:
        messages.append({"role": "system", "content": "[architect_verifier_prompt]\n" + architect_prompt})
    messages.append({"role": "user", "content": json.dumps({"verifier_packet": packet}, sort_keys=True, default=str)})
    return messages


def _model_output(packet: Mapping[str, Any], *, architect_prompt: str) -> str:
    model = make_azure_callable(
        deployment_env="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT54_MINI_KEY",
        endpoint_env="AZURE_OPENAI_ENDPOINT",
        effort=os.environ.get("AETHER_VERIFIER_EFFORT", "high"),
        poll_interval_s=float(os.environ.get("AETHER_VERIFIER_POLL_INTERVAL_S", "5")),
        poll_timeout_s=float(os.environ.get("AETHER_VERIFIER_POLL_TIMEOUT_S", "900")),
    )
    max_output_tokens = int(os.environ.get("AETHER_TRACE_VERIFIER_MAX_OUTPUT_TOKENS", "6000"))
    return model(_model_messages(packet, architect_prompt=architect_prompt), max_output_tokens=max_output_tokens)


def _judge(parsed: ModelVerifierResult | None) -> dict[str, Any]:
    if parsed is None:
        return {"parse_ok": False, "evidence_bound": False, "actionable": False, "specific_repair": False}
    text = json.dumps(parsed.as_dict(), sort_keys=True).lower()
    return {
        "parse_ok": True,
        "evidence_bound": any(term in text for term in ("packet", "check", "artifact", "memory", "repeat", "evidence")),
        "actionable": bool(parsed.missing_evidence_requests) or any(f.repair_instruction for f in parsed.findings),
        "specific_repair": any(f.repair_instruction and ("write" in f.repair_instruction.lower() or "check" in f.repair_instruction.lower()) for f in parsed.findings),
    }


def _run_one(task: str, trace_path: Path, *, mode: str, out_dir: Path, configs: Mapping[str, Any]) -> dict[str, Any]:
    trace = _trace_root(trace_path)
    replay_step = len(trace.get("steps", []) or [])
    architect_prompt, evidence_requirements, false_positive_risks, min_evidence, config_error = _architect_prompt_for(task, configs)
    variants = []
    for variant in ("generic", "architect_prompt"):
        prompt = architect_prompt if variant == "architect_prompt" else ""
        compiled = _compiled_from_trace(
            task=task,
            trace=trace,
            verifier_prompt=prompt,
            evidence_requirements=evidence_requirements if prompt else (),
            false_positive_risks=false_positive_risks if prompt else (),
            minimum_completion_evidence=min_evidence if prompt else (),
        )
        ledger = _ledger_from_trace(trace, replay_step=replay_step)
        replay_envmap = EnvMap(
            task_prompt=_section(trace, "task_prompt"),
            workspace_root="/app",
        )
        packet = build_verifier_packet(
            compiled, ledger, step=replay_step, reason="max_steps",
            envmap=replay_envmap,
        )
        raw: Any
        parse_error = ""
        parsed: ModelVerifierResult | None = None
        if variant == "architect_prompt" and config_error:
            raw = {"blocked": config_error}
            parse_error = config_error
        else:
            raw = (
                _fake_output(packet, variant=variant, architect_prompt=prompt)
                if mode == "fake"
                else _model_output(packet, architect_prompt=prompt)
            )
            try:
                parsed = parse_model_verifier_result(raw)
                ledger.apply_verifier_result(parsed, step=replay_step)
            except Exception as exc:
                parse_error = str(exc)
        judgement = _judge(parsed)
        variant_dir = out_dir / task / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        (variant_dir / "verifier_packet.json").write_text(json.dumps(packet, indent=2, sort_keys=True, default=str))
        (variant_dir / "verifier_prompt.txt").write_text(prompt, encoding="utf-8")
        (variant_dir / "raw_output.json").write_text(json.dumps(raw, indent=2, sort_keys=True, default=str) if not isinstance(raw, str) else raw)
        (variant_dir / "parsed_result.json").write_text(json.dumps(parsed.as_dict() if parsed else {"parse_error": parse_error}, indent=2, sort_keys=True))
        (variant_dir / "active_findings_after.json").write_text(json.dumps(ledger.active_finding_context(replay_step + 1), indent=2, sort_keys=True))
        (variant_dir / "judgement.json").write_text(json.dumps(judgement, indent=2, sort_keys=True))
        variants.append({
            "variant": variant,
            "verdict": parsed.verdict if parsed else "parse_error",
            "finding_count": len(parsed.findings) if parsed else 0,
            "missing_evidence_requests": len(parsed.missing_evidence_requests) if parsed else 0,
            "architect_prompt_present": bool(prompt),
            "config_error": config_error if variant == "architect_prompt" else "",
            **judgement,
        })
    generic, architect = variants
    return {
        "task": task,
        "trace": str(trace_path),
        "replay_step": replay_step,
        "status": "ok" if not architect.get("config_error") else "blocked",
        "variants": variants,
        "architect_prompt_improved": bool(
            architect.get("parse_ok")
            and architect.get("evidence_bound")
            and architect.get("actionable")
            and (
                int(architect.get("finding_count", 0)) > int(generic.get("finding_count", 0))
                or bool(architect.get("specific_repair")) and not bool(generic.get("specific_repair"))
            )
        ),
        "prompt_only_invention_blocked": bool(
            architect.get("parse_ok")
            and generic.get("parse_ok")
            and architect.get("verdict") == generic.get("verdict")
            and int(architect.get("finding_count", 0)) == int(generic.get("finding_count", 0))
            and not architect.get("config_error")
        ),
    }


def run(*, mode: str, out_dir: Path, cases: tuple[tuple[str, str], ...] = DEFAULT_CASES) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    configs = _load_architect_configs()
    rows = [_run_one(task, Path(trace), mode=mode, out_dir=out_dir, configs=configs) for task, trace in cases]
    summary = {
        "schema_version": "aether_next.trace_verifier_replay_ab.v1",
        "mode": mode,
        "rows": rows,
        "counts": {
            "cases": len(rows),
            "ok": sum(1 for row in rows if row["status"] == "ok"),
            "architect_prompt_improved": sum(1 for row in rows if row["architect_prompt_improved"]),
            "prompt_only_invention_blocked": sum(1 for row in rows if row["prompt_only_invention_blocked"]),
        },
    }
    (out_dir / "trace_verifier_replay_ab.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    report = ["# Trace Verifier Replay A/B", "", f"Mode: `{mode}`", ""]
    report.append("| task | generic verdict | architect verdict | improved | notes |")
    report.append("|---|---|---|---:|---|")
    for row in rows:
        variants = {item["variant"]: item for item in row["variants"]}
        notes = variants["architect_prompt"].get("config_error") or ""
        report.append(
            f"| {row['task']} | {variants['generic']['verdict']} | {variants['architect_prompt']['verdict']} | "
            f"{row['architect_prompt_improved']} | {notes} |"
        )
    (out_dir / "TRACE_VERIFIER_REPLAY_AB_REPORT.md").write_text("\n".join(report) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fake", "model"), default="fake")
    parser.add_argument("--out-dir", default="trace_verifier_replay_ab_fake")
    args = parser.parse_args()
    print(json.dumps(run(mode=args.mode, out_dir=Path(args.out_dir)), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
