#!/usr/bin/env python3
"""Architect-only replay: run the architect + validate + repair for each task.

No solver loop, no grader, no long-lived container.  For each task, seeds a
temporary workspace from the Docker image, builds the envmap, runs a single
architect model call, validates, repairs, compiles, and dumps the resulting
contract.

Usage::

    python3.11 replay_architect.py \\
        --tasks adaptive-rejection-sampler,path-tracing \\
        --out architect_replay.json

Env vars required for Azure models::

    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_GPT54_MINI_DEPLOYMENT
    AZURE_OPENAI_GPT54_MINI_KEY
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Ensure the build dir is importable when run as a script.
_BUILD_DIR = str(Path(__file__).resolve().parent)
if _BUILD_DIR not in sys.path:
    sys.path.insert(0, _BUILD_DIR)

from aether_next.compiler import CapabilityRegistry, ConfigCompiler  # noqa: E402
from aether_next.contract_compile import (  # noqa: E402
    contract_to_eval_index,
    contract_to_objective_graph,
    contract_to_runtime_ir,
)
from reference_legacy.contract_hooks import ContractArchitect  # noqa: E402
from aether_next.envmap_builder import build_envmap_from_task  # noqa: E402
from aether_next.kernel_messages import build_architect_request  # noqa: E402
from aether_next.model_hooks import ModelHooks  # noqa: E402
from aether_next.providers.azure_model import make_azure_callable  # noqa: E402
from aether_next.repair import repair_config  # noqa: E402
from aether_next.runners.docker_helpers import seed_workspace_from_image  # noqa: E402

_OFFICIAL_TASKS_DIR = str(
    Path(__file__).resolve().parent.parent / "official_tasks"
)


def _read_docker_image(task_dir: str) -> str:
    """Read ``docker_image`` from ``task.toml`` in the task directory."""
    toml_path = Path(task_dir) / "task.toml"
    if not toml_path.exists():
        raise FileNotFoundError(f"task.toml not found in {task_dir}")
    text = toml_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("docker_image"):
            _, _, value = stripped.partition("=")
            return value.strip().strip('"').strip("'")
    raise ValueError(f"docker_image not found in {toml_path}")


def _read_instruction(task_dir: str) -> str:
    """Read instruction.md from the task directory, with fallback."""
    instruction_path = Path(task_dir) / "instruction.md"
    if instruction_path.exists():
        return instruction_path.read_text(encoding="utf-8")
    return "Complete the task as described in the workspace."


def _run_one_task(
    task_name: str,
    task_dir: str,
    image: str,
    hooks: ModelHooks,
) -> dict[str, Any]:
    """Run architect-only replay for a single task. Returns a record dict."""
    workspace = tempfile.mkdtemp(prefix=f"replay_{task_name}_")
    try:
        # 1. Seed workspace from image.
        seed_error = seed_workspace_from_image(image, Path(workspace))
        if seed_error is not None:
            return {
                "task": task_name,
                "image": image,
                "error": f"seed_workspace failed: {seed_error}",
            }

        # 2. Read instruction.
        instruction = _read_instruction(task_dir)

        # 3. Build envmap.
        envmap = build_envmap_from_task(workspace, instruction)

        # 4. Build compiler and analyze.
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        objective_graph, eval_index = compiler.analyze_envmap(envmap)

        # 5. Run architect (single model call).
        request = build_architect_request(envmap, compiler)
        ir = hooks.architect(request)

        # 6. Validate, repair, re-validate.
        pre_issues = compiler.validate(
            ir, envmap,
            objective_graph=objective_graph, eval_index=eval_index,
        )
        pre_fatal = [i.code for i in pre_issues if i.fatal]

        repaired_ir, repair_codes = repair_config(
            ir, compiler, envmap,
            objective_graph=objective_graph, eval_index=eval_index,
        )

        post_issues = compiler.validate(
            repaired_ir, envmap,
            objective_graph=objective_graph, eval_index=eval_index,
        )
        remaining_fatal = [i.code for i in post_issues if i.fatal]
        valid = not remaining_fatal

        # 7. Compile (if valid).
        compiled_dict: dict[str, Any] | None = None
        compile_error: str | None = None
        if valid:
            try:
                compiled = compiler.compile(
                    repaired_ir, envmap,
                    objective_graph=objective_graph, eval_index=eval_index,
                )
                compiled_dict = {"task_prompt_len": len(compiled.task_prompt)}
            except ValueError as exc:
                compile_error = str(exc)

        return {
            "task": task_name,
            "image": image,
            "variant": "v1_repair",
            "valid": valid,
            "parse_errors": list(hooks.last_parse_errors),
            "pre_repair_fatal_codes": pre_fatal,
            "repair_codes": list(repair_codes),
            "remaining_fatal_codes": remaining_fatal,
            "runtime_config": asdict(repaired_ir),
            "objective_graph": asdict(objective_graph),
            "eval_checks": [asdict(c) for c in eval_index.checks],
            "compile_error": compile_error,
            "compiled_summary": compiled_dict,
        }
    finally:
        # 8. Clean up temp workspace.
        shutil.rmtree(workspace, ignore_errors=True)


def _run_one_task_v2(
    task_name: str,
    task_dir: str,
    image: str,
    architect_model: Any,
) -> dict[str, Any]:
    """Run V2 contract-architect replay for a single task."""
    workspace = tempfile.mkdtemp(prefix=f"replay_{task_name}_v2_")
    try:
        seed_error = seed_workspace_from_image(image, Path(workspace))
        if seed_error is not None:
            return {
                "task": task_name,
                "image": image,
                "variant": "v2_contract",
                "error": f"seed_workspace failed: {seed_error}",
            }

        instruction = _read_instruction(task_dir)
        envmap = build_envmap_from_task(workspace, instruction)
        compiler = ConfigCompiler(CapabilityRegistry.from_envmap(envmap))
        request = build_architect_request(envmap, compiler)

        # Contract extraction via model.
        ca = ContractArchitect(architect_model)
        contract, parse_errors = ca.extract(
            request, workspace_root=envmap.workspace_root,
        )
        if contract is None:
            return {
                "task": task_name,
                "image": image,
                "variant": "v2_contract",
                "valid": False,
                "parse_errors": parse_errors,
            }

        # Build harness structures from contract.
        objective_graph = contract_to_objective_graph(contract, envmap)
        eval_index = contract_to_eval_index(contract, envmap)
        ir, repair_codes = contract_to_runtime_ir(contract, compiler, envmap)

        # Validate the repaired IR.
        post_issues = compiler.validate(
            ir, envmap,
            objective_graph=objective_graph,
            eval_index=eval_index,
        )
        remaining_fatal = [i.code for i in post_issues if i.fatal]
        valid = not remaining_fatal

        return {
            "task": task_name,
            "image": image,
            "variant": "v2_contract",
            "valid": valid,
            "parse_errors": parse_errors,
            "repair_codes": list(repair_codes),
            "remaining_fatal_codes": remaining_fatal,
            "contract": asdict(contract),
            "runtime_config": asdict(ir),
            "objective_graph": asdict(objective_graph),
            "eval_checks": [asdict(c) for c in eval_index.checks],
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Architect-only replay: architect + validate + repair for each task.",
    )
    ap.add_argument(
        "--tasks",
        required=True,
        help="Comma-separated task names under --tasks-root.",
    )
    ap.add_argument(
        "--tasks-root",
        default=_OFFICIAL_TASKS_DIR,
        help=f"Root directory containing task folders (default: {_OFFICIAL_TASKS_DIR}).",
    )
    ap.add_argument(
        "--image-tag",
        default="20251031",
        help="Docker image tag (default: 20251031). Used only as fallback when "
        "task.toml has no docker_image.",
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
        "--variant",
        default="v1_repair",
        choices=["v1_repair", "v2_contract"],
        help="Replay variant (default: v1_repair).",
    )
    ap.add_argument(
        "--out",
        default="architect_replay.json",
        help="Output JSON file for results (default: architect_replay.json).",
    )
    args = ap.parse_args(argv)

    task_names = [t.strip() for t in args.tasks.split(",") if t.strip()]
    if not task_names:
        print("error: no task names given", file=sys.stderr)
        return 1

    # Build architect model callable.
    architect_model = make_azure_callable(
        deployment_env=args.architect_deploy_env,
        key_env=args.architect_key_env,
        endpoint_env=args.endpoint_env,
        effort=args.effort,
    )
    hooks = ModelHooks(architect_model, architect_model)

    records: list[dict[str, Any]] = []
    out_path = Path(args.out)

    for i, task_name in enumerate(task_names, 1):
        task_dir = os.path.join(args.tasks_root, task_name)
        if not os.path.isdir(task_dir):
            print(f"[{i}/{len(task_names)}] SKIP {task_name}: dir not found", flush=True)
            records.append({"task": task_name, "error": "task_dir_not_found"})
            _write_records(out_path, records)
            continue

        try:
            image = _read_docker_image(task_dir)
        except (FileNotFoundError, ValueError):
            image = f"alexgshaw/{task_name}:{args.image_tag}"

        print(
            f"[{i}/{len(task_names)}] RUN  {task_name}  "
            f"variant={args.variant}  image={image}",
            flush=True,
        )

        try:
            if args.variant == "v2_contract":
                record = _run_one_task_v2(
                    task_name, task_dir, image, architect_model,
                )
            else:
                record = _run_one_task(task_name, task_dir, image, hooks)
        except Exception as exc:
            record = {
                "task": task_name,
                "image": image,
                "error": f"{type(exc).__name__}: {exc}",
            }

        records.append(record)
        _write_records(out_path, records)

        valid = record.get("valid", "?")
        repair = record.get("repair_codes", [])
        error = record.get("error")
        status = "ERROR" if error else ("VALID" if valid else "INVALID")
        print(
            f"[{i}/{len(task_names)}] DONE {task_name}  "
            f"status={status}  repairs={repair}",
            flush=True,
        )

    print(f"\nResults written to {out_path}", flush=True)
    return 0


def _write_records(path: Path, records: list[dict[str, Any]]) -> None:
    """Write records to JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2, default=str)


if __name__ == "__main__":
    raise SystemExit(main())
