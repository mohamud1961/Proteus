#!/usr/bin/env python3
"""Deterministic EnvMap audit across official tasks.

This script does not run models, Docker, or graders. It builds EnvMaps from the
official task corpus and emits a board that helps answer:

- what the harness can already see deterministically;
- where workspace visibility is thin or truncated;
- which tasks carry strong tooling/language pressure in the instruction text;
- where later tooling failures might reflect missing harness surfacing rather
  than model incapability.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

from aether_next.envmap_builder import build_envmap_from_task

_BUILD_DIR = Path(__file__).resolve().parent
_TASKS_ROOT = _BUILD_DIR.parent / "official_tasks"
_TASKS_INDEX = _TASKS_ROOT / "tasks_index.json"

_TOOL_VOCAB = (
    "python", "python3", "r", "rscript", "node", "npm", "git", "ssh", "sshd",
    "nginx", "openssl", "ffmpeg", "ffprobe", "qemu", "docker", "make", "gcc",
    "g++", "curl", "wget", "sqlite3", "java", "tesseract", "expect",
)

_LANGUAGE_VOCAB = (
    "python", "javascript", "typescript", "r", "rust", "c", "c++", "java",
    "bash", "shell", "sql", "sparql", "ocaml", "cobol",
)


def _load_index(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"expected list in {path}")
    return [item for item in data if isinstance(item, dict) and str(item.get("slug", "")).strip()]


def _task_workspace_dir(task_dir: Path) -> Path:
    env_dir = task_dir / "environment"
    return env_dir if env_dir.is_dir() else task_dir


def _instruction_text(task_dir: Path, index_item: dict[str, Any]) -> str:
    from_index = str(index_item.get("instruction", "")).strip()
    if from_index:
        return from_index
    instruction_md = task_dir / "instruction.md"
    if instruction_md.is_file():
        return instruction_md.read_text(encoding="utf-8", errors="replace")
    return ""


def _extract_vocab_hits(text: str, vocab: tuple[str, ...]) -> list[str]:
    lowered = text.lower()
    hits = []
    for item in vocab:
        pattern = r"(?<![a-z0-9_])" + re.escape(item.lower()) + r"(?![a-z0-9_])"
        if re.search(pattern, lowered):
            hits.append(item)
    return hits


def _extract_output_paths(text: str, *, limit: int = 12) -> list[str]:
    paths = [path.rstrip(".,:;)") for path in re.findall(r"/app/[A-Za-z0-9_./-]+", text)]
    deduped = list(dict.fromkeys(paths))
    return deduped[:limit]


def _risk_flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if row["file_tree_truncated"]:
        flags.append("file_tree_truncated")
    if row["visible_file_count"] < 3:
        flags.append("sparse_visible_workspace")
    if row["instruction_tool_hints"] and not row["has_environment_dir"]:
        flags.append("tooling_pressure_without_environment_dir")
    if row["prompt_declared_output_missing_paths"]:
        flags.append("prompt_declared_output_not_visible")
    if row["instruction_referenced_missing_paths"] and not row["likely_inputs"]:
        flags.append("deliverable_pressure_with_few_input_hints")
    if row["likely_tests_or_checkers_count"] >= 8:
        flags.append("heavy_visible_test_surface")
    return flags


def _audit_row(task_root: Path, item: dict[str, Any]) -> dict[str, Any]:
    slug = str(item["slug"])
    task_dir = task_root / slug
    workspace_dir = _task_workspace_dir(task_dir)
    instruction = _instruction_text(task_dir, item)
    envmap = build_envmap_from_task(str(workspace_dir), instruction, workspace_root="/app")
    summary = envmap.file_map_summary
    row = {
        "task": slug,
        "category": str(item.get("category", "")),
        "difficulty": str(item.get("difficulty", "")),
        "workspace_dir": str(workspace_dir.relative_to(task_root.parent)),
        "instruction_chars": len(instruction),
        "visible_file_count": int(summary.get("visible_file_count", 0)),
        "visible_dir_count": int(summary.get("visible_dir_count", 0)),
        "file_tree_truncated": "... truncated after" in (envmap.file_tree or ""),
        "has_environment_dir": (task_dir / "environment").is_dir(),
        "has_solution_dir": (task_dir / "solution").is_dir(),
        "has_tests_dir": (task_dir / "tests").is_dir(),
        "top_level": list(summary.get("top_level", []))[:20],
        "likely_inputs": list(summary.get("likely_inputs", []))[:12],
        "likely_existing_solution_files": list(summary.get("likely_existing_solution_files", []))[:12],
        "likely_tests_or_checkers": list(summary.get("likely_tests_or_checkers", []))[:12],
        "likely_tests_or_checkers_count": len(list(summary.get("likely_tests_or_checkers", []))),
        "instruction_tool_hints": _extract_vocab_hits(instruction, _TOOL_VOCAB),
        "instruction_language_hints": _extract_vocab_hits(instruction, _LANGUAGE_VOCAB),
        "instruction_output_paths": _extract_output_paths(instruction),
        "instruction_referenced_paths": list(summary.get("instruction_referenced_paths", []))[:20],
        "instruction_referenced_visible_paths": list(summary.get("instruction_referenced_visible_paths", []))[:20],
        "instruction_referenced_alias_matches": list(summary.get("instruction_referenced_alias_matches", []))[:20],
        "instruction_referenced_missing_paths": list(summary.get("instruction_referenced_missing_paths", []))[:20],
        "prompt_declared_output_paths": list(summary.get("prompt_declared_output_paths", []))[:20],
        "prompt_declared_output_visible_paths": list(summary.get("prompt_declared_output_visible_paths", []))[:20],
        "prompt_declared_output_alias_matches": list(summary.get("prompt_declared_output_alias_matches", []))[:20],
        "prompt_declared_output_missing_paths": list(summary.get("prompt_declared_output_missing_paths", []))[:20],
    }
    row["risk_flags"] = _risk_flags(row)
    return row


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tool_counter = Counter(tool for row in rows for tool in row["instruction_tool_hints"])
    language_counter = Counter(lang for row in rows for lang in row["instruction_language_hints"])
    risk_counter = Counter(flag for row in rows for flag in row["risk_flags"])
    return {
        "task_count": len(rows),
        "tasks_with_truncated_file_tree": sum(1 for row in rows if row["file_tree_truncated"]),
        "tasks_with_environment_dir": sum(1 for row in rows if row["has_environment_dir"]),
        "tasks_with_solution_dir": sum(1 for row in rows if row["has_solution_dir"]),
        "tasks_with_tests_dir": sum(1 for row in rows if row["has_tests_dir"]),
        "tasks_with_visible_test_checkers": sum(1 for row in rows if row["likely_tests_or_checkers_count"] > 0),
        "tasks_with_tooling_hints": sum(1 for row in rows if row["instruction_tool_hints"]),
        "tasks_with_output_paths": sum(1 for row in rows if row["instruction_output_paths"]),
        "top_instruction_tool_hints": tool_counter.most_common(20),
        "top_instruction_language_hints": language_counter.most_common(20),
        "risk_flag_counts": risk_counter.most_common(),
    }


def _report(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# EnvMap Audit Report",
        "",
        "Deterministic audit only: no models, no Docker, no grader, no verifier.",
        "",
        f"- Indexed tasks audited: {summary['task_count']}",
        f"- Truncated file trees: {summary['tasks_with_truncated_file_tree']}",
        f"- Tasks with environment directories: {summary['tasks_with_environment_dir']}",
        f"- Tasks with visible tests/checkers: {summary['tasks_with_visible_test_checkers']}",
        f"- Tasks with tooling hints in instructions: {summary['tasks_with_tooling_hints']}",
        "",
        "## Top Tool Hints",
        "",
    ]
    for tool, count in summary["top_instruction_tool_hints"]:
        lines.append(f"- `{tool}`: {count}")
    lines.extend(["", "## Risk Flags", ""])
    for flag, count in summary["risk_flag_counts"]:
        lines.append(f"- `{flag}`: {count}")
    lines.extend([
        "",
        "## Task Board",
        "",
        "| task | files | tests | env dir | tool hints | output paths | risk flags |",
        "|---|---:|---:|---|---|---|---|",
    ])
    for row in rows:
        lines.append(
            f"| {row['task']} | {row['visible_file_count']} | {row['likely_tests_or_checkers_count']} | "
            f"{'yes' if row['has_environment_dir'] else 'no'} | "
            f"{', '.join(row['instruction_tool_hints']) or 'none'} | "
            f"{', '.join(row['instruction_output_paths']) or 'none'} | "
            f"{', '.join(row['risk_flags']) or 'none'} |"
        )
    lines.append("")
    return "\n".join(lines)


def run(tasks_root: Path, out_dir: Path) -> dict[str, Any]:
    index_items = _load_index(tasks_root / "tasks_index.json")
    rows = [_audit_row(tasks_root, item) for item in index_items]
    rows.sort(key=lambda row: row["task"])
    summary = _summary(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "envmap_audit_rows.json").write_text(json.dumps(rows, indent=2, sort_keys=True))
    (out_dir / "envmap_audit_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    (out_dir / "ENVMAP_AUDIT_REPORT.md").write_text(_report(rows, summary), encoding="utf-8")
    return {
        "task_count": summary["task_count"],
        "rows_path": str(out_dir / "envmap_audit_rows.json"),
        "summary_path": str(out_dir / "envmap_audit_summary.json"),
        "report_path": str(out_dir / "ENVMAP_AUDIT_REPORT.md"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks-root", default=str(_TASKS_ROOT))
    parser.add_argument("--out-dir", default=str(_BUILD_DIR / "envmap_audit"))
    args = parser.parse_args()
    result = run(Path(args.tasks_root), Path(args.out_dir))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
