#!/usr/bin/env python3
"""Known-bad verifier eval: replay one verification round over frozen sentinel state.

Falsifiability gate for the completion-evidence protocol (Road v2 P2): a
verifier mechanism that cannot fail a known-bad workspace is theater. The
fixtures are the REAL 2026-07-07 sentinel false-clean snapshots plus one
known-good snapshot:

  kv-store-grpc   known-bad  (proto declares SetValRequest.val; task requires value)
  gcode-to-text   known-bad  (out.txt holds M486 comment text, not the decoded toolpath flag)
  video-processing known-bad (output.toml frames 72/90; official ranges 50-54/62-64)
  log-summary-date-ranges known-good (official grader passed)

Predictions of record (audit addendum, 2026-07-08): known-bads convert to a
non-completed verdict; known-good stays completed. A miss is recorded as a
failed prediction, never reinterpreted.

Modes:
  --mode dry    build packet + inspector over the snapshot, ZERO model calls
                (plumbing proof; used by the test suite)
  --mode model  live verifier round via Azure (run under the run protocol:
                launched/monitored by the designated monitor agent)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

_BUILD_DIR = str(Path(__file__).resolve().parent.parent)
if _BUILD_DIR not in sys.path:
    sys.path.insert(0, _BUILD_DIR)

from aether_next.evidence_finalization import (  # noqa: E402
    executing_source_identity,
    finalize_evidence_directory,
)
from aether_next.model_hooks import ModelHooks, ModelOutputError  # noqa: E402
from aether_next.providers.azure_model import AzureModelError  # noqa: E402
from aether_next.real_executor import SubprocessExecutor  # noqa: E402
from aether_next.runners.docker_exec_executor import DockerExecExecutor  # noqa: E402
from aether_next.runtime_ir import EnvMap  # noqa: E402
from aether_next.verifier import parse_model_verifier_result  # noqa: E402
from aether_next.verifier_inspector import (  # noqa: E402
    VerifierInspectionRequest,
    execute_verifier_inspection_requests,
)
from aether_next.verifier_overlay import VerifierOverlay  # noqa: E402
from aether_next.verifier_packets import build_verifier_packet  # noqa: E402
from run_trace_verifier_replay_ab import (  # noqa: E402
    _compiled_from_trace,
    _ledger_from_trace,
    _load_json,
    _trace_root,
)

_RUNS = Path(_BUILD_DIR) / "vm_goal_runs"
_MODEL_TEXT_TELEMETRY_FIELDS = frozenset({
    "text",
    "raw_verifier_output",
    "assistant_output",
    "content",
})

DEFAULT_CASES: tuple[tuple[str, str, str], ...] = (
    ("kv-store-grpc", "20260707T162100Z_sentinel_steps200", "known_bad"),
    ("gcode-to-text", "20260707T152214Z_sentinel", "known_bad"),
    ("video-processing", "20260707T152214Z_sentinel", "known_bad"),
    ("log-summary-date-ranges", "20260707T152214Z_sentinel", "known_good"),
    ("code-from-image", "20260707T162100Z_sentinel_steps200", "known_good"),
)


def _hash_only_provider_telemetry(
    rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep provider route metadata while excluding model-authored text."""
    return [
        {
            key: value
            for key, value in row.items()
            if key not in _MODEL_TEXT_TELEMETRY_FIELDS
        }
        for row in rows
        if isinstance(row, Mapping)
    ]


def _inspection_environment_validity(
    inspection_rounds: list[dict[str, Any]],
) -> tuple[bool, tuple[str, ...]]:
    """Classify evaluator-owned inspection failures before scoring a verdict.

    A frozen-state replay is not a semantic model measurement when its own
    workspace translation failed.  This intentionally checks only generic
    execution symptoms (a command trying to enter an absent workspace), not
    task names, expected answers, or model verdict text.
    """
    issues: list[str] = []
    for round_data in inspection_rounds:
        for row in round_data.get("results", ()):
            if not isinstance(row, Mapping):
                continue
            text = "\n".join(
                str(row.get(key, ""))
                for key in ("error", "stderr", "stdout")
            ).lower()
            if "no such file or directory" in text and ("cd:" in text or "workspace" in text):
                issues.append("inspection_workspace_path_unavailable")
            if (
                str(row.get("kind", "")) == "perceive_artifact"
                and "no vision model available for perceive_artifact" in text
            ):
                issues.append("inspection_vision_route_unavailable")
    return (not issues, tuple(sorted(set(issues))))


def _historical_launch_commands(trace: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract explicit background launches from recorded solver actions.

    This is a replay fixture operation, not a planner: it replays only command
    fragments that the historical trace already executed and labels them as
    evaluator-owned setup.  It intentionally does not infer a service command
    from a task name, source filename, or model output.
    """
    commands: list[str] = []
    source_commands: list[str] = []
    for step in trace.get("steps", ()) or ():
        turn = step.get("turn", {}) if isinstance(step, Mapping) else {}
        for action in turn.get("actions", ()) if isinstance(turn, Mapping) else ():
            if not isinstance(action, Mapping):
                continue
            source_commands.append(str((action.get("arguments") or {}).get("command", "")))
        # Trace action arguments can be deliberately truncated for context
        # hygiene, while execution receipts retain the complete command.  The
        # receipt is execution authority, so use it as a fallback rather than
        # guessing the omitted launch fragment.
        for observation in step.get("observations", ()) if isinstance(step, Mapping) else ():
            if not isinstance(observation, Mapping):
                continue
            summary = str(observation.get("summary", ""))
            if summary.startswith("command exit=") and ":" in summary:
                source_commands.append(summary.split(":", 1)[1])
    for command in source_commands:
        for line in command.splitlines():
            stripped = line.strip()
            if stripped.startswith("nohup ") and stripped.endswith("&"):
                commands.append(stripped)
    return tuple(dict.fromkeys(commands))


def _restore_historical_launches(
    executor: DockerExecExecutor,
    commands: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Replay recorded launches when, and only when, the trace recorded one.

    A frozen task may be a file-only case with no background process.  The
    absence of a launch command is therefore evaluator metadata, not a fatal
    replay error.  It is retained in the row so a later audit can distinguish
    "nothing to restore" from an attempted launch that failed.
    """
    if not commands:
        return [{
            "kind": "historical_process_restore",
            "status": "not_applicable",
            "reason": "no_explicit_background_launch_in_trace",
        }]

    receipts: list[dict[str, Any]] = []
    for command in commands:
        result = executor.run_command(command, timeout_s=60)
        receipts.append({
            "kind": "historical_process_restore",
            "status": "attempted",
            "command": command,
            "success": result.success,
            "exit_code": result.exit_code,
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:4000],
        })
    return receipts


def _start_container_replay(image: str, workspace: str) -> str:
    """Start an isolated task-image container with the snapshot mounted at /app."""
    if not image.strip():
        raise RuntimeError("trace has no task image for container replay")
    name = f"aether-knownbad-{uuid.uuid4().hex[:12]}"
    proc = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-v", f"{os.path.abspath(workspace)}:/app",
            "-w", "/app", image, "sleep", "infinity",
        ],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"container replay start failed: {(proc.stderr or proc.stdout).strip()[:1000]}")
    return proc.stdout.strip()


def _stop_container_replay(container_id: str) -> None:
    if container_id:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )


def _final_submit_step(trace: Mapping[str, Any]) -> int:
    steps = trace.get("steps", []) or []
    for item in reversed(steps):
        turn = item.get("turn", {}) if isinstance(item, dict) else {}
        if isinstance(turn, dict) and turn.get("kind") == "submit_outcome":
            return int(item.get("step", len(steps)))
    return len(steps)


def _load_case(task: str, run_dir: str) -> dict[str, Any]:
    base = _RUNS / run_dir
    trace_path = base / "traces" / f"{task}.trace.json"
    snapshot = base / "snapshots" / task / "final"
    if not trace_path.exists():
        raise FileNotFoundError(f"trace missing: {trace_path}")
    if not snapshot.is_dir():
        raise FileNotFoundError(f"snapshot missing: {snapshot}")
    trace = _trace_root(trace_path)
    config = trace.get("architect_config", {})
    if not isinstance(config, dict) or not config.get("verifier_identity_prompt"):
        raise ValueError(f"{task}: trace architect_config lacks verifier_identity_prompt")
    compiled = _compiled_from_trace(
        task=task,
        trace=trace,
        verifier_prompt=str(config["verifier_identity_prompt"]),
        evidence_requirements=tuple(config.get("evidence_requirements", ()) or ()),
        false_positive_risks=tuple(config.get("false_positive_risks", ()) or ()),
        minimum_completion_evidence=tuple(config.get("minimum_completion_evidence", ()) or ()),
    )
    submit_step = _final_submit_step(trace)
    ledger = _ledger_from_trace(trace, replay_step=submit_step + 1)
    return {
        "task": task,
        "run_dir": run_dir,
        "trace_path": str(trace_path),
        "snapshot": str(snapshot),
        "compiled": compiled,
        "ledger": ledger,
        "submit_step": submit_step,
        "task_image": str(trace.get("image", "")),
        "historical_launch_commands": _historical_launch_commands(trace),
    }


def _workspace_copy(snapshot: str, scratch_root: Path) -> str:
    # The snapshot is committed evidence: never point an executor (or its
    # overlay sibling directories) at it. Copy first.
    # parts[-2] is the task name (e.g. "kv-store-grpc"), parts[-1] is "final"
    target = scratch_root / Path(snapshot).parts[-2]
    shutil.copytree(snapshot, target)
    return str(target)


def _run_case(
    case: Mapping[str, Any],
    *,
    mode: str,
    verifier_model: Callable[..., str] | None,
    vision_model: Callable[..., str] | None,
    scratch_root: Path,
    runtime_mode: str,
    restore_live_processes: bool,
) -> dict[str, Any]:
    compiled = case["compiled"]
    ledger = case["ledger"]
    workspace = _workspace_copy(case["snapshot"], scratch_root)
    container_id = ""
    setup_receipts: list[dict[str, Any]] = []
    if runtime_mode == "container_replay":
        container_id = _start_container_replay(str(case["task_image"]), workspace)
        executor = DockerExecExecutor(container_id, workspace, container_workdir="/app")
        envmap = EnvMap(task_prompt=compiled.task_prompt, workspace_root="/app")
        overlay = VerifierOverlay(executor, "/app")
        if restore_live_processes:
            setup_receipts.extend(_restore_historical_launches(
                executor, tuple(case["historical_launch_commands"]),
            ))
    else:
        executor = SubprocessExecutor(workspace)
        envmap = EnvMap(task_prompt=compiled.task_prompt, workspace_root=workspace)
        overlay = VerifierOverlay(executor, workspace)
    hooks_for_inspection = None
    if vision_model is not None:
        hooks_for_inspection = ModelHooks(
            architect_model=lambda m, *, max_output_tokens=8000: "{}",
            solver_model=lambda m, *, max_output_tokens=8000: "{}",
            vision_model=vision_model,
        )

    inspection_rounds: list[dict[str, Any]] = []

    def inspector(requests: tuple[VerifierInspectionRequest, ...]) -> list[dict[str, Any]]:
        results = execute_verifier_inspection_requests(
            requests,
            compiled=compiled,
            ledger=ledger,
            executor=executor,
            envmap=envmap,
            overlay=overlay,
            hooks=hooks_for_inspection,
        )
        inspection_rounds.append({
            "requests": [asdict(request) for request in requests],
            "results": results,
        })
        return results

    packet = build_verifier_packet(
        compiled,
        ledger,
        step=int(case["submit_step"]),
        reason="solver_submit",
        envmap=envmap,
    )
    row: dict[str, Any] = {
        "task": case["task"],
        "run_dir": case["run_dir"],
        "expectation": case["expectation"],
        "snapshot": case["snapshot"],
        "packet_bytes": len(json.dumps(packet, default=str)),
        "packet_handles": len(packet.get("state_inspection_handles", []) or []),
        "runtime_mode": runtime_mode,
        "runtime_setup": setup_receipts,
    }
    hooks: ModelHooks | None = None
    try:
        if mode == "dry":
            # Plumbing proof without a model: execute one representative
            # read-only inspection against the copied snapshot.
            probe = inspector((
                VerifierInspectionRequest(request_id="dry-probe", kind="read_file", path="."),
            ))
            row.update({
                "mode": "dry",
                "dry_inspection_ok": bool(probe),
                "verdict": "",
                "prediction": "not_evaluated_dry_mode",
            })
            return row
        assert verifier_model is not None
        model_run_id = f"verifier-knownbad:{uuid.uuid4().hex}"
        hooks = ModelHooks(
            architect_model=lambda m, *, max_output_tokens=8000: "{}",
            solver_model=lambda m, *, max_output_tokens=8000: "{}",
            verifier_model=verifier_model,
            vision_model=vision_model,
            run_id=model_run_id,
            task_id=str(case["task"]),
        )
        row["model_run_id"] = model_run_id
        raw = hooks.verify_with_inspector(packet, compiled, ledger, inspector=inspector)
        parsed = parse_model_verifier_result(raw)
        converted = parsed.verdict != "completed"
        expected_converted = case["expectation"] == "known_bad"
        row.update({
            "mode": "model",
            "verdict": parsed.verdict,
            "confidence": parsed.confidence,
            "summary": parsed.summary,
            "findings": [asdict(f) for f in parsed.findings],
            "completion_evidence": [asdict(e) for e in parsed.completion_evidence],
            "inspections_performed": sum(
                len(round_data["results"]) for round_data in inspection_rounds
            ),
            "prediction": "HIT" if converted == expected_converted else "MISS",
        })
        valid, issues = _inspection_environment_validity(inspection_rounds)
        row.update({
            "measurement_valid": valid,
            "measurement_issues": list(issues),
            "raw_verifier_output": raw,
            "inspection_rounds": inspection_rounds,
        })
        if not valid:
            row["prediction"] = "INVALID_ENVIRONMENT"
        return row
    except (AzureModelError, ModelOutputError) as exc:
        valid, issues = _inspection_environment_validity(inspection_rounds)
        row.update({
            "mode": "model",
            "measurement_valid": False,
            "measurement_issues": list(issues) if not valid else ["provider_or_model_output_invalid"],
            "prediction": "INVALID_ENVIRONMENT" if not valid else "INVALID_PROVIDER",
            "provider_error_type": type(exc).__name__,
            "provider_error": str(exc),
            "inspection_rounds": inspection_rounds,
        })
        return row
    finally:
        if hooks is not None:
            # Provider receipts are diagnostic evidence, not model content:
            # preserve route/status/job/usage metadata and hash-only output
            # identity so a bounded-loop failure can distinguish provider
            # sequencing from repeated verifier inspection requests.
            row["model_call_telemetry"] = _hash_only_provider_telemetry(
                hooks.drain_model_telemetry()
            )
            row["quarantined_model_call_telemetry"] = _hash_only_provider_telemetry(
                hooks.drain_quarantined_model_telemetry()
            )
        overlay.teardown()
        _stop_container_replay(container_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["dry", "model"], required=True)
    parser.add_argument(
        "--runtime-mode",
        choices=["frozen_subprocess", "container_replay"],
        default="frozen_subprocess",
        help="Replay frozen files locally or mount them at /app in the trace task image.",
    )
    parser.add_argument(
        "--restore-live-processes",
        action="store_true",
        help="Replay explicit historical nohup launches inside a container replay.",
    )
    parser.add_argument("--tasks", default=",".join(case[0] for case in DEFAULT_CASES))
    parser.add_argument("--deploy-env", default="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT")
    parser.add_argument("--key-env", default="AZURE_OPENAI_GPT54_MINI_KEY")
    parser.add_argument("--endpoint-env", default="AZURE_OPENAI_ENDPOINT")
    parser.add_argument("--vision-deploy-env", default="")
    parser.add_argument("--out-dir", type=Path, default=Path("verifier_knownbad_eval_out"))
    args = parser.parse_args()

    verifier_model = None
    vision_model = None
    if args.mode == "model":
        from aether_next.providers.azure_model import make_azure_callable

        verifier_model = make_azure_callable(
            deployment_env=args.deploy_env, key_env=args.key_env, endpoint_env=args.endpoint_env,
            role="verifier",
        )
        if args.vision_deploy_env:
            vision_model = make_azure_callable(
                deployment_env=args.vision_deploy_env, key_env=args.key_env,
                endpoint_env=args.endpoint_env,
            )

    wanted = {name.strip() for name in args.tasks.split(",") if name.strip()}
    rows: list[dict[str, Any]] = []
    scratch_root = Path(tempfile.mkdtemp(prefix="knownbad_eval_"))
    try:
        for task, run_dir, expectation in DEFAULT_CASES:
            if task not in wanted:
                continue
            case = _load_case(task, run_dir)
            case["expectation"] = expectation
            rows.append(_run_case(
                case, mode=args.mode, verifier_model=verifier_model,
                vision_model=vision_model, scratch_root=scratch_root,
                runtime_mode=args.runtime_mode,
                restore_live_processes=args.restore_live_processes,
            ))
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    source = executing_source_identity(_BUILD_DIR)
    source_path = args.out_dir / "source_identity.json"
    source_path.write_text(json.dumps(source, indent=2, sort_keys=True), encoding="utf-8")
    (args.out_dir / "knownbad_eval_rows.json").write_text(
        json.dumps({"rows": rows}, indent=2, sort_keys=True, default=str), encoding="utf-8",
    )
    lines = ["# Known-bad Verifier Eval", "", "| Task | Expectation | Verdict | Prediction |", "|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['task']} | {row['expectation']} | {row.get('verdict','')} | {row.get('prediction','')} |")
    report_path = args.out_dir / "KNOWNBAD_EVAL.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    marker = finalize_evidence_directory(
        args.out_dir,
        required_paths=(source_path, args.out_dir / "knownbad_eval_rows.json", report_path),
        metadata={
            "status": "invalid" if any(
                str(row.get("prediction", "")).startswith("INVALID_") for row in rows
            ) else "completed",
            "source_commit": source.get("commit", ""),
        },
    )
    print(json.dumps({"rows": len(rows), "out_dir": str(args.out_dir), "final_marker": marker}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
