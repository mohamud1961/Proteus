#!/usr/bin/env python3
"""Build a verifier/grader alignment board from Aether result rows."""
from __future__ import annotations

import argparse
from pathlib import Path

from aether_next.alignment_board import build_alignment_board, load_result_rows, write_alignment_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", help="JSON result row files")
    parser.add_argument("--out", required=True, help="Output JSON board path")
    parser.add_argument("--md", help="Optional Markdown report path")
    args = parser.parse_args()

    rows, sources = load_result_rows(args.results)
    board = build_alignment_board(rows, source_files=sources)
    write_alignment_report(board, args.out, args.md)
    print(f"wrote {len(board.rows)} rows to {Path(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
