#!/usr/bin/env python3
"""Create a finalised execution plan for one model-role evaluation board.

This command never calls a model. With --allow-model it marks the plan ready
only after validating a passed deterministic certification summary. Dedicated
role runners then consume the plan.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
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


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _validate_deterministic_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        raise RuntimeError("--deterministic-summary is required with --allow-model")
    payload = _load(path)
    if not bool(payload.get("passed", False)):
        raise RuntimeError("deterministic summary is not passed")
    failures = payload.get("required_failures", []) or []
    if failures:
        raise RuntimeError("deterministic summary contains required failures")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--boards-file",
        default=str(_BUILD_ROOT / "evals" / "model_boards.v1.json"),
    )
    parser.add_argument(
        "--board",
        choices=("architect", "solver", "verifier", "perception", "system_smoke", "system_full"),
        required=True,
    )
    parser.add_argument("--samples", type=int, default=None)
    parser.add_argument("--effort", choices=("none", "low", "medium", "high", "xhigh"), default=None)
    parser.add_argument("--deterministic-summary", default=None)
    parser.add_argument("--allow-model", action="store_true")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args(argv)

    boards_path = Path(args.boards_file).resolve()
    payload = _load(boards_path)
    boards = payload.get("boards", {})
    if args.board not in boards:
        parser.error(f"board not found: {args.board}")
    global_rules = dict(payload.get("global_rules", {}) or {})
    board = dict(boards[args.board])
    samples = int(args.samples or global_rules.get("samples_per_case", 3))
    if samples < 1:
        parser.error("--samples must be >= 1")
    effort = str(args.effort or global_rules.get("default_effort", "low"))

    deterministic: dict[str, Any] | None = None
    if args.allow_model:
        deterministic = _validate_deterministic_summary(
            Path(args.deterministic_summary).resolve() if args.deterministic_summary else None
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else _BUILD_ROOT / f"model_role_plan_{args.board}_{stamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    source = executing_source_identity(_BUILD_ROOT)

    plan = {
        "schema": "aether.model_role_eval_plan.v1",
        "board_name": args.board,
        "board_definition": board,
        "global_rules": global_rules,
        "samples": samples,
        "effort": effort,
        "model_execution_requested": bool(args.allow_model),
        "status": "ready_for_dedicated_runner" if args.allow_model else "plan_only",
        "source_identity": source,
        "boards_file": str(boards_path),
        "boards_file_sha256": sha256_file(boards_path),
        "deterministic_gate": None if deterministic is None else {
            "passed": deterministic.get("passed"),
            "required_failures": deterministic.get("required_failures", []),
            "summary_path": str(Path(args.deterministic_summary).resolve()),
            "summary_sha256": sha256_file(Path(args.deterministic_summary).resolve()),
        },
        "taxonomy_delivery_to_models": False,
    }
    plan_path = output_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True, default=str), encoding="utf-8")
    marker = finalize_evidence_directory(
        output_dir,
        required_paths=(plan_path,),
        metadata={
            "status": plan["status"],
            "board": args.board,
            "source_commit": source.get("commit", ""),
            "source_tree": source.get("tree", ""),
        },
    )
    print(json.dumps({
        "status": plan["status"],
        "board": args.board,
        "samples": samples,
        "effort": effort,
        "output_dir": str(output_dir),
        "final_marker": marker,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
