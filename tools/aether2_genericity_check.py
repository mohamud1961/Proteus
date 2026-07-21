#!/usr/bin/env python3
"""Mechanical genericity gate for the Aether-2 harness line."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import Iterable

ACTIVE_AETHER2_ROOTS = (
    Path("aether"),
    Path("harness") / "aether2",
)

DEFAULT_BANNED_TASK_NAMES = {
    "extract-moves-from-video",
    "install-windows-3.11",
    "qemu-startup",
}

DEFAULT_BANNED_BENCHMARK_VOCAB = {
    "terminal-bench",
    "tb2",
    "tb2.0",
}

DEFAULT_BANNED_META_TOOLS = {
    "search_receipts",
    "view_receipt",
    "view_file_cache",
    "search_files",
    "probe_service",
}

TASK_BRANCH_PATTERNS = (
    re.compile(r"\bif\s+task_(?:name|id)\s*(?:==|in\b)", re.IGNORECASE),
    re.compile(r"\btask_(?:name|id)\s*(?:==|in\b)", re.IGNORECASE),
    re.compile(r"\bmatch\s+task_(?:name|id)\b", re.IGNORECASE),
)

SENTENCE_TERMINATOR = re.compile(r"[.!?](?:\s|$)")
DISALLOWED_DESCRIPTION_TERMS = (
    "run_command",
    "start_job",
    "job_status",
    "session_start",
    "session_send",
    "session_read",
    "read_file",
    "write_file",
    "wait",
    "task_done",
)


def _discover_task_names(repo_root: Path) -> set[str]:
    official_tasks = repo_root / "official_tasks"
    discovered: set[str] = set()
    if not official_tasks.exists():
        return discovered
    for child in official_tasks.iterdir():
        if child.is_dir():
            discovered.add(child.name)
    return discovered


def _iter_text_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return (
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and not path.name.startswith(".")
    )


def _scan_text(path: Path, banned_terms: Iterable[str]) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return [f"{path}: unreadable ({exc})"]

    lower = content.lower()
    issues: list[str] = []
    for term in banned_terms:
        if term.lower() in lower:
            issues.append(f"{path}: contains banned term {term!r}")
    for pattern in TASK_BRANCH_PATTERNS:
        if pattern.search(content):
            issues.append(f"{path}: contains task-conditional affordance {pattern.pattern!r}")
    return issues


def _extract_leading_description(path: Path) -> str | None:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return f"unreadable ({exc})"

    try:
        module = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return f"unparseable ({exc.msg})"

    docstring = ast.get_docstring(module, clean=True)
    if docstring:
        return docstring.strip()

    lines = source.splitlines()
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("#!") or stripped.startswith("#"):
            idx += 1
            continue
        break
    if idx < len(lines) and lines[idx].strip().startswith(("#", '"""', "'''")):
        comment_lines: list[str] = []
        while idx < len(lines):
            stripped = lines[idx].strip()
            if not stripped:
                break
            if not stripped.startswith("#"):
                break
            comment_lines.append(stripped.lstrip("#").strip())
            idx += 1
        description = " ".join(part for part in comment_lines if part)
        return description or None
    return None


def _description_is_single_sentence(description: str) -> bool:
    return bool(description) and "\n" not in description and len(SENTENCE_TERMINATOR.findall(description)) == 1


def _description_mentions_disallowed_specifics(description: str) -> str | None:
    lower = description.lower()
    for term in DISALLOWED_DESCRIPTION_TERMS:
        if term.lower() in lower:
            return term
    if re.search(r"\b[\w.-]+\.py\b", description):
        return "file reference"
    if re.search(r"\b[a-z0-9]+(?:-[a-z0-9]+)+\b", description):
        return "task-style identifier"
    return None


def _active_aether2_root(repo_root: Path) -> Path | None:
    for relative_root in ACTIVE_AETHER2_ROOTS:
        candidate = repo_root / relative_root
        if candidate.exists():
            return candidate
    return None


def _is_top_level_mechanism(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return len(relative.parts) == 1


def _is_prompt_facing_file(path: Path) -> bool:
    lowered_parts = {part.lower() for part in path.parts}
    lowered_stem = path.stem.lower()
    return (
        "prompt" in lowered_stem
        or "prompts" in lowered_parts
        or "skills" in lowered_parts
        or "agents" in lowered_parts
    )


def _collect_issues(repo_root: Path) -> list[str]:
    aether2_root = _active_aether2_root(repo_root)
    if aether2_root is None:
        expected = ", ".join(str(path) for path in ACTIVE_AETHER2_ROOTS)
        return [f"{repo_root}: missing active Aether-2 implementation root; expected one of: {expected}"]

    task_names = _discover_task_names(repo_root) or DEFAULT_BANNED_TASK_NAMES
    global_banned_terms = set(task_names)
    prompt_banned_terms = (
        set(task_names)
        | set(DEFAULT_BANNED_META_TOOLS)
        | set(DEFAULT_BANNED_BENCHMARK_VOCAB)
    )

    issues: list[str] = []
    for path in _iter_text_files(aether2_root):
        if path.name.endswith(".py") and path.name != "__init__.py" and _is_top_level_mechanism(path, aether2_root):
            description = _extract_leading_description(path)
            if not description:
                issues.append(f"{path}: missing top-level one-sentence description")
            else:
                if not _description_is_single_sentence(description):
                    issues.append(f"{path}: top-level description must be exactly one sentence")
                disallowed = _description_mentions_disallowed_specifics(description)
                if disallowed:
                    issues.append(f"{path}: top-level description names a specific {disallowed}")
        banned_terms = prompt_banned_terms if _is_prompt_facing_file(path) else global_banned_terms
        issues.extend(_scan_text(path, banned_terms))
    return issues


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
        help="Repository root containing the active Aether-2 implementation root and optional official_tasks.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    issues = _collect_issues(repo_root)
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
