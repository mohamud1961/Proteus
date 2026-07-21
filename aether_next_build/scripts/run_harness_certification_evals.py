#!/usr/bin/env python3
"""Run manifest-selected Aether-Next harness evals with retained evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_BUILD_ROOT = Path(__file__).resolve().parents[1]
if str(_BUILD_ROOT) not in sys.path:
    sys.path.insert(0, str(_BUILD_ROOT))

from evals.framework import run_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=str(_BUILD_ROOT / "evals" / "manifest.v1.json"),
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--tier",
        choices=("component", "system", "certification", "all"),
        default="certification",
        help="component: isolated gates; system: integration/system only; certification: all deterministic blocking gates; all: include plans and diagnostics",
    )
    parser.add_argument("--cases", default="", help="comma-separated case IDs")
    parser.add_argument("--allow-model", action="store_true")
    parser.add_argument("--allow-vm", action="store_true")
    args = parser.parse_args(argv)

    layers_by_tier = {
        "component": {"component", "task_corpus", "meta"},
        "system": {"system"},
        "certification": {"component", "task_corpus", "meta", "system"},
        "all": None,
    }
    case_ids = {item.strip() for item in args.cases.split(",") if item.strip()} or None
    result = run_manifest(
        args.manifest,
        output_dir=args.output_dir,
        layers=layers_by_tier[args.tier],
        case_ids=case_ids,
        allow_model=args.allow_model,
        allow_vm=args.allow_vm,
    )
    print(json.dumps({
        "status": result.status,
        "passed": result.passed,
        "output_dir": result.output_dir,
        "required_failures": list(result.required_failures),
        "case_statuses": {case.case_id: case.status for case in result.cases},
        "final_marker": dict(result.final_marker),
    }, indent=2, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
