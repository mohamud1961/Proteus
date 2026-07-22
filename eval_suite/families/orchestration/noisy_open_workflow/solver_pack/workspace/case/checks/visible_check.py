#!/usr/bin/env python3
import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    report = Path(args.report).read_text(encoding="utf-8")
    required = [
        "## Decision",
        "## Evidence IDs",
        "## Rejected Distractors",
        "## Verification"
    ]
    missing = [section for section in required if section not in report]
    if missing:
        print("VISIBLE_FAIL missing sections:", ",".join(missing))
        return 2
    print("VISIBLE_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
