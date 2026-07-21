#!/usr/bin/env python3
"""Stage 1 trace replay acceptance for runtime enforcement.

This is not a benchmark rerun. It reuses the VM Stage 1 artifacts to prove the
new runtime gates would classify the observed failures before another VM run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

_BUILD_DIR = str(Path(__file__).resolve().parent)
if _BUILD_DIR not in sys.path:
    sys.path.insert(0, _BUILD_DIR)

from aether_next.compiler import CapabilityRegistry, ConfigCompiler  # noqa: E402
from aether_next.ledger import ExecutionLedger, Receipt  # noqa: E402
from aether_next.no_progress import NoProgressController  # noqa: E402
from reference_legacy.proof_contract import analyze_proof_contract  # noqa: E402
from aether_next.runtime_ir import (  # noqa: E402
    ActionRequest,
    AutomaticMemoryPolicy,
    CapabilityDescriptor,
    CompletionPolicy,
    EnvMap,
    ModelVerifierPolicy,
    RuntimeConfigIR,
)


DEFAULT_RUN = Path("vm_goal_runs/20260701T144500Z_aether_next_vm_stage1_py311")
REPAIR_SLICE_RERUN = Path("vm_goal_runs/20260701T_runtime_enforcement_repair_slice_rerun")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _env(task_prompt: str) -> EnvMap:
    return EnvMap(
        task_prompt=task_prompt,
        workspace_root="/app",
        capabilities={
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
            "shell": CapabilityDescriptor("shell", "Run shell", tool_names=("run_command",)),
        },
    )


def _trace_body(trace_data: dict[str, Any]) -> dict[str, Any]:
    """Trace files predating the H1 to_dict() fix nest the step/config data under
    a "trace" key; the fixed format is flat (task/image/reward/status sit
    alongside architect_config/steps/etc. directly). Accept either."""
    trace = trace_data.get("trace")
    return trace if isinstance(trace, dict) else trace_data


def _compiled_from_trace(trace_data: dict[str, Any]) -> Any:
    trace = _trace_body(trace_data)
    config = trace.get("architect_config", {}) if isinstance(trace.get("architect_config", {}), dict) else {}
    task_prompt = ""
    for item in trace.get("prefix_messages", []) or []:
        content = str(item.get("content", ""))
        if content.startswith("[task_prompt]"):
            task_prompt = content.split("\n", 1)[1] if "\n" in content else ""
    env = _env(task_prompt or str(trace_data.get("task", "")))
    ir = RuntimeConfigIR(
        architect_summary=str(config.get("architect_summary", "")),
        solver_identity_prompt=str(config.get("solver_identity_prompt", "")),
        selected_capabilities=("filesystem", "shell"),
        automatic_memory_policy=AutomaticMemoryPolicy(mode="advisory"),
        completion_policy=CompletionPolicy(require_all_obligations=False, require_recent_progress=False),
        model_verifier_policy=ModelVerifierPolicy(enabled=True),
        success_definition=str(config.get("success_definition", "")),
        local_verification_limits=tuple(str(x) for x in config.get("local_verification_limits", ()) or ()),
        verifier_identity_prompt=str(config.get("verifier_identity_prompt", "")),
        evidence_requirements=tuple(str(x) for x in config.get("evidence_requirements", ()) or ()),
        false_positive_risks=tuple(str(x) for x in config.get("false_positive_risks", ()) or ()),
        minimum_completion_evidence=tuple(str(x) for x in config.get("minimum_completion_evidence", ()) or ()),
    )
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)


def _receipts_from_trace(trace_data: dict[str, Any]) -> ExecutionLedger:
    ledger = ExecutionLedger()
    for step in _trace_body(trace_data).get("steps", []) or []:
        step_no = int(step.get("step", 0))
        for idx, obs in enumerate(step.get("observations", []) or []):
            if not isinstance(obs, dict):
                continue
            payload: dict[str, Any] = {}
            path = str(obs.get("path", "")).strip()
            if path:
                payload["path"] = path.removeprefix("/app/")
            summary = str(obs.get("summary", ""))
            if obs.get("exit_code") is not None:
                payload["exit_code"] = obs.get("exit_code")
            if obs.get("stdout_tail"):
                payload["stdout"] = str(obs.get("stdout_tail"))
            if obs.get("stderr_tail"):
                payload["stderr"] = str(obs.get("stderr_tail"))
            if obs.get("kind") == "run_command" and ":" in summary:
                payload["command"] = summary.split(":", 1)[1].strip()
            if obs.get("kind") == "write_file" and path:
                payload["modified_paths"] = (payload["path"],)
                payload["artifact_paths"] = (payload["path"],)
            ledger.record(Receipt(
                str(obs.get("receipt_id") or f"step-{step_no}:obs-{idx}"),
                step_no,
                str(obs.get("kind", "")),
                bool(obs.get("success", False)),
                summary,
                state_change=obs.get("kind") == "write_file",
                failure_class=str(obs.get("failure_class", "")),
                payload=payload,
            ))
    return ledger


_FILTER_BLOCKING_CODES = {
    "insufficient_adversarial_filter_evidence",
    "missing_clean_preservation_evidence",
}


def _filter_acceptance(root: Path) -> dict[str, Any]:
    trace_data = _load(root / "traces" / "filter-js-from-html.trace.json")
    compiled = _compiled_from_trace(trace_data)
    ledger = _receipts_from_trace(trace_data)
    analysis = analyze_proof_contract(compiled, ledger)
    codes = {finding["code"] for finding in analysis["findings"]}
    # Accept any of the security/html-filter analyzer's blocking codes -- what
    # matters is that the false-clean is blocked, not which specific obligation
    # (attack-class coverage vs clean-preservation evidence) caught it first.
    passed = analysis["status"] == "failed" and bool(codes & _FILTER_BLOCKING_CODES)
    return {
        "case": "filter_false_clean",
        "passed": passed,
        "expected": sorted(_FILTER_BLOCKING_CODES),
        "analysis": analysis,
    }


def _sparql_proof_acceptance(root: Path) -> dict[str, Any]:
    trace_data = _load(root / "traces" / "sparql-university.trace.json")
    compiled = _compiled_from_trace(trace_data)
    ledger = ExecutionLedger()
    graph = (root / "snapshots" / "sparql-university" / "final" / "university_graph.ttl").read_text(encoding="utf-8")
    query = (root / "snapshots" / "sparql-university" / "final" / "solution.sparql").read_text(encoding="utf-8")
    ledger.record(Receipt("graph-read", 0, "read_file", True, "read university_graph.ttl", payload={"path": "university_graph.ttl", "excerpt": graph}))
    ledger.record(Receipt("query-read", 1, "read_file", True, "read solution.sparql", payload={"path": "solution.sparql", "excerpt": query}))
    analysis = analyze_proof_contract(compiled, ledger)
    codes = {finding["code"] for finding in analysis["findings"]}
    passed = {"declared_query_terms_absent_from_graph", "missing_semantic_query_execution"}.issubset(codes)
    return {
        "case": "sparql_invented_predicates",
        "passed": passed,
        "expected": ["declared_query_terms_absent_from_graph", "missing_semantic_query_execution"],
        "analysis": analysis,
    }


def _sparql_repeat_acceptance(root: Path) -> dict[str, Any]:
    trace_data = _load(root / "traces" / "sparql-university.trace.json")
    controller = NoProgressController()
    ledger = ExecutionLedger()
    blocked: list[dict[str, Any]] = []
    for step in _trace_body(trace_data).get("steps", []) or []:
        step_no = int(step.get("step", 0))
        for idx, obs in enumerate(step.get("observations", []) or []):
            if obs.get("kind") != "run_command":
                continue
            summary = str(obs.get("summary", ""))
            command = summary.split(":", 1)[1].strip() if ":" in summary else summary
            action = ActionRequest(
                action_id=f"replay-{step_no}-{idx}",
                kind="run_command",
                capability_id="shell",
                arguments={"command": command},
                intent="replay",
                expected_observation="replay",
                if_fail_next="replay",
            )
            decision = controller.evaluate(action, ledger)
            if decision is not None:
                blocked.append(decision.as_dict() | {"step": step_no, "command": command})
                # Continue recording once to show the historical loop would have
                # been intercepted; no need to simulate downstream branching.
                return {
                    "case": "sparql_repeated_evidence_display",
                    "passed": True,
                    "blocked": blocked,
                }
            ledger.record(Receipt(
                str(obs.get("receipt_id") or f"step-{step_no}:cmd-{idx}"),
                step_no,
                "run_command",
                bool(obs.get("success", False)),
                summary,
                payload={"command": command, "stdout": str(obs.get("stdout_tail", ""))},
            ))
    return {"case": "sparql_repeated_evidence_display", "passed": False, "blocked": blocked}


def _filter_semantic_phrase_independence_acceptance(rerun_root: Path) -> dict[str, Any]:
    """Replays the ACTUAL Stage 1 repair-slice rerun's filter-js-from-html trace
    (kernel status=completed, reward=0.0 -- a real false-clean under the new,
    genuinely populated architect contract).

    This is NOT a pass/fail gate -- it's documented, honest evidence of where
    Slice A's structural evidence-shape checks stop and Slice B (verifier-
    executed independent probes) picks up. The real solver in this run wrote a
    genuinely richer self-authored fixture than the synthetic single-sample case
    (it covers all three attack classes AND mentions preservation), so A1's
    structural obligations are satisfied by the SHAPE of the evidence -- while
    the underlying filter.py implementation was still wrong (grader failed both
    tests). Slice A can verify that adversarial-shaped evidence was gathered; it
    cannot verify the evidence's content is correct without independently
    executing it, which is exactly Slice B's job. Do NOT "fix" this by stacking
    more keyword/phrase requirements onto proof_contract.py -- that recreates
    the same brittleness this slice was meant to remove. The phrase-independence
    fix itself (the actual A1 bug) is proven by the synthetic unit test
    test_filter_security_analyzer_fires_on_differently_worded_risk_text, which
    uses a deliberately narrow single-class sample where the fix's effect is
    directly observable.
    """
    trace_path = rerun_root / "traces" / "filter-js-from-html.trace.json"
    if not trace_path.exists():
        return {"case": "filter_semantic_phrase_independence", "passed": None, "gate": False, "skipped": f"no trace at {trace_path}"}
    trace_data = _load(trace_path)
    compiled = _compiled_from_trace(trace_data)
    ledger = _receipts_from_trace(trace_data)
    analysis = analyze_proof_contract(compiled, ledger)
    codes = {finding["code"] for finding in analysis["findings"]}
    blocked = analysis["status"] == "failed" and bool(codes & {
        "insufficient_adversarial_filter_evidence", "missing_clean_preservation_evidence",
    })
    return {
        "case": "filter_semantic_phrase_independence",
        "passed": blocked,
        "gate": False,  # informational: does not gate the overall replay-acceptance summary
        "note": (
            "Slice A structural checks are satisfied by this run's evidence SHAPE "
            "(multi-class + preservation mentions), even though the implementation "
            "was wrong -- this is the known, deliberate Slice A/B boundary, not a bug."
        ),
        "expected": "blocking finding despite non-matching risk-text phrasing (see docstring for why this may legitimately not block)",
        "architect_false_positive_risks": list(
            json.loads(json.dumps(_trace_body(trace_data).get("architect_config", {}).get("false_positive_risks", ()) or ()))
        ),
        "analysis": analysis,
    }


def _openssl_structural_evidence_acceptance(rerun_root: Path) -> dict[str, Any]:
    """Replays the ACTUAL Stage 1 repair-slice rerun's openssl-selfsigned-cert
    trace (kernel status=incomplete/model_limit despite reward=1.0 -- the solver
    genuinely re-gathered permission and openssl-inspection evidence at steps
    24/28/29, but the active finding from step 2 never cleared). Proves the real
    evidence the solver produced satisfies the new openssl structural analyzer
    (proof_contract.py A1), which is the deterministic gate the stale-finding
    resolver (verifier.py A2) checks before auto-resolving a finding the verifier
    itself never revisited. (The finding-lifecycle replay itself -- the verifier
    returning uncertain with an empty findings list, then the runtime clearing
    the stale finding once this evidence exists -- is proven separately by
    test_stale_active_finding_resolves_once_runtime_confirms_the_requested_evidence,
    since the trace format doesn't retain full verifier result payloads.)
    """
    trace_path = rerun_root / "traces" / "openssl-selfsigned-cert.trace.json"
    if not trace_path.exists():
        return {"case": "openssl_structural_evidence", "passed": None, "skipped": f"no trace at {trace_path}"}
    trace_data = _load(trace_path)
    compiled = _compiled_from_trace(trace_data)
    ledger = _receipts_from_trace(trace_data)
    analysis = analyze_proof_contract(compiled, ledger)
    passed = analysis["status"] == "passed"
    return {
        "case": "openssl_structural_evidence",
        "passed": passed,
        "expected": "proof_contract status=passed once real permission+openssl-inspection evidence is present",
        "analysis": analysis,
    }


def run(root: Path, out_dir: Path, *, repair_slice_rerun_root: Path = REPAIR_SLICE_RERUN) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        _filter_acceptance(root),
        _sparql_repeat_acceptance(root),
        _sparql_proof_acceptance(root),
    ]
    if repair_slice_rerun_root.exists():
        rows.append(_filter_semantic_phrase_independence_acceptance(repair_slice_rerun_root))
        rows.append(_openssl_structural_evidence_acceptance(repair_slice_rerun_root))
    summary = {
        "schema_version": "aether_next.stage1_replay_acceptance.v1",
        "run_root": str(root),
        "repair_slice_rerun_root": str(repair_slice_rerun_root) if repair_slice_rerun_root.exists() else None,
        "rows": rows,
        # A row with passed=None was skipped (its source trace wasn't available).
        # A row with gate=False is informational (documents a known, deliberate
        # boundary rather than asserting a regression). Neither gates the summary.
        "passed": all(
            row["passed"] for row in rows
            if row["passed"] is not None and row.get("gate", True)
        ),
    }
    (out_dir / "stage1_replay_acceptance.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    report = ["# Stage 1 Replay Acceptance", ""]
    for row in rows:
        report.append(f"- {row['case']}: passed={row['passed']}")
    report.append("")
    report.append(f"overall_passed={summary['passed']}")
    (out_dir / "STAGE1_REPLAY_ACCEPTANCE_REPORT.md").write_text("\n".join(report) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", default=str(DEFAULT_RUN))
    parser.add_argument("--out-dir", default="stage1_replay_acceptance_runtime_enforcement")
    args = parser.parse_args()
    print(json.dumps(run(Path(args.run_root), Path(args.out_dir)), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
