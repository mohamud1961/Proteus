#!/usr/bin/env python3
"""Verifier-only experiment harness.

This script exercises verifier packets and result parsing without running a
solver, Docker, a benchmark task, or the official grader.  It supports a fake
mode for deterministic local validation and a model mode for Codex/VM use when
model credentials are available.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aether_next.analysis import _check_id
from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    CheckSpec,
    CompletionPolicy,
    DeliverableSpec,
    EnvMap,
    EvalIndex,
    ModelVerifierPolicy,
    ObjectiveGraph,
    ProofObligation,
    RuntimeConfigIR,
)
from aether_next.model_hooks import DEFAULT_VERIFIER_IDENTITY_PROMPT, ModelHooks, VERIFIER_RUNTIME_CONTRACT
from aether_next.verifier import ModelVerifierResult, VerifierFinding, parse_model_verifier_result
from aether_next.verifier_packets import build_verifier_packet


class _VerifierEvalError(Exception):
    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def _env(task: str, *, file_map_summary: dict[str, Any] | None = None) -> EnvMap:
    return EnvMap(
        task_prompt=task,
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run shell", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
        },
        file_map_summary=file_map_summary or {},
    )


def _compiled(
    task: str,
    *,
    check_command: str = "test -e out.txt",
    deliverable_path: str = "out.txt",
    file_map_summary: dict[str, Any] | None = None,
):
    check_id = _check_id("verifier_only", check_command)
    envmap = _env(task, file_map_summary=file_map_summary)
    objective = ObjectiveGraph(
        deliverables=(DeliverableSpec(path=deliverable_path),),
        obligations=(ProofObligation(f"artifact:{deliverable_path}", "artifact", f"{deliverable_path} satisfies task", deliverable_path),),
    )
    eval_index = EvalIndex(checks=(CheckSpec(check_id, "visible synthetic check", check_command, "verifier_only"),))
    ir = RuntimeConfigIR(
        architect_summary="verifier-only synthetic runtime",
        solver_identity_prompt=(
            "Use evidence-first repair. If verifier feedback appears, repair the cited artifact "
            "or gather missing evidence before submitting again."
        ),
        selected_capabilities=("shell", "filesystem"),
        completion_policy=CompletionPolicy(require_all_obligations=False, require_recent_progress=False),
        model_verifier_policy=ModelVerifierPolicy(enabled=True),
        check_plan=(check_id,),
        success_definition=f"{deliverable_path} must exist and satisfy the task-specific semantic requirement.",
        local_verification_limits=("Synthetic visible checks cannot prove hidden benchmark grader behavior.",),
        verifier_identity_prompt=(
            "Inspect the current workspace state for this task. Prefer raw files, source inputs, artifacts, "
            "and independent read-only checks over solver-authored command summaries."
        ),
    )
    return ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(
        ir, envmap, objective_graph=objective, eval_index=eval_index,
    )


def _base_ledger() -> ExecutionLedger:
    ledger = ExecutionLedger()
    return ledger


def _cases() -> list[dict[str, Any]]:
    return [
        _case_semantic_wrong(),
        _case_solver_authored_claim_conflicts_with_raw_state(),
        _case_missing_artifact(),
        _case_schema_mismatch(),
        _case_repeated_no_progress(),
        _case_insufficient_evidence(),
    ]


def _case_semantic_wrong() -> dict[str, Any]:
    compiled = _compiled("Write out.txt containing the exact token PASS-123.")
    ledger = _base_ledger(); ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt("write-wrong", 1, "write_file", True, "wrote out.txt", True, payload={"path": "out.txt", "artifact_paths": ("out.txt",), "excerpt": "PASS-124", "after_content_hash": "h-wrong"}))
    ledger.record(Receipt("check-exists", 2, "check_result", True, "existence check passed", True, payload={"check_id": compiled.check_plan_ids[0], "command": "test -e out.txt", "passed": True, "origin": "verifier_only"}))
    return {"case_id": "semantic_wrong", "compiled": compiled, "ledger": ledger, "reason": "deterministic_success_candidate"}


def _case_solver_authored_claim_conflicts_with_raw_state() -> dict[str, Any]:
    compiled = _compiled(
        "Read data/events.log and write summary.csv with the true count of ERROR lines.",
        check_command="test -e summary.csv",
        deliverable_path="summary.csv",
        file_map_summary={
            "likely_inputs": ("data/events.log",),
            "instruction_referenced_visible_paths": ("data/events.log", "summary.csv"),
            "prompt_declared_output_paths": ("summary.csv",),
            "likely_tests_or_checkers": ("tests/check_summary.py",),
        },
    )
    ledger = _base_ledger(); ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt(
        "write-summary",
        1,
        "write_file",
        True,
        "wrote summary.csv",
        True,
        payload={"path": "summary.csv", "artifact_paths": ("summary.csv",), "excerpt": "metric,count\nERROR,1\n", "after_content_hash": "h-summary-wrong"},
    ))
    ledger.record(Receipt(
        "solver-self-check",
        2,
        "run_command",
        True,
        "solver recomputed ERROR count and reported success",
        False,
        payload={
            "command": "python3 - <<'PY'\nprint('summary.csv matches data/events.log')\nPY",
            "exit_code": 0,
            "stdout": "summary.csv matches data/events.log\n",
            "stderr": "",
        },
    ))
    ledger.record(Receipt(
        "check-exists",
        3,
        "check_result",
        True,
        "existence check passed",
        True,
        payload={"check_id": compiled.check_plan_ids[0], "command": "test -e summary.csv", "passed": True, "origin": "verifier_only"},
    ))
    return {"case_id": "solver_claim_conflicts_with_raw_state", "compiled": compiled, "ledger": ledger, "reason": "solver_submit_success_candidate"}


def _case_missing_artifact() -> dict[str, Any]:
    compiled = _compiled("Create required out.txt.")
    ledger = _base_ledger(); ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt("check-missing", 1, "check_result", False, "out.txt missing", False, failure_class="missing_artifact", payload={"check_id": compiled.check_plan_ids[0], "command": "test -e out.txt", "passed": False, "origin": "verifier_only", "detail": "No such file"}))
    return {"case_id": "missing_artifact", "compiled": compiled, "ledger": ledger, "reason": "deterministic_failure"}


def _case_schema_mismatch() -> dict[str, Any]:
    compiled = _compiled("Create out.txt as JSON with key result.", check_command="python3 -c 'import json; json.load(open(\"out.txt\"))[\"result\"]'")
    ledger = _base_ledger(); ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt("write-json", 1, "write_file", True, "wrote out.txt", True, payload={"path": "out.txt", "artifact_paths": ("out.txt",), "excerpt": "{}", "after_content_hash": "h-json"}))
    ledger.record(Receipt("check-schema", 2, "check_result", False, "schema missing result", False, failure_class="schema_mismatch", payload={"check_id": compiled.check_plan_ids[0], "command": compiled.eval_index.checks[0].command, "passed": False, "origin": "verifier_only", "detail": "KeyError: result"}))
    return {"case_id": "schema_mismatch", "compiled": compiled, "ledger": ledger, "reason": "deterministic_failure"}


def _case_repeated_no_progress() -> dict[str, Any]:
    compiled = _compiled("Repair out.txt so it contains DONE.")
    ledger = _base_ledger(); ledger.ensure_objective(compiled.objective_graph)
    finding = VerifierFinding("vf-loop", 1, "needs_repair", "blocking", "out.txt still lacks DONE", evidence=("excerpt lacks DONE",), repair_instruction="Rewrite out.txt with DONE.", applies_to=("out.txt",))
    ledger.apply_verifier_result(ModelVerifierResult("needs_repair", findings=(finding,)), step=1)
    ledger.record(Receipt("read-1", 2, "read_file", True, "read out.txt", payload={"path": "out.txt", "content_hash": "same", "excerpt": "TODO"}))
    ledger.record(Receipt("read-2", 3, "read_file", True, "read out.txt again", payload={"path": "out.txt", "content_hash": "same", "excerpt": "TODO"}))
    return {"case_id": "repeated_no_progress", "compiled": compiled, "ledger": ledger, "reason": "solver_submit_success_candidate"}


def _case_insufficient_evidence() -> dict[str, Any]:
    compiled = _compiled("Create out.txt containing a computed answer and show evidence.")
    ledger = _base_ledger(); ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt("write-answer", 1, "write_file", True, "wrote out.txt", True, payload={"path": "out.txt", "artifact_paths": ("out.txt",), "after_content_hash": "h-answer"}))
    return {"case_id": "insufficient_evidence", "compiled": compiled, "ledger": ledger, "reason": "solver_submit_success_candidate"}


def _fake_output(case_id: str, packet: dict[str, Any]) -> dict[str, Any]:
    if case_id == "semantic_wrong":
        return {"verdict": "needs_repair", "confidence": "high", "summary": "Visible evidence shows the artifact token differs from the success definition.", "findings": [{"finding_id": "vf-semantic-wrong", "summary": "out.txt contains PASS-124 but success requires PASS-123.", "evidence": ["artifact_evidence excerpt PASS-124", "success_definition requires semantic match"], "repair_instruction": "Rewrite out.txt with PASS-123 and rerun visible checks.", "applies_to": ["out.txt"]}]}
    if case_id == "solver_claim_conflicts_with_raw_state":
        return {"verdict": "uncertain_missing_evidence", "confidence": "high", "summary": "The deliverable exists and the solver claims it recomputed the count, but the packet has not independently inspected the raw input data/events.log.", "missing_evidence_requests": ["Read data/events.log as raw source state.", "Compare the raw ERROR count against summary.csv with a verifier-owned read-only check."]}
    if case_id == "missing_artifact":
        return {"verdict": "needs_repair", "confidence": "high", "summary": "Required artifact is missing.", "findings": [{"finding_id": "vf-missing-artifact", "summary": "out.txt is absent and the visible check failed.", "evidence": ["deterministic check failed", "artifacts_present is empty"], "repair_instruction": "Create out.txt and rerun the visible check.", "applies_to": ["out.txt"]}]}
    if case_id == "schema_mismatch":
        return {"verdict": "needs_repair", "confidence": "high", "summary": "Schema check failed.", "findings": [{"finding_id": "vf-schema", "summary": "out.txt JSON lacks required key result.", "evidence": ["schema_mismatch check_result", "KeyError: result"], "repair_instruction": "Write valid JSON containing result.", "applies_to": ["out.txt"]}]}
    if case_id == "repeated_no_progress":
        return {"verdict": "needs_repair", "confidence": "medium", "summary": "Active finding remains unresolved and no artifact change is visible.", "findings": [{"finding_id": "vf-loop-still-active", "summary": "The same file was reread twice with the same hash after the finding.", "evidence": ["active_findings contains vf-loop", "files_already_read shows repeated read", "changes_since_active_findings has no write"], "repair_instruction": "Stop rereading and modify out.txt, then verify the change.", "applies_to": ["out.txt"]}]}
    return {"verdict": "uncertain_missing_evidence", "confidence": "medium", "summary": "Artifact exists but packet lacks content/check evidence proving the requested computation.", "missing_evidence_requests": ["Read out.txt or include artifact excerpt/hash.", "Run a visible check tied to the success definition."]}


def _judge(case_id: str, packet: dict[str, Any], parsed: ModelVerifierResult | None) -> dict[str, Any]:
    if parsed is None:
        return {"evidence_bound": False, "actionable": False, "notes": "parse failed"}
    text = json.dumps(parsed.as_dict()).lower()
    evidence_bound = any(key in text for key in (
        "check",
        "artifact",
        "excerpt",
        "hash",
        "active",
        "success_definition",
        "schema",
        "file contents",
        "read-only evidence",
        "current evidence",
    ))
    actionable = parsed.verdict == "uncertain_missing_evidence" and bool(parsed.missing_evidence_requests)
    actionable = actionable or any(f.repair_instruction for f in parsed.findings)
    return {"evidence_bound": evidence_bound, "actionable": actionable, "notes": f"deterministic judgement for {case_id}"}


def _model_messages(packet: dict[str, Any]) -> list[dict[str, str]]:
    verifier_prompt = (
        packet.get("architect_verifier_prompt", {}).get("rendered")
        or DEFAULT_VERIFIER_IDENTITY_PROMPT
    )
    return [
        {"role": "system", "content": verifier_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "verifier_runtime_contract": VERIFIER_RUNTIME_CONTRACT,
                    "verifier_packet": packet,
                },
                indent=2,
                sort_keys=True,
            ),
        },
    ]


def _model_output(packet: dict[str, Any]) -> str:
    from aether_next.providers.azure_model import make_azure_callable

    model = make_azure_callable(
        deployment_env="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT54_MINI_KEY",
        endpoint_env="AZURE_OPENAI_ENDPOINT",
        effort=os.environ.get("AETHER_VERIFIER_EFFORT", "high"),
        poll_interval_s=float(os.environ.get("AETHER_VERIFIER_POLL_INTERVAL_S", "5")),
        poll_timeout_s=float(os.environ.get("AETHER_VERIFIER_POLL_TIMEOUT_S", "900")),
    )
    return model(_model_messages(packet), max_output_tokens=int(os.environ.get("AETHER_VERIFIER_MAX_OUTPUT_TOKENS", "6000")))


def _model_output_with_inspector(case_id: str, compiled, ledger: ExecutionLedger, packet: dict[str, Any]) -> str:
    from aether_next.providers.azure_model import make_azure_callable

    model = make_azure_callable(
        deployment_env="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT54_MINI_KEY",
        endpoint_env="AZURE_OPENAI_ENDPOINT",
        effort=os.environ.get("AETHER_VERIFIER_EFFORT", "high"),
        poll_interval_s=float(os.environ.get("AETHER_VERIFIER_POLL_INTERVAL_S", "5")),
        poll_timeout_s=float(os.environ.get("AETHER_VERIFIER_POLL_TIMEOUT_S", "900")),
    )
    hooks = ModelHooks(model, model, verifier_model=model)
    try:
        return hooks.verify_with_inspector(packet, compiled, ledger, _synthetic_inspector(case_id))
    except Exception as exc:
        raise _VerifierEvalError(str(exc), str(getattr(hooks, "last_raw_verifier_output", ""))) from exc


def _synthetic_inspector(case_id: str):
    file_contents = {
        "semantic_wrong": {"out.txt": "PASS-124\n"},
        "solver_claim_conflicts_with_raw_state": {
            "data/events.log": "INFO boot\nERROR bad\nERROR worse\n",
            "summary.csv": "metric,count\nERROR,1\n",
        },
        "missing_artifact": {},
        "schema_mismatch": {"out.txt": "{}\n"},
        "repeated_no_progress": {"out.txt": "TODO\n"},
        "insufficient_evidence": {"out.txt": "answer-without-derivation\n"},
    }
    files = file_contents.get(case_id, {})

    def inspect(requests):
        rows = []
        for request in requests:
            row = {
                "request_id": request.request_id,
                "kind": request.kind,
                "read_only": True,
            }
            if request.kind == "read_file":
                path = request.path
                if path in files:
                    content = files[path]
                    row.update({
                        "path": path,
                        "exists": True,
                        "bytes": len(content),
                        "offset": request.offset,
                        "excerpt": content[request.offset: request.offset + max(1, request.limit)],
                    })
                else:
                    row.update({"path": path, "exists": False, "error": "file_not_found"})
            elif request.kind == "inspect_artifact":
                row.update({"path": request.path, "exists": request.path in files})
            elif request.kind == "inspect_recent_receipts":
                row.update({"receipts": []})
            elif request.kind == "inspect_artifact_history":
                row.update({"path": request.path, "history": []})
            elif request.kind == "overlay_run_command":
                row.update({"exit_code": 0, "stdout": "synthetic verifier-only overlay output\n", "stderr": ""})
            else:
                row.update({"error": f"synthetic inspector does not implement {request.kind}"})
            rows.append(row)
        return rows

    return inspect


def run(mode: str, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in _cases():
        case_id = item["case_id"]
        case_dir = out_dir / case_id; case_dir.mkdir(exist_ok=True)
        packet = build_verifier_packet(
            item["compiled"],
            item["ledger"],
            step=len(item["ledger"].all_receipts()) + 1,
            reason=item["reason"],
            envmap=_env(item["compiled"].task_prompt),
        )
        try:
            raw = (
                _fake_output(case_id, packet)
                if mode == "fake"
                else _model_output_with_inspector(case_id, item["compiled"], item["ledger"], packet)
            )
            model_error = ""
        except _VerifierEvalError as exc:
            raw = exc.raw
            model_error = str(exc)
        parsed = None; parse_error = ""
        try:
            parsed = parse_model_verifier_result(raw)
            item["ledger"].apply_verifier_result(parsed, step=packet["step"])
        except Exception as exc:  # pragma: no cover - model-mode forensic path
            parse_error = model_error or str(exc)
        judgement = _judge(case_id, packet, parsed)
        (case_dir / "verifier_packet.json").write_text(json.dumps(packet, indent=2, sort_keys=True))
        (case_dir / "raw_output.json").write_text(json.dumps(raw, indent=2, sort_keys=True) if not isinstance(raw, str) else raw)
        (case_dir / "parsed_result.json").write_text(json.dumps(parsed.as_dict() if parsed else {"parse_error": parse_error}, indent=2, sort_keys=True))
        (case_dir / "active_findings_after.json").write_text(json.dumps(item["ledger"].active_finding_context(packet["step"] + 1), indent=2, sort_keys=True))
        (case_dir / "judgement.json").write_text(json.dumps(judgement, indent=2, sort_keys=True))
        rows.append({
            "case": case_id,
            "raw_verdict": raw.get("verdict") if isinstance(raw, dict) else "raw_text",
            "parsed_verdict": parsed.verdict if parsed else "parse_error",
            "parse_ok": parsed is not None,
            "parse_error": parse_error,
            "active_findings": [f["finding_id"] for f in item["ledger"].active_finding_context(packet["step"] + 1)],
            **judgement,
            "artifact_paths": packet.get("artifacts_present", []),
        })
    report = _report(mode, rows)
    (out_dir / "VERIFIER_ONLY_EXPERIMENT_REPORT.md").write_text(report)
    (out_dir / "summary.json").write_text(json.dumps({"mode": mode, "rows": rows}, indent=2, sort_keys=True))
    return {"mode": mode, "out_dir": str(out_dir), "rows": rows}


def _report(mode: str, rows: list[dict[str, Any]]) -> str:
    lines = [f"# Verifier-Only Experiment Report", "", f"Mode: `{mode}`", "", "| case | raw verdict | parsed verdict | parse ok | active findings | evidence-bound | actionable | notes | artifact paths |", "|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append("| {case} | {raw_verdict} | {parsed_verdict} | {parse_ok} | {active_findings} | {evidence_bound} | {actionable} | {notes} | {artifact_paths} |".format(**row))
    lines.extend(["", "No solver, Docker, VM, benchmark, or official grader run is performed by this script."])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fake", "model"), default="fake")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()
    default = Path("verifier_only_eval_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    result = run(args.mode, Path(args.out_dir) if args.out_dir else default)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
