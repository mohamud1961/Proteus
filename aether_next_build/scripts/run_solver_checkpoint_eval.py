#!/usr/bin/env python3
"""Plan or execute strict production-path Solver checkpoint evaluations."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

_BUILD_ROOT = Path(__file__).resolve().parents[1]
if str(_BUILD_ROOT) not in sys.path:
    sys.path.insert(0, str(_BUILD_ROOT))

from aether_next.compiler import CapabilityRegistry, ConfigCompiler  # noqa: E402
from aether_next.evidence_finalization import (  # noqa: E402
    executing_source_identity,
    finalize_evidence_directory,
    sha256_file,
)
from aether_next.kernel_solver_turn import handle_solver_parse_error  # noqa: E402
from aether_next.kernel_messages import build_solver_messages  # noqa: E402
from aether_next.ledger import ExecutionLedger  # noqa: E402
from aether_next.model_hooks import ModelHooks, ModelOutputError  # noqa: E402
from aether_next.providers.azure_model import make_azure_callable  # noqa: E402
from aether_next.redaction import redact_text_with_events  # noqa: E402
from aether_next.runtime_ir import (  # noqa: E402
    CapabilityDescriptor,
    EnvMap,
    RuntimeConfigIR,
    SolverTurn,
)


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected object in {path}")
    return payload


def _require_deterministic_pass(path: Path | None) -> dict[str, Any]:
    if path is None:
        raise RuntimeError("--deterministic-summary is required with --allow-model")
    payload = _load(path)
    if not bool(payload.get("passed", False)) or payload.get("required_failures"):
        raise RuntimeError("deterministic certification summary is not a clean pass")
    return payload


def _capabilities() -> dict[str, CapabilityDescriptor]:
    return {
        "shell": CapabilityDescriptor("shell", "Run commands", tool_names=("run_command",)),
        "filesystem": CapabilityDescriptor("filesystem", "Read and write files", tool_names=("read_file", "write_file")),
        "managed_process": CapabilityDescriptor("managed_process", "Manage processes", tool_names=("launch_process", "probe_service", "stop_process")),
        "service_probe": CapabilityDescriptor("service_probe", "Probe services", tool_names=("probe_service",)),
        "artifact_inspection": CapabilityDescriptor("artifact_inspection", "Inspect artifacts", tool_names=("inspect_artifact",)),
        "output_handle_retrieval": CapabilityDescriptor("output_handle_retrieval", "Read retained outputs", tool_names=("read_output", "grep_output")),
        "network_fetch": CapabilityDescriptor("network_fetch", "Acquire resources when authorised", tool_names=("bootstrap_acquire",)),
    }


def _compiled(case: Mapping[str, Any]):
    env = EnvMap(
        task_prompt=str(case["task_prompt"]),
        workspace_root="/app",
        capabilities=_capabilities(),
        network_scope="loopback_only",
    )
    ir = RuntimeConfigIR(
        architect_summary="Solver checkpoint evaluation runtime.",
        solver_identity_prompt=(
            "Act as the production Solver. Choose exactly one causal action from current evidence, "
            "or submit only when current evidence is coherent. Keep the decision commitment concise."
        ),
        selected_capabilities=tuple(_capabilities()),
        success_definition=str(case["task_prompt"]),
        evidence_requirements=("Use current model-visible evidence and preserve the observation boundary.",),
        minimum_completion_evidence=("Current independent evidence with no active findings.",),
    )
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)


def _normalise_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/")
    return text[5:] if text.startswith("/app/") else text.lstrip("./")


def _score_turn(turn: SolverTurn, expected: Mapping[str, Any]) -> dict[str, Any]:
    findings: list[str] = []
    expected_kind = str(expected.get("turn_kind", ""))
    if turn.kind != expected_kind:
        findings.append(f"turn kind {turn.kind!r} != expected {expected_kind!r}")

    actions = tuple(turn.actions)
    if expected_kind == "act" and len(actions) != 1:
        findings.append(f"expected exactly one action, observed {len(actions)}")
    if expected_kind == "submit_outcome" and actions:
        findings.append("submit_outcome carried actions")

    action = actions[0] if len(actions) == 1 else None
    allowed = {str(item) for item in expected.get("allowed_action_kinds", [])}
    forbidden = {str(item) for item in expected.get("forbidden_action_kinds", [])}
    if action is not None:
        if allowed and action.kind not in allowed:
            findings.append(f"action kind {action.kind!r} not in allowed {sorted(allowed)}")
        if action.kind in forbidden:
            findings.append(f"forbidden action kind selected: {action.kind}")
        args = dict(action.arguments)
        for key, value in (expected.get("argument_equals", {}) or {}).items():
            observed = args.get(key)
            if key == "path":
                equal = _normalise_path(observed) == _normalise_path(value)
            else:
                equal = str(observed) == str(value)
            if not equal:
                findings.append(f"argument {key}={observed!r} != {value!r}")
        for key, value in (expected.get("argument_contains", {}) or {}).items():
            if str(value) not in str(args.get(key, "")):
                findings.append(f"argument {key} does not contain {value!r}")
        for key, values in (expected.get("argument_any_contains", {}) or {}).items():
            observed = str(args.get(key, ""))
            if not any(str(value) in observed for value in values):
                findings.append(f"argument {key}={observed!r} contains none of {values!r}")
        forbidden_commands = {str(item).strip() for item in expected.get("forbidden_command_exact", [])}
        command = str(args.get("command", "")).strip()
        if command and command in forbidden_commands:
            findings.append(f"repeated forbidden command: {command}")

    commitment = {
        "summary": bool(str(turn.summary).strip()),
        "evidence_gap": bool(str(turn.evidence_gap).strip()),
        "intent": bool(action and str(action.intent).strip()) if expected_kind == "act" else True,
        "expected_observation": bool(action and str(action.expected_observation).strip()) if expected_kind == "act" else True,
        "if_fail_next": bool(action and str(action.if_fail_next).strip()) if expected_kind == "act" else True,
    }
    missing_commitment = [key for key, value in commitment.items() if not value]
    # These fields are useful diagnostic signals, but are deliberately not
    # production-schema requirements.  The runtime treats them as audit
    # metadata, so a board that marks them as protocol failures would be
    # measuring a stricter, evaluator-invented contract.
    commitment_findings = []
    if missing_commitment:
        commitment_findings.append("missing advisory commitment fields: " + ", ".join(missing_commitment))
    return {
        "passed": not findings,
        "findings": findings,
        "advisory_findings": commitment_findings,
        "commitment": commitment,
        "turn_kind": turn.kind,
        "action_kind": action.kind if action else "",
        "action_arguments": dict(action.arguments) if action else {},
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = len(rows)
    parse_valid = sum(1 for row in rows if row.get("parse_valid"))
    protocol_valid = sum(1 for row in rows if row.get("protocol_valid"))
    decisive = sum(1 for row in rows if row.get("score", {}).get("passed"))
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_case.setdefault(str(row["case_id"]), []).append(row)
    reliability = {
        case_id: sum(1 for row in case_rows if row.get("score", {}).get("passed"))
        for case_id, case_rows in sorted(by_case.items())
    }
    return {
        "attempt_count": attempts,
        "provider_validity": parse_valid / attempts if attempts else 0.0,
        "protocol_validity": protocol_valid / attempts if attempts else 0.0,
        "decisive_action_count": decisive,
        "decisive_action_rate": decisive / attempts if attempts else 0.0,
        "case_pass_counts": reliability,
        "all_cases_at_least_2_of_3": all(value >= 2 for value in reliability.values()),
        "strict_promotion": (
            attempts > 0
            and parse_valid == attempts
            and protocol_valid == attempts
            and decisive >= min(attempts, 22)
            and all(value >= 2 for value in reliability.values())
        ),
    }


def _solve_with_production_protocol_correction(
    hooks: ModelHooks,
    messages: list[dict[str, str]],
    compiled: Any,
) -> tuple[SolverTurn, ExecutionLedger, str]:
    """Exercise the kernel's bounded malformed-output contract for one turn."""
    ledger = ExecutionLedger()
    before_count = len(ledger.all_receipts())
    try:
        ledger.record_accounting(
            receipt_id="checkpoint:solver_provider_turn:1",
            step=1,
            counter="solver_provider_turns",
            event="primary_solver_call",
        )
        return hooks.solve(messages, compiled), ledger, ""
    except ModelOutputError as exc:
        turn = handle_solver_parse_error(
            hooks, exc, 1, compiled, messages, ledger, None, None, before_count,
        )
        if turn is None:
            raise RuntimeError("production protocol correction returned no turn") from exc
        return turn, ledger, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-file", default=str(_BUILD_ROOT / "evals" / "solver_checkpoints.v1.json"))
    parser.add_argument("--samples", type=int, default=None)
    parser.add_argument("--effort", choices=("none", "low", "medium", "high", "xhigh"), default=None)
    parser.add_argument("--deterministic-summary", default=None)
    parser.add_argument("--allow-model", action="store_true")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args(argv)

    cases_path = Path(args.cases_file).resolve()
    payload = _load(cases_path)
    rules = dict(payload.get("rules", {}) or {})
    cases = list(payload.get("cases", []) or [])
    samples = int(args.samples or rules.get("samples_per_case", 3))
    effort = str(args.effort or rules.get("default_effort", "low"))
    if samples < 1:
        parser.error("--samples must be >= 1")
    deterministic = None
    if args.allow_model:
        deterministic = _require_deterministic_pass(
            Path(args.deterministic_summary).resolve() if args.deterministic_summary else None
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = Path(args.output_dir).resolve() if args.output_dir else _BUILD_ROOT / f"solver_checkpoint_eval_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    source = executing_source_identity(_BUILD_ROOT)
    plan = {
        "schema": "aether.solver_checkpoint_plan.v1",
        "cases_file": str(cases_path),
        "cases_file_sha256": sha256_file(cases_path),
        "case_ids": [case["id"] for case in cases],
        "samples": samples,
        "effort": effort,
        "model_execution_requested": bool(args.allow_model),
        "source_identity": source,
        "deterministic_gate": None if deterministic is None else {
            "passed": deterministic.get("passed"),
            "summary_sha256": sha256_file(Path(args.deterministic_summary).resolve()),
        },
    }
    plan_path = out / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True, default=str), encoding="utf-8")

    if not args.allow_model:
        marker = finalize_evidence_directory(
            out,
            required_paths=(plan_path, cases_path),
            metadata={"status": "plan_only", "source_commit": source.get("commit", "")},
        )
        print(json.dumps({"status": "plan_only", "output_dir": str(out), "case_count": len(cases), "final_marker": marker}, indent=2))
        return 0

    model = make_azure_callable(
        deployment_env="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT54_MINI_KEY",
        endpoint_env="AZURE_OPENAI_ENDPOINT",
        effort=effort,
        role="solver",
    )
    rows: list[dict[str, Any]] = []
    for case in cases:
        compiled = _compiled(case)
        messages = build_solver_messages(compiled, dict(case["context"]))
        for sample in range(1, samples + 1):
            hooks = ModelHooks(model, model, run_id=f"solver-checkpoint:{case['id']}:{sample}", task_id=str(case["id"]))
            raw = ""
            protocol_ledger = ExecutionLedger()
            initial_protocol_error = ""
            try:
                turn, protocol_ledger, initial_protocol_error = _solve_with_production_protocol_correction(
                    hooks, messages, compiled,
                )
                raw = str(getattr(hooks, "last_raw_solver_output", ""))
                score = _score_turn(turn, case["expected"])
                retry_failed = any(
                    receipt.receipt_id == "step-1:solver_parse_error_retry"
                    for receipt in protocol_ledger.all_receipts()
                )
                parse_valid = not retry_failed
                protocol_valid = not turn.validate(compiled.action_schema)
                error = "" if parse_valid else "same-step protocol correction remained invalid"
            except ModelOutputError as exc:
                turn = None
                raw = str(getattr(hooks, "last_raw_solver_output", ""))
                score = {"passed": False, "findings": [str(exc)]}
                parse_valid = False
                protocol_valid = False
                error = str(exc)
            redacted, redaction_events = redact_text_with_events(raw)
            row = {
                "case_id": case["id"],
                "sample": sample,
                "official_archetypes": case.get("official_archetypes", []),
                "parse_valid": parse_valid,
                "protocol_valid": protocol_valid,
                "score": score,
                "turn": None if turn is None else {
                    "kind": turn.kind,
                    "summary": turn.summary,
                    "evidence_gap": turn.evidence_gap,
                    "actions": [
                        {
                            "action_id": action.action_id,
                            "kind": action.kind,
                            "capability_id": action.capability_id,
                            "arguments": dict(action.arguments),
                            "intent": action.intent,
                            "expected_observation": action.expected_observation,
                            "if_fail_next": action.if_fail_next,
                        }
                        for action in turn.actions
                    ],
                },
                "error": error,
                "initial_protocol_error": initial_protocol_error,
                "production_protocol_receipts": [
                    {
                        "receipt_id": receipt.receipt_id,
                        "kind": receipt.kind,
                        "success": receipt.success,
                        "failure_class": receipt.failure_class,
                        "summary": receipt.summary,
                        "payload": receipt.payload,
                    }
                    for receipt in protocol_ledger.all_receipts()
                ],
                "raw_output_redacted": redacted,
                "raw_output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "redaction_events": redaction_events,
                "telemetry": list(hooks.drain_model_telemetry()),
                "quarantined_telemetry": list(hooks.drain_quarantined_model_telemetry()),
            }
            rows.append(row)
            case_dir = out / "cases" / str(case["id"]) / f"sample_{sample:02d}"
            case_dir.mkdir(parents=True, exist_ok=True)
            (case_dir / "result.json").write_text(json.dumps(row, indent=2, sort_keys=True, default=str), encoding="utf-8")

    aggregate = _aggregate(rows)
    summary = {
        "schema": "aether.solver_checkpoint_result.v1",
        "plan": plan,
        "aggregate": aggregate,
        "rows": rows,
    }
    summary_path = out / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    marker = finalize_evidence_directory(
        out,
        required_paths=(plan_path, out / "cases", summary_path),
        metadata={
            "status": "completed",
            "strict_promotion": aggregate["strict_promotion"],
            "source_commit": source.get("commit", ""),
        },
    )
    print(json.dumps({"status": "completed", "output_dir": str(out), "aggregate": aggregate, "final_marker": marker}, indent=2, sort_keys=True))
    return 0 if aggregate["strict_promotion"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
