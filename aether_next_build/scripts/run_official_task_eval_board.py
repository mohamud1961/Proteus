#!/usr/bin/env python3
"""Plan or execute stratified official-task harness boards.

Task taxonomy in evals/official_task_board.v1.json is evaluation-only. This
runner never passes family labels or expected strategies into the harness.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

_BUILD_ROOT = Path(__file__).resolve().parents[1]
if str(_BUILD_ROOT) not in sys.path:
    sys.path.insert(0, str(_BUILD_ROOT))

from aether_next.evidence_finalization import (  # noqa: E402
    executing_source_identity,
    finalize_evidence_directory,
    sha256_file,
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _task_ids(board: dict[str, Any], board_name: str) -> list[str]:
    if board_name == "smoke":
        return [str(item["task_id"]) for item in board["smoke_board"]]
    return [str(item) for item in board["full_board"]]


def _validate_task_dirs(tasks_dir: Path, task_ids: list[str]) -> list[str]:
    findings: list[str] = []
    for task_id in task_ids:
        task_dir = tasks_dir / task_id
        if not task_dir.is_dir():
            findings.append(f"missing task directory: {task_id}")
            continue
        if not (task_dir / "instruction.md").is_file():
            findings.append(f"missing public instruction.md: {task_id}")
        if not ((task_dir / "task.toml").is_file() or (task_dir / "task.yaml").is_file()):
            findings.append(f"missing public task manifest: {task_id}")
    return findings


def _require_deterministic_pass(path: Path | None) -> dict[str, Any]:
    if path is None:
        raise RuntimeError("--deterministic-summary is required with --allow-model")
    payload = _load_json(path)
    if not bool(payload.get("passed", False)) or payload.get("required_failures"):
        raise RuntimeError("deterministic certification summary is not a clean pass")
    return payload


def _aggregate(rows_by_sample: list[list[dict[str, Any]]]) -> dict[str, Any]:
    all_rows = [row for rows in rows_by_sample for row in rows]
    official_passes = sum(1 for row in all_rows if float(row.get("reward", 0) or 0) >= 1.0)
    false_cleans = [
        row for row in all_rows
        if row.get("kernel_status") == "completed" and float(row.get("reward", 0) or 0) < 1.0
    ]
    false_blocks = [
        row for row in all_rows
        if row.get("kernel_status") != "completed" and float(row.get("reward", 0) or 0) >= 1.0
    ]
    protocol_failures = [
        row for row in all_rows
        if row.get("error") in {
            "multiple_distinct_assistant_outputs",
            "provider_output_incomplete",
            "provider_structured_output_invalid_json",
        }
        or row.get("classifier_label") in {"provider_protocol_failure", "solver_protocol_failure"}
    ]
    evidence_not_final = [
        row for row in all_rows
        if (row.get("evidence_finalization") or {}).get("status") != "finalized"
    ]
    return {
        "attempt_count": len(all_rows),
        "official_passes": official_passes,
        "official_pass_rate": official_passes / len(all_rows) if all_rows else 0.0,
        "false_clean_count": len(false_cleans),
        "false_block_count": len(false_blocks),
        "provider_protocol_failure_count": len(protocol_failures),
        "evidence_not_finalized_count": len(evidence_not_final),
        "false_clean_tasks": [row.get("task") for row in false_cleans],
        "false_block_tasks": [row.get("task") for row in false_blocks],
        "strict_integrity_pass": not false_cleans and not false_blocks and not evidence_not_final,
        "perfect_official_board": bool(all_rows) and official_passes == len(all_rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--tasks", default="", help="Optional comma-separated subset of official task IDs.")
    parser.add_argument("--board-file", default=str(_BUILD_ROOT / "evals" / "official_task_board.v1.json"))
    parser.add_argument("--tasks-dir", required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--effort", choices=("none", "low", "medium", "high", "xhigh"), default="low")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--run-timeout-s", type=int, default=1800)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--deterministic-summary", default=None)
    parser.add_argument("--allow-model", action="store_true")
    parser.add_argument("--network-scope", choices=("loopback_only", "external_unrestricted"), default=None)
    args = parser.parse_args(argv)

    if args.samples < 1:
        parser.error("--samples must be >= 1")
    board_path = Path(args.board_file).resolve()
    board = _load_json(board_path)
    tasks = _task_ids(board, args.board)
    if args.tasks:
        requested = [item.strip() for item in args.tasks.split(",") if item.strip()]
        unknown = sorted(set(requested) - set(board.get("full_board", [])))
        if unknown:
            print(json.dumps({"status": "invalid_task_selection", "unknown_tasks": unknown}, indent=2))
            return 2
        tasks = requested
    tasks_dir = Path(args.tasks_dir).resolve()
    findings = _validate_task_dirs(tasks_dir, tasks)
    if findings:
        print(json.dumps({"status": "invalid_task_corpus", "findings": findings}, indent=2))
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = Path(args.output_dir).resolve() if args.output_dir else _BUILD_ROOT / f"official_{args.board}_board_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    source = executing_source_identity(_BUILD_ROOT)
    plan = {
        "schema": "aether.official_task_eval_plan.v1",
        "board": args.board,
        "board_sha256": sha256_file(board_path),
        "official_archive_sha256": board["source"]["archive_sha256"],
        "task_count": len(tasks),
        "tasks": tasks,
        "samples": args.samples,
        "effort": args.effort,
        "max_steps": args.max_steps,
        "run_timeout_s": args.run_timeout_s,
        "network_scope": args.network_scope,
        "taxonomy_delivery_to_harness": False,
        "source_identity": source,
        "model_execution_allowed": bool(args.allow_model),
    }
    plan_path = out / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True, default=str), encoding="utf-8")

    if not args.allow_model:
        marker = finalize_evidence_directory(
            out,
            required_paths=(plan_path,),
            metadata={"status": "plan_only", "source_commit": source.get("commit", "")},
        )
        print(json.dumps({"status": "plan_only", "output_dir": str(out), "task_count": len(tasks), "final_marker": marker}, indent=2))
        return 0

    deterministic = _require_deterministic_pass(
        Path(args.deterministic_summary).resolve() if args.deterministic_summary else None
    )
    deterministic_copy = out / "deterministic_summary.json"
    deterministic_copy.write_text(json.dumps(deterministic, indent=2, sort_keys=True), encoding="utf-8")

    rows_by_sample: list[list[dict[str, Any]]] = []
    logs: list[str] = []
    task_arg = ",".join(tasks)
    for sample in range(1, args.samples + 1):
        sample_dir = out / f"sample_{sample:02d}"
        sample_dir.mkdir(parents=True, exist_ok=True)
        results_path = sample_dir / "results.json"
        trace_dir = sample_dir / "traces"
        command = [
            sys.executable,
            str(_BUILD_ROOT / "run_pilot.py"),
            "--tasks", task_arg,
            "--tasks-dir", str(tasks_dir),
            "--effort", args.effort,
            "--max-steps", str(args.max_steps),
            "--run-timeout-s", str(args.run_timeout_s),
            "--trace-dir", str(trace_dir),
            "--out", str(results_path),
            "--provenance-mode", "production",
        ]
        if args.network_scope:
            command.extend(["--network-scope", args.network_scope])
        proc = subprocess.run(
            command,
            cwd=_BUILD_ROOT,
            capture_output=True,
            text=True,
            errors="replace",
        )
        (sample_dir / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
        (sample_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
        logs.append(f"sample {sample}: exit={proc.returncode}")
        if not results_path.is_file():
            print(json.dumps({"status": "sample_runner_failed", "sample": sample, "command": command, "logs": logs}, indent=2))
            return 3
        rows = _load_json(results_path)
        if not isinstance(rows, list):
            print(json.dumps({"status": "invalid_sample_results", "sample": sample}, indent=2))
            return 3
        rows_by_sample.append(rows)

    aggregate = _aggregate(rows_by_sample)
    summary = {
        "schema": "aether.official_task_eval_result.v1",
        "plan": plan,
        "deterministic_gate": {
            "passed": deterministic.get("passed"),
            "summary_sha256": sha256_file(deterministic_copy),
        },
        "aggregate": aggregate,
        "samples": rows_by_sample,
    }
    summary_path = out / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    marker = finalize_evidence_directory(
        out,
        required_paths=(plan_path, deterministic_copy, summary_path, *(out / f"sample_{sample:02d}" for sample in range(1, args.samples + 1))),
        metadata={
            "status": "completed",
            "board": args.board,
            "strict_integrity_pass": aggregate["strict_integrity_pass"],
            "perfect_official_board": aggregate["perfect_official_board"],
            "source_commit": source.get("commit", ""),
        },
    )
    print(json.dumps({"status": "completed", "output_dir": str(out), "aggregate": aggregate, "final_marker": marker}, indent=2, sort_keys=True))
    return 0 if aggregate["strict_integrity_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
