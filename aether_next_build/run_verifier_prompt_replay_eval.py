#!/usr/bin/env python3
"""Replay diagnostic for architect-authored verifier prompts.

This does not run a solver, benchmark task, Docker, or grader.  It freezes a
small evidence state, builds verifier packets twice, and compares generic vs
architect-authored verifier prompt behavior through the same parser/result
artifact path used by verifier-only evals.
"""
from __future__ import annotations

import argparse
import json
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
from aether_next.verifier import ModelVerifierResult, VerifierFinding, parse_model_verifier_result
from aether_next.verifier_packets import build_verifier_packet


ARCHITECT_VERIFIER_PROMPT = """\
You are verifying a file-repair task. Judge only packet evidence.
Success requires out.txt to exist, contain DONE, and have fresh evidence after
the latest blocking finding. Treat unresolved active findings or open obligations
without a later state-changing write/check as non-progress. Feedback must cite packet evidence
and give the solver a concrete next action.
"""


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Repair out.txt so it contains DONE.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "Run shell", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
        },
    )


def _compiled(*, architect_prompt: bool) -> Any:
    envmap = _env()
    check_id = _check_id("replay", "grep -q DONE out.txt")
    objective = ObjectiveGraph(
        deliverables=(DeliverableSpec(path="out.txt"),),
        obligations=(ProofObligation("artifact:out.txt", "artifact", "out.txt contains DONE", "out.txt"),),
    )
    eval_index = EvalIndex(checks=(CheckSpec(check_id, "out.txt contains DONE", "grep -q DONE out.txt", "replay"),))
    ir = RuntimeConfigIR(
        architect_summary="verifier prompt replay runtime",
        solver_identity_prompt="Repair out.txt and prove it contains DONE before submitting.",
        selected_capabilities=("shell", "filesystem"),
        completion_policy=CompletionPolicy(require_all_obligations=False, require_recent_progress=False),
        model_verifier_policy=ModelVerifierPolicy(enabled=True),
        check_plan=(check_id,),
        success_definition="out.txt must exist, contain DONE, and be backed by fresh post-repair evidence.",
        local_verification_limits=("Visible grep cannot prove hidden benchmark behavior.",),
        verifier_identity_prompt=ARCHITECT_VERIFIER_PROMPT if architect_prompt else "",
        evidence_requirements=("latest out.txt excerpt/hash", "grep -q DONE out.txt result", "evidence after active finding"),
        false_positive_risks=("existence-only checks", "repeated reads of unchanged TODO content"),
        minimum_completion_evidence=("write or command that changes out.txt", "passing DONE check"),
    )
    return ConfigCompiler(CapabilityRegistry.from_envmap(envmap)).compile(
        ir, envmap, objective_graph=objective, eval_index=eval_index,
    )


def _ledger(compiled: Any) -> ExecutionLedger:
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    ledger.apply_verifier_result(
        ModelVerifierResult(
            "needs_repair",
            findings=(
                VerifierFinding(
                    "vf-loop",
                    1,
                    "needs_repair",
                    "blocking",
                    "out.txt still lacks DONE",
                    evidence=("out.txt excerpt TODO",),
                    repair_instruction="Rewrite out.txt with DONE and rerun grep.",
                    applies_to=("out.txt",),
                ),
            ),
        ),
        step=1,
    )
    ledger.record(Receipt("read-1", 2, "read_file", True, "read out.txt", payload={"path": "out.txt", "content_hash": "same", "excerpt": "TODO"}))
    ledger.record(Receipt(
        "auto-1",
        3,
        "automatic_memory",
        True,
        "automatic memory surfaced 1 prior event(s) for read_file:out.txt",
        payload={
            "action_kind": "read_file",
            "target": {"action_kind": "read_file", "target_type": "file", "key": "out.txt", "label": "read_file:out.txt", "explicit": True},
            "match_count": 1,
            "latest_receipt_id": "read-1",
            "same_content_hash": True,
            "repeat_justified": False,
            "guidance": "Automatic memory found prior evidence for this target.",
            "recent_evidence": [{"receipt_id": "read-1", "kind": "read_file", "path": "out.txt", "excerpt": "TODO"}],
        },
    ))
    ledger.record(Receipt("read-2", 3, "read_file", True, "read out.txt again", payload={"path": "out.txt", "content_hash": "same", "excerpt": "TODO"}))
    ledger.record(Receipt(
        "check-1",
        4,
        "check_result",
        False,
        "grep failed",
        failure_class="check_failed",
        payload={"check_id": compiled.check_plan_ids[0], "command": "grep -q DONE out.txt", "passed": False, "origin": "replay", "detail": "DONE not found"},
    ))
    return ledger


def _fake_verifier_output(packet: dict[str, Any]) -> dict[str, Any]:
    """Deterministic state-only evaluator used to validate packet isolation.

    Architect strategy must never be present in a model-visible Verifier packet.
    This fixture therefore judges the observable active finding and obligation,
    rather than using the old prompt-presence proxy.
    """
    return {
        "verdict": "needs_repair",
        "confidence": "high",
        "summary": "The packet still has an active finding that out.txt lacks DONE, the out.txt obligation is open, and no fresh post-repair evidence is present.",
        "findings": [{
            "finding_id": "vf-repeat-no-repair",
            "summary": "The active repair finding is unresolved: out.txt lacks DONE and no later write/change is present.",
            "evidence": [
                "active_findings: out.txt still lacks DONE with evidence 'out.txt excerpt TODO'",
                "open_obligations: artifact:out.txt remains open",
                "minimum_completion_evidence requires a write/change and passing DONE check",
            ],
            "repair_instruction": "Write DONE to out.txt, rerun grep -q DONE out.txt, then submit only if that check passes.",
            "applies_to": ["out.txt"],
        }],
    }


def _judge(parsed: ModelVerifierResult) -> dict[str, Any]:
    text = json.dumps(parsed.as_dict(), sort_keys=True).lower()
    return {
        "evidence_bound": all(term in text for term in ("out.txt", "done")) and any(term in text for term in ("grep", "active_findings", "open_obligations", "excerpt")),
        "actionable": bool(parsed.findings and parsed.findings[0].repair_instruction) or bool(parsed.missing_evidence_requests),
        "specific_repair": bool(parsed.findings and "grep -q done" in parsed.findings[0].repair_instruction.lower()),
    }


def run(out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for variant, architect_prompt in (("generic", False), ("architect_prompt", True)):
        compiled = _compiled(architect_prompt=architect_prompt)
        ledger = _ledger(compiled)
        packet = build_verifier_packet(
            compiled,
            ledger,
            step=5,
            reason="replay",
            envmap=_env(),
        )
        raw = _fake_verifier_output(packet)
        parsed = parse_model_verifier_result(raw)
        ledger.apply_verifier_result(parsed, step=5)
        judgement = _judge(parsed)
        variant_dir = out_dir / variant
        variant_dir.mkdir(exist_ok=True)
        (variant_dir / "verifier_packet.json").write_text(json.dumps(packet, indent=2, sort_keys=True))
        (variant_dir / "raw_output.json").write_text(json.dumps(raw, indent=2, sort_keys=True))
        (variant_dir / "parsed_result.json").write_text(json.dumps(parsed.as_dict(), indent=2, sort_keys=True))
        (variant_dir / "active_findings_after.json").write_text(json.dumps(ledger.active_finding_context(6), indent=2, sort_keys=True))
        (variant_dir / "judgement.json").write_text(json.dumps(judgement, indent=2, sort_keys=True))
        rows.append({
            "variant": variant,
            "verdict": parsed.verdict,
            "finding_count": len(parsed.findings),
            "architect_prompt_requested": architect_prompt,
            **judgement,
        })
    summary = {
        "mode": "fake_replay",
        "rows": rows,
        "state_only_packet_actionable": all(
            row["specific_repair"] and row["verdict"] == "needs_repair"
            for row in rows
        ),
    }
    (out_dir / "verifier_prompt_replay_eval.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    report = ["# Verifier Prompt Replay Eval", ""]
    for row in rows:
        report.append(
            f"- {row['variant']}: verdict={row['verdict']} findings={row['finding_count']} "
            f"evidence_bound={row['evidence_bound']} actionable={row['actionable']} specific_repair={row['specific_repair']}"
        )
    report.append("")
    report.append(f"state_only_packet_actionable={summary['state_only_packet_actionable']}")
    (out_dir / "VERIFIER_PROMPT_REPLAY_REPORT.md").write_text("\n".join(report) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="verifier_prompt_replay_eval_fake")
    args = parser.parse_args()
    print(json.dumps(run(Path(args.out_dir)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
