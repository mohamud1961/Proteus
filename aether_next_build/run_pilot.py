#!/usr/bin/env python3
"""Pilot runner: drive Aether-Next against official Terminal-Bench tasks.

Usage::

    python3.11 run_pilot.py \\
        --tasks adaptive-rejection-sampler,path-tracing \\
        --out results.json

Or as a module from the build dir::

    python3.11 -m run_pilot --tasks adaptive-rejection-sampler --out results.json

Env vars required for Azure models (defaults target GPT-5.4-Mini):

    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_GPT54_MINI_DEPLOYMENT
    AZURE_OPENAI_GPT54_MINI_KEY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Ensure the build dir is importable when run as a script.
_BUILD_DIR = str(Path(__file__).resolve().parent)
if _BUILD_DIR not in sys.path:
    sys.path.insert(0, _BUILD_DIR)

from aether_next.providers.azure_model import make_azure_callable  # noqa: E402
from aether_next.run_adapter import ensure_certified_architect_mode  # noqa: E402
from aether_next.runners.docker_runner import run_tbench_task  # noqa: E402
from aether_next.task_metadata_loader import declared_docker_image  # noqa: E402


_OFFICIAL_TASKS_DIR = str(
    Path(__file__).resolve().parent.parent / "official_tasks"
)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_hash(root: Path, *, include_suffixes: tuple[str, ...] = (".py", ".md", ".toml", ".json")) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in rel or rel.startswith("local_goal_runs/") or rel.startswith("verifier_only_eval_") or rel.startswith("architect_only_eval_") or rel.startswith("deterministic_integration_eval_"):
            continue
        if include_suffixes and path.suffix not in include_suffixes:
            continue
        h.update(rel.encode("utf-8") + b"\\0")
        h.update(_hash_file(path).encode("ascii") + b"\\0")
    return h.hexdigest()


def _git_info(root: Path) -> dict[str, Any]:
    def run(args: list[str]) -> str:
        return subprocess.check_output(["git", *args], cwd=str(root), stderr=subprocess.DEVNULL, text=True).strip()
    try:
        sha = run(["rev-parse", "HEAD"])
        branch = run(["branch", "--show-current"])
        dirty = bool(run(["status", "--porcelain"]))
        return {"git_available": True, "code_sha": sha, "branch": branch, "dirty": dirty}
    except Exception:
        return {"git_available": False, "code_sha": "", "branch": "", "dirty": None}


def _build_run_provenance(args: argparse.Namespace) -> dict[str, Any]:
    build_root = Path(__file__).resolve().parent
    git = _git_info(build_root)
    tree_hash = _tree_hash(build_root / "aether_next")
    prompt_hashes = {
        "run_args": hashlib.sha256(json.dumps(vars(args), sort_keys=True, default=str).encode("utf-8")).hexdigest(),
    }
    return {
        "schema_version": "aether_run_provenance.v1",
        "provenance_mode": args.provenance_mode,
        **git,
        "code_tree_hash": tree_hash,
        "prompt_hashes": prompt_hashes,
        "model_params": {
            "solver_deploy_env": args.solver_deploy_env,
            "architect_deploy_env": args.architect_deploy_env,
            "endpoint_env": args.endpoint_env,
            "effort": args.effort,
        },
    }


def _task_hash(task_dir: str) -> str:
    return _tree_hash(Path(task_dir), include_suffixes=())


def _task_image_tag(task_dir: str) -> str:
    task_path = Path(task_dir).resolve()
    safe_name = "".join(ch if ch.isalnum() else "-" for ch in task_path.name.lower()).strip("-")
    return f"aether-next-task-{safe_name}-{_task_hash(str(task_path))[:12]}"


def _build_task_image(task_dir: str, image_tag: str) -> None:
    task_path = Path(task_dir).resolve()
    dockerfile = task_path / "Dockerfile"
    if not dockerfile.exists():
        raise FileNotFoundError(
            f"no declared docker image and no Dockerfile found in {task_dir}"
        )
    inspect = subprocess.run(
        ["docker", "image", "inspect", image_tag],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
    )
    if inspect.returncode == 0:
        return
    build_timeout_s = int(os.environ.get("AETHER_TASK_IMAGE_BUILD_TIMEOUT_S", "1800"))
    build = subprocess.run(
        ["docker", "build", "-t", image_tag, str(task_path)],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=max(1, build_timeout_s),
    )
    if build.returncode != 0:
        detail = (build.stdout + build.stderr).strip()[-4000:]
        raise RuntimeError(f"docker build failed for {task_path}: {detail}")


def _resolve_task_image(task_dir: str) -> str:
    """Resolve a real runnable image for TOML/YAML task layouts."""
    declared = declared_docker_image(task_dir)
    if declared:
        return declared
    image_tag = _task_image_tag(task_dir)
    _build_task_image(task_dir, image_tag)
    return image_tag


def _read_docker_image(task_dir: str) -> str:
    """Compatibility wrapper for tests and older callers."""
    return _resolve_task_image(task_dir)


def _print_summary(records: list[dict[str, Any]]) -> None:
    """Print a final summary table to stdout."""
    header = f"{'task':<40} {'reward':>7}  {'classifier_label'}"
    print("\n" + "=" * len(header))
    print(header)
    print("-" * len(header))
    for rec in records:
        task = rec.get("task", "?")[:40]
        reward = rec.get("reward", "?")
        label = rec.get("classifier_label", "?")
        print(f"{task:<40} {reward:>7}  {label}")
    print("=" * len(header))
    total = len(records)
    wins = sum(1 for r in records if r.get("reward", 0) == 1.0)
    print(f"Total: {wins}/{total} tasks rewarded.\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pilot runner for Aether-Next on Terminal-Bench tasks.",
    )
    ap.add_argument(
        "--tasks",
        required=True,
        help="Comma-separated task names under official_tasks/.",
    )
    ap.add_argument(
        "--tasks-dir",
        default=_OFFICIAL_TASKS_DIR,
        help=f"Root directory containing task folders (default: {_OFFICIAL_TASKS_DIR}).",
    )
    ap.add_argument(
        "--solver-deploy-env",
        default="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT",
        help="Env var name for solver deployment (default: AZURE_OPENAI_GPT54_MINI_DEPLOYMENT).",
    )
    ap.add_argument(
        "--solver-key-env",
        default="AZURE_OPENAI_GPT54_MINI_KEY",
        help="Env var name for solver API key (default: AZURE_OPENAI_GPT54_MINI_KEY).",
    )
    ap.add_argument(
        "--architect-deploy-env",
        default="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT",
        help="Env var name for architect deployment (default: AZURE_OPENAI_GPT54_MINI_DEPLOYMENT).",
    )
    ap.add_argument(
        "--architect-key-env",
        default="AZURE_OPENAI_GPT54_MINI_KEY",
        help="Env var name for architect API key (default: AZURE_OPENAI_GPT54_MINI_KEY).",
    )
    ap.add_argument(
        "--endpoint-env",
        default="AZURE_OPENAI_ENDPOINT",
        help="Env var name for Azure endpoint (default: AZURE_OPENAI_ENDPOINT).",
    )
    ap.add_argument(
        "--effort",
        default="medium",
        choices=["none", "low", "medium", "high", "xhigh"],
        help="Reasoning effort for model calls (default: medium).",
    )
    ap.add_argument(
        "--max-steps",
        type=int,
        default=30,
        help="Max kernel steps per task (default: 30).",
    )
    ap.add_argument(
        "--run-timeout-s",
        type=int,
        default=1800,
        help="Timeout in seconds for Docker command/grader execution (default: 1800).",
    )
    ap.add_argument(
        "--network-scope",
        choices=["loopback_only", "external_unrestricted"],
        default=None,
        help="Optional explicit pre-container network scope. Omit to use public task metadata/default policy.",
    )
    ap.add_argument(
        "--trace-dir",
        default=None,
        help="Directory to write per-task JSON trace files for step-by-step audit. "
        "When set, each task emits <task>.trace.json with architect config, "
        "solver context/turn/observations, gate decisions, and reconfigurations.",
    )
    ap.add_argument(
        "--architect-mode",
        default="workbench",
        choices=["workbench"],
        help="Certified architect mode (legacy ir/contract modes are quarantined in reference_legacy).",
    )
    ap.add_argument(
        "--vision-deploy-env",
        default="",
        help="Optional env var naming a vision-capable deployment; when set, "
        "the solver gains real image transcription via inspect_artifact.",
    )
    ap.add_argument(
        "--vision-key-env",
        default="",
        help="Env var for the vision deployment API key (defaults to the solver key env).",
    )
    ap.add_argument(
        "--snapshot-dir",
        default=None,
        help="Directory to write workspace snapshots for resumable replay. "
        "When set, the final container /app is copied to <dir>/<task>/final/.",
    )
    ap.add_argument(
        "--snapshot-steps",
        default="",
        help="Comma-separated step numbers at which to snapshot mid-run "
        "(e.g. '1,5,10'). Requires --snapshot-dir.",
    )
    ap.add_argument(
        "--provenance-mode",
        default="production",
        choices=["production", "test_untrusted"],
        help="Production/model-backed runs require resolvable code SHA or tree hash; local tests may use test_untrusted.",
    )
    ap.add_argument(
        "--out",
        default="pilot_results.json",
        help="Output JSON file for results (default: pilot_results.json).",
    )

    args = ap.parse_args(argv)
    task_names = [t.strip() for t in args.tasks.split(",") if t.strip()]
    snap_steps = tuple(
        int(s) for s in args.snapshot_steps.split(",") if s.strip()
    ) if args.snapshot_steps else ()
    run_provenance = _build_run_provenance(args)
    if args.provenance_mode == "production" and not (run_provenance.get("code_sha") or run_provenance.get("code_tree_hash")):
        print("error: unresolved production provenance", file=sys.stderr)
        return 2

    if not task_names:
        print("error: no task names given", file=sys.stderr)
        return 1

    try:
        ensure_certified_architect_mode(args.architect_mode)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Build model callables (reads env vars now, fails fast if missing).
    solver_model = make_azure_callable(
        deployment_env=args.solver_deploy_env,
        key_env=args.solver_key_env,
        endpoint_env=args.endpoint_env,
        effort=args.effort,
        role="solver",
    )
    vision_model = None
    if args.vision_deploy_env:
        from aether_next.providers.azure_model import make_azure_vision_callable
        vision_model = make_azure_vision_callable(
            deployment_env=args.vision_deploy_env,
            key_env=args.vision_key_env or args.solver_key_env,
            endpoint_env=args.endpoint_env,
        )

    architect_model = make_azure_callable(
        deployment_env=args.architect_deploy_env,
        key_env=args.architect_key_env,
        endpoint_env=args.endpoint_env,
        effort=args.effort,
        role="architect",
    )

    records: list[dict[str, Any]] = []

    def persist_results() -> None:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2, default=str)

    for i, task_name in enumerate(task_names, 1):
        task_dir = os.path.join(args.tasks_dir, task_name)
        if not os.path.isdir(task_dir):
            print(f"[{i}/{len(task_names)}] SKIP {task_name}: directory not found", flush=True)
            records.append({
                "task": task_name,
                "image": "",
                "architect_mode": args.architect_mode,
                "reward": 0.0,
                "status": "error",
                "error": "task_dir_not_found",
                "error_detail": f"{task_dir} does not exist",
                "classifier_label": "environment_runner_failure",
                "classifier_confidence": "high",
                "classifier_detail": "task directory missing",
                "step": 0,
                "reconfigurations": 0,
                "model_parse_errors": [],
                "grader_exit": -1,
                "grader_stdout_tail": "",
                "grader_stderr_tail": "",
                "receipt_summary": [],
                "run_provenance": dict(run_provenance),
            })
            continue

        try:
            docker_image = _resolve_task_image(task_dir)
        except (FileNotFoundError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"[{i}/{len(task_names)}] SKIP {task_name}: {exc}", flush=True)
            records.append({
                "task": task_name,
                "image": "",
                "reward": 0.0,
                "status": "error",
                "error": "task_image_resolution_error",
                "error_detail": str(exc),
                "classifier_label": "environment_runner_failure",
                "classifier_confidence": "high",
                "classifier_detail": str(exc),
                "step": 0,
                "reconfigurations": 0,
                "model_parse_errors": [],
                "grader_exit": -1,
                "grader_stdout_tail": "",
                "grader_stderr_tail": "",
                "receipt_summary": [],
            })
            continue

        print(f"[{i}/{len(task_names)}] RUN  {task_name}  image={docker_image}", flush=True)

        record_index = len(records)
        records.append({
            "task": task_name,
            "image": docker_image,
            "architect_mode": args.architect_mode,
            "reward": 0.0,
            "status": "running",
            "kernel_status": "running",
            "error": "attempt_in_progress",
            "error_detail": "task attempt started but no terminal result row has been written yet",
            "classifier_label": "attempt_in_progress",
            "classifier_confidence": "high",
            "classifier_detail": "non-terminal launch receipt; replace with completed/error row when run_tbench_task returns",
            "step": 0,
            "reconfigurations": 0,
            "model_parse_errors": [],
            "grader_exit": -1,
            "grader_stdout_tail": "",
            "grader_stderr_tail": "",
            "receipt_summary": [],
        })
        persist_results()

        def _record_progress(stage: str, detail: str) -> None:
            records[record_index]["running_stage"] = stage
            records[record_index]["running_detail"] = detail
            persist_results()

        try:
            record = run_tbench_task(
                task_dir=task_dir,
                image=docker_image,
                architect_model=architect_model,
                solver_model=solver_model,
                vision_model=vision_model,
                max_steps=args.max_steps,
                run_timeout_s=args.run_timeout_s,
                trace_dir=args.trace_dir,
                architect_mode=args.architect_mode,
                snapshot_dir=args.snapshot_dir,
                snapshot_steps=snap_steps,
                run_provenance={**run_provenance, "task_hash": _task_hash(task_dir)},
                progress_callback=_record_progress,
                network_scope=args.network_scope,
            )
        except Exception as exc:
            record = {
                "task": task_name,
                "image": docker_image,
                "architect_mode": args.architect_mode,
                "reward": 0.0,
                "status": "error",
                "error": type(exc).__name__,
                "error_detail": str(exc)[:4000],
                "classifier_label": "environment_runner_failure",
                "classifier_confidence": "high",
                "classifier_detail": f"run_tbench_task raised: {exc}",
                "step": 0,
                "reconfigurations": 0,
                "model_parse_errors": [],
                "grader_exit": -1,
                "grader_stdout_tail": "",
                "grader_stderr_tail": "",
                "receipt_summary": [],
            }

        record.setdefault("run_provenance", {**run_provenance, "task_hash": _task_hash(task_dir)})
        records[record_index] = record

        reward = record.get("reward", "?")
        label = record.get("classifier_label", "?")
        status = record.get("status", "?")
        print(
            f"[{i}/{len(task_names)}] DONE {task_name}  "
            f"reward={reward}  status={status}  classifier={label}",
            flush=True,
        )

        # Persist results incrementally so a crash preserves completed records.
        persist_results()

    out_path = Path(args.out)
    print(f"\nResults written to {out_path}", flush=True)

    _print_summary(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
