#!/usr/bin/env python3
"""Architect-only vNext experiment.

Runs old ContractArchitect and new Runtime Workbench Architect on selected
official task prompts/workspace maps. Official grader/test files are included
only in the saved review context and deterministic rubric, not in the architect
model input.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys
from typing import Any

_BUILD_DIR = str(Path(__file__).resolve().parent)
if _BUILD_DIR not in sys.path:
    sys.path.insert(0, _BUILD_DIR)

from aether_next.compiler import CapabilityRegistry, ConfigCompiler  # noqa: E402
from aether_next.architect_quality import score_architect_config  # noqa: E402
from reference_legacy.contract_hooks import ContractArchitect  # noqa: E402
from aether_next.envmap_builder import build_envmap_from_task  # noqa: E402
from aether_next.kernel_messages import build_architect_request  # noqa: E402
from aether_next.providers.azure_model import make_azure_callable  # noqa: E402
from aether_next.workbench_compile import realization_preview  # noqa: E402
from aether_next.workbench_hooks import WorkbenchArchitect  # noqa: E402


def _default_tasks_root() -> str:
    build_dir = Path(_BUILD_DIR)
    candidates = [
        build_dir.parent / "official_tasks",
        build_dir.parents[1] / "official_tasks",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    return str(candidates[0])


DEFAULT_TASKS = (
    "filter-js-from-html",
    "sparql-university",
    "openssl-selfsigned-cert",
    "video-processing",
    "install-windows-3.11",
    "fix-git",
    "gpt2-codegolf",
    "extract-moves-from-video",
    "git-multibranch",
    "configure-git-webserver",
    "qemu-alpine-ssh",
    "financial-document-processor",
    "vulnerable-secret",
    "query-optimize",
    "hf-model-inference",
)


def _read_text(path: Path, limit: int = 5000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def _task_workspace_dir(task_dir: Path) -> Path:
    env_dir = task_dir / "environment"
    return env_dir if env_dir.is_dir() else task_dir


def _review_context(task_dir: Path) -> dict[str, str]:
    return {
        "instruction_excerpt": _read_text(task_dir / "instruction.md", 6000),
        "test_sh_excerpt": _read_text(task_dir / "tests" / "test.sh", 5000),
        "test_outputs_py_excerpt": _read_text(task_dir / "tests" / "test_outputs.py", 8000),
        "task_toml_excerpt": _read_text(task_dir / "task.toml", 3000),
    }


def _score_contract(contract: Any | None) -> dict[str, Any]:
    if contract is None:
        return {"score": 0, "max_score": 8, "missing": ["parseable TaskContract"]}
    missing: list[str] = []
    if not contract.deliverables:
        missing.append("deliverables")
    if not contract.success_definition:
        missing.append("success_definition")
    if not contract.stop_conditions:
        missing.append("stop_conditions")
    if not contract.capabilities:
        missing.append("capabilities")
    if not contract.failure_hypotheses:
        missing.append("failure_hypotheses")
    if not contract.required_checks and not contract.output_schemas and not contract.thresholds:
        missing.append("verification_or_schema_signal")
    if not contract.tooling_notes:
        missing.append("tooling_notes")
    # Old contract path has no architect-authored solver prompt by design.
    missing.append("architect_designed_solver_prompt")
    return {"score": max(0, 8 - len(missing)), "max_score": 8, "missing": missing}


def _run_task(task_name: str, tasks_root: Path, model: Any, max_output_tokens: int, *, include_old_contract: bool = False) -> dict[str, Any]:
    task_dir = tasks_root / task_name
    instruction = _read_text(task_dir / "instruction.md", 12000)
    workspace = _task_workspace_dir(task_dir)
    envmap = build_envmap_from_task(str(workspace), instruction, workspace_root="/app")
    compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
    request = build_architect_request(envmap, compiler)

    old_contract = None
    old_errors: list[str] = []
    if include_old_contract:
        old_arch = ContractArchitect(model)
        old_contract, old_errors = old_arch.extract(request, workspace_root=envmap.workspace_root)

    new_arch = WorkbenchArchitect(model, max_output_tokens=max_output_tokens)
    new_config, new_errors = new_arch.configure(request)
    preview = realization_preview(new_config, envmap) if new_config is not None else None

    return {
        "task": task_name,
        "architect_input_summary": {
            "task_prompt_chars": len(request["task_prompt"]),
            "file_tree_available": bool(request["envmap"]["file_tree"]),
            "file_map_summary": request["envmap"]["file_map_summary"],
            "runtime_manual_keys": sorted(request["runtime_manual"]),
            "capability_count": len(request["capability_index"]),
        },
        "review_context": _review_context(task_dir),
        "old_contract": None if old_contract is None else asdict(old_contract),
        "old_errors": old_errors,
        "old_score": _score_contract(old_contract),
        "workbench_config": None if new_config is None else new_config.as_dict(),
        "workbench_raw_output": new_arch.last_raw_output,
        "workbench_repaired_output": new_arch.last_repaired_output,
        "workbench_errors": new_errors,
        "workbench_warning_codes": list(new_arch.last_warning_codes),
        "workbench_warnings": list(new_arch.last_warnings),
        "workbench_rejected_config_items": list(new_arch.last_rejected_config_items),
        "workbench_score": score_architect_config(new_config, preview),
        "workbench_realization_preview": preview,
    }


def _markdown_report(records: list[dict[str, Any]]) -> str:
    lines = [
        "# Architect-Only Eval Report",
        "",
        "Scope: old ContractArchitect vs Runtime Workbench Architect on official task prompts/workspace maps.",
        "Official test/grader excerpts are saved as review context only; they are not sent to the architect model.",
        "",
        "| task | old score | overall | solver | verifier | config | key missing |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for rec in records:
        old = rec["old_score"]
        new = rec["workbench_score"]
        solver = new["solver_prompt"]
        verifier = new["verifier_prompt"]
        config = new["config_contract"]
        missing = (
            solver["missing"][:3]
            + verifier["missing"][:3]
            + config["missing"][:3]
        )
        lines.append(
            f"| {rec['task']} | {old['score']}/{old['max_score']} | "
            f"{new['overall_score']}/{new['max_score']} | "
            f"{solver['score']}/{solver['max_score']} | "
            f"{verifier['score']}/{verifier['max_score']} | "
            f"{config['score']}/{config['max_score']} | "
            f"{', '.join(missing) or 'none'} |"
        )
    lines.extend(["", "## Notes", ""])
    for rec in records:
        lines.append(f"### {rec['task']}")
        lines.append("")
        lines.append(f"- Old missing: {', '.join(rec['old_score']['missing']) or 'none'}")
        score = rec["workbench_score"]
        lines.append(f"- Overall: {score['overall_score']}/{score['max_score']}")
        lines.append(f"- Solver prompt: {score['solver_prompt']['score']}/{score['solver_prompt']['max_score']} missing={', '.join(score['solver_prompt']['missing']) or 'none'}")
        lines.append(f"- Verifier prompt: {score['verifier_prompt']['score']}/{score['verifier_prompt']['max_score']} missing={', '.join(score['verifier_prompt']['missing']) or 'none'}")
        lines.append(f"- Config contract: {score['config_contract']['score']}/{score['config_contract']['max_score']} missing={', '.join(score['config_contract']['missing']) or 'none'}")
        if rec["workbench_config"]:
            prompt = rec["workbench_config"]["solver_system_prompt"]
            verifier_prompt = rec["workbench_config"].get("verifier_system_prompt", {})
            snapshot = score.get("config_snapshot") or {}
            lines.append(f"- Solver prompt words: {snapshot.get('solver_prompt_words', 0)}")
            lines.append(f"- Verifier prompt words: {snapshot.get('verifier_prompt_words', 0)}")
            lines.append(f"- Solver role: {prompt.get('role', '')}")
            lines.append(f"- Verifier role: {verifier_prompt.get('role', '')}")
            lines.append(f"- Workflow: {' / '.join(prompt.get('workflow', []))}")
            lines.append(f"- Self-verification: {' / '.join(prompt.get('self_verification', []))}")
            lines.append(f"- Evidence requirements: {' / '.join(rec['workbench_config'].get('evidence_requirements', []))}")
            lines.append(f"- False-positive risks: {' / '.join(rec['workbench_config'].get('false_positive_risks', []))}")
            lines.append(f"- Minimum completion evidence: {' / '.join(rec['workbench_config'].get('minimum_completion_evidence', []))}")
        if rec["old_errors"] or rec["workbench_errors"]:
            lines.append(f"- Errors: old={rec['old_errors']} workbench={rec['workbench_errors']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default=",".join(DEFAULT_TASKS))
    parser.add_argument("--tasks-root", default=_default_tasks_root())
    parser.add_argument("--out-dir", default="architect_only_eval")
    parser.add_argument("--effort", default="high", choices=["none", "low", "medium", "high", "xhigh"])
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=24000,
        help="Max output tokens for the isolated WorkbenchArchitect runs.",
    )
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--include-old-contract", action="store_true")
    args = parser.parse_args(argv)

    model = make_azure_callable(
        deployment_env="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT54_MINI_KEY",
        endpoint_env="AZURE_OPENAI_ENDPOINT",
        effort=args.effort,
        poll_interval_s=2.0,
        poll_timeout_s=420.0,
    )

    tasks_root = Path(args.tasks_root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    task_names = [item.strip() for item in args.tasks.split(",") if item.strip()]
    records_by_task: dict[str, dict[str, Any]] = {}
    workers = max(1, int(args.concurrency))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_task,
                task,
                tasks_root,
                model,
                args.max_output_tokens,
                include_old_contract=bool(args.include_old_contract),
            ): task
            for task in task_names
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                records_by_task[task] = future.result()
                print(json.dumps({"task": task, "status": "architect_only_done"}, sort_keys=True), flush=True)
            except Exception as exc:
                records_by_task[task] = {
                    "task": task,
                    "error": str(exc),
                    "old_score": {"score": 0, "max_score": 8, "missing": ["exception"]},
                    "workbench_score": {
                        "overall_score": 0,
                        "max_score": 10,
                        "solver_prompt": {"score": 0, "max_score": 10, "missing": ["exception"], "warnings": []},
                        "verifier_prompt": {"score": 0, "max_score": 10, "missing": ["exception"], "warnings": []},
                        "config_contract": {"score": 0, "max_score": 10, "missing": ["exception"], "warnings": []},
                    },
                    "workbench_config": None,
                    "old_errors": [],
                    "workbench_errors": [str(exc)],
                }
                print(json.dumps({"task": task, "status": "architect_only_error", "error": str(exc)}, sort_keys=True), flush=True)
    records = [records_by_task[task] for task in task_names]

    (out_dir / "architect_only_eval.json").write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    (out_dir / "ARCHITECT_EVAL_REPORT.md").write_text(_markdown_report(records), encoding="utf-8")
    print(json.dumps({
        "records": len(records),
        "report": str(out_dir / "ARCHITECT_EVAL_REPORT.md"),
        "json": str(out_dir / "architect_only_eval.json"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
