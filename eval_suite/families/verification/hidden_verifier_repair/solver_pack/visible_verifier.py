#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-dir", default="candidate")
    args = parser.parse_args()

    candidate_dir = Path(args.candidate_dir).resolve()
    report_path = candidate_dir / "fix_report.json"

    reasons = []
    if not report_path.exists():
        reasons.append("missing_fix_report")
    else:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not bool(report.get("hidden_case_reproduced", False)):
            reasons.append("hidden_case_not_reproduced")
        if not bool(report.get("fix_applied", False)):
            reasons.append("fix_not_applied")
        if not bool(report.get("rerun_visible_pass", False)):
            reasons.append("visible_rerun_not_passed")

    result = {"passed": not reasons, "reason_codes": sorted(set(reasons))}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
