"""Grader for the whole-harness registry smoke pack.

Verifies that the harness registry report produced by the stub agent matches
the expected shape: correct harness_id, valid registry, and expected pack count.
This is an offline deterministic grader — no network or docker required.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def grade_workspace(*, workspace_root: Path, mode: str = "visible") -> dict[str, Any]:
    """Grade the harness registry smoke workspace.

    Checks that harness_registry_report.json exists and contains a valid registry.
    """
    candidate = _read_json(workspace_root / "harness_registry_report.json")
    reason_codes: list[str] = []

    if candidate is None:
        reason_codes.append("missing_harness_registry_report")
    else:
        if candidate.get("harness_id") != "runtime_control_harness_v1":
            reason_codes.append("wrong_harness_id")
        if not candidate.get("registry_valid", False):
            reason_codes.append("registry_invalid")
        if candidate.get("family_pack_count", 0) < 1:
            reason_codes.append("no_family_packs_registered")
        if not candidate.get("all_family_packs_present", False):
            reason_codes.append("family_packs_missing")

    verdict = "pass" if not reason_codes else "fail"
    score = 1.0 if verdict == "pass" else 0.0
    return {
        "mode": mode,
        "workspace_root": str(workspace_root),
        "verdict": verdict,
        "score": score,
        "reason_codes": sorted(set(reason_codes)),
        "candidate": candidate,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grade the harness registry smoke workspace."
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--mode", choices=("visible", "hidden"), default="visible")
    args = parser.parse_args(argv)

    result = grade_workspace(
        workspace_root=Path(args.workspace),
        mode=args.mode,
    )
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
