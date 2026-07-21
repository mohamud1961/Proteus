#!/usr/bin/env python3
"""Automatic-memory policy diagnostic eval.

This is a deterministic harness mechanism eval, not a benchmark run. It checks
whether automatic memory surfaces repeat evidence and keeps stricter
policies advisory-only in the certified harness.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from aether_next.kernel import AetherNextKernel
from aether_next.execution import MemoryExecutor
from aether_next.ledger import Receipt
from aether_next.runtime_ir import (
    ActionRequest,
    AutomaticMemoryPolicy,
    BootstrapPolicy,
    CapabilityDescriptor,
    CompletionPolicy,
    ContextPolicy,
    EnvMap,
    HelperToolPolicy,
    RuntimeConfigIR,
    SolverTurn,
)
from aether_next.tracing import RunTrace


POLICY_MODES = ("off", "advisory", "require_justification", "soft_block_exact_repeat")


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Diagnostic repeated-action task.",
        workspace_root="/app",
        capabilities={
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
            "shell": CapabilityDescriptor("shell", "Run shell", tool_names=("run_command",)),
        },
    )


def _runtime(mode: str) -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary=f"automatic memory diagnostic {mode}",
        solver_identity_prompt="Use automatic memory findings instead of repeating actions.",
        selected_capabilities=("filesystem", "shell"),
        context_policy=ContextPolicy(mode="retrieval_augmented"),
        automatic_memory_policy=AutomaticMemoryPolicy(mode=mode),
        completion_policy=CompletionPolicy(require_all_obligations=False, require_recent_progress=False),
        bootstrap_policy=BootstrapPolicy(allow_acquisition=False),
        helper_tool_policy=HelperToolPolicy(allow_creation=False),
    )


def _action(kind: str, args: dict[str, Any], *, action_id: str = "a", cap: str = "filesystem") -> ActionRequest:
    return ActionRequest(
        action_id=action_id,
        kind=kind,
        capability_id=cap,
        arguments=args,
        intent="diagnostic",
        expected_observation="diagnostic",
        if_fail_next="diagnostic",
    )


class _Hooks:
    def __init__(self, runtime: RuntimeConfigIR, turns: list[SolverTurn]) -> None:
        self.runtime = runtime
        self.turns = list(turns)

    def architect(self, request):
        return self.runtime

    def solve(self, messages, compiled):
        if self.turns:
            return self.turns.pop(0)
        return SolverTurn(kind="submit_outcome", summary="submit")


def _case_turns(case: str) -> list[SolverTurn]:
    if case == "repeat_read":
        read = _action("read_file", {"path": "input.txt"}, action_id="read")
        return [
            SolverTurn(kind="act", summary="read input", actions=(read,)),
            SolverTurn(kind="act", summary="read input again", actions=(read,)),
        ]
    if case == "repeat_command":
        cmd = _action("run_command", {"command": "echo diagnostic"}, action_id="cmd", cap="shell")
        return [
            SolverTurn(kind="act", summary="run command", actions=(cmd,)),
            SolverTurn(kind="act", summary="run command again", actions=(cmd,)),
        ]
    if case == "justified_repeat_read":
        first = _action("read_file", {"path": "input.txt"}, action_id="read")
        second = _action(
            "read_file",
            {"path": "input.txt", "repeat_justification": "checking after expected external state change"},
            action_id="read",
        )
        return [
            SolverTurn(kind="act", summary="read input", actions=(first,)),
            SolverTurn(kind="act", summary="read input justified", actions=(second,)),
        ]
    raise ValueError(f"unknown case: {case}")


def _score(case: str, mode: str, receipts: tuple[Receipt, ...], trace: RunTrace) -> dict[str, Any]:
    reads = [r for r in receipts if r.kind == "read_file"]
    commands = [r for r in receipts if r.kind == "run_command"]
    automatic = [r for r in receipts if r.kind == "automatic_memory"]
    blocks = [r for r in receipts if r.kind in {"automatic_memory_block", "automatic_memory_advisory"}]
    context_has_finding = any(
        "automatic_memory_findings" in step.get("context_seen", {})
        for step in trace.steps
    )
    should_advisory = mode in {"require_justification", "soft_block_exact_repeat"} and case != "justified_repeat_read"
    should_surface = mode != "off"
    should_allow_justified = case == "justified_repeat_read"
    passed = True
    reasons: list[str] = []
    if should_surface and not automatic:
        passed = False; reasons.append("automatic_memory_not_surfaced")
    if not should_surface and automatic:
        passed = False; reasons.append("automatic_memory_surfaced_when_off")
    if should_advisory and not blocks:
        passed = False; reasons.append("repeat_advisory_not_emitted")
    if not should_advisory and blocks:
        passed = False; reasons.append("unexpected_advisory")
    if should_allow_justified and mode != "off" and len(reads) < 2:
        passed = False; reasons.append("justified_repeat_not_dispatched")
    if should_surface and not context_has_finding:
        passed = False; reasons.append("context_missing_automatic_memory_findings")
    return {
        "case": case,
        "mode": mode,
        "passed": passed,
        "reasons": reasons,
        "read_count": len(reads),
        "command_count": len(commands),
        "automatic_memory_count": len(automatic),
        "advisory_count": len(blocks),
        "block_count": 0,
        "context_has_automatic_memory": context_has_finding,
    }


def run(out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for case in ("repeat_read", "repeat_command", "justified_repeat_read"):
        for mode in POLICY_MODES:
            trace = RunTrace()
            executor = MemoryExecutor(files={"input.txt": "alpha"}, workspace_root="/app")
            hooks = _Hooks(_runtime(mode), _case_turns(case))
            result = AetherNextKernel(max_steps=3).run(_env(), executor, hooks, trace=trace)
            row = _score(case, mode, result.receipts, trace)
            rows.append(row)
            case_dir = out_dir / case / mode
            case_dir.mkdir(parents=True, exist_ok=True)
            (case_dir / "receipts.json").write_text(json.dumps([r.__dict__ for r in result.receipts], indent=2, sort_keys=True, default=str))
            (case_dir / "trace.json").write_text(json.dumps(trace.to_dict(), indent=2, sort_keys=True, default=str))
            (case_dir / "row.json").write_text(json.dumps(row, indent=2, sort_keys=True))
    summary = {
        "schema_version": "aether_next.automatic_memory_diagnostic.v1",
        "rows": rows,
        "counts": {
            "rows": len(rows),
            "passed": sum(1 for row in rows if row["passed"]),
            "failed": sum(1 for row in rows if not row["passed"]),
        },
    }
    (out_dir / "automatic_memory_diagnostic_eval.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    report = ["# Automatic Memory Diagnostic Eval", ""]
    report.append("| case | mode | passed | reads | commands | automatic | advisories | reasons |")
    report.append("|---|---|---:|---:|---:|---:|---:|---|")
    for row in rows:
        report.append(
            f"| {row['case']} | {row['mode']} | {row['passed']} | {row['read_count']} | {row['command_count']} | "
            f"{row['automatic_memory_count']} | {row.get('advisory_count', row['block_count'])} | {', '.join(row['reasons']) or 'none'} |"
        )
    (out_dir / "AUTOMATIC_MEMORY_DIAGNOSTIC_REPORT.md").write_text("\n".join(report) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="automatic_memory_diagnostic_eval")
    args = parser.parse_args()
    print(json.dumps(run(Path(args.out_dir)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
