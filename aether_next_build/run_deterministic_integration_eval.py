#!/usr/bin/env python3
"""Run model-free, Docker-free integration scenarios for Aether-Next vNext."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from aether_next.integration_scenarios import run_all_integration_scenarios


def _report(rows: list[dict]) -> str:
    lines = [
        "# Deterministic Integration Scenario Report",
        "",
        "These scenarios use no model calls, Docker, VM, benchmark task attempt, or official grader.",
        "They exercise the real kernel path with static WorkbenchArchitect configs and scripted solver/verifier hooks.",
        "",
        "| scenario | status | key checks | receipt count | verifier calls |",
        "|---|---|---|---:|---:|",
    ]
    for row in rows:
        passed = ", ".join(f"{k}={v}" for k, v in sorted(row["checks"].items()))
        lines.append(
            f"| {row['scenario_id']} | {row['status']} | {passed} | {len(row['receipts'])} | {len(row['verifier_packets'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default=None,
        help="directory for the auditable scenario bundle (default: timestamped cwd directory)",
    )
    args = parser.parse_args(argv)
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else Path("deterministic_integration_eval_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [result.as_dict() for result in run_all_integration_scenarios()]
    for row in rows:
        case_dir = out_dir / row["scenario_id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "scenario_result.json").write_text(json.dumps(row, indent=2, sort_keys=True))
    (out_dir / "summary.json").write_text(json.dumps({"rows": rows}, indent=2, sort_keys=True))
    (out_dir / "DETERMINISTIC_INTEGRATION_REPORT.md").write_text(_report(rows))
    print(json.dumps({"out_dir": str(out_dir), "rows": [{"scenario_id": row["scenario_id"], "status": row["status"], "checks": row["checks"]} for row in rows]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
