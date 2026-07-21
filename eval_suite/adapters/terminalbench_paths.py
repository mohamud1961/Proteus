"""Shared path resolution for mirrored TerminalBench task assets."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TERMINALBENCH_TASK_ID = "regex-log"
TERMINALBENCH_TASK_ROOT_CANDIDATES = (
    Path("/private/tmp/terminalbench/official_tasks"),
    REPO_ROOT / "official_tasks",
)
TERMINALBENCH_SEARCH_ROOTS = (
    REPO_ROOT / "tracking",
    REPO_ROOT / "research",
)
REQUIRED_TASK_FILES = (
    Path("task.toml"),
    Path("instruction.md"),
    Path("tests/test_outputs.py"),
)


def resolve_terminalbench_tasks_root(task_id: str = TERMINALBENCH_TASK_ID) -> Path:
    for candidate in TERMINALBENCH_TASK_ROOT_CANDIDATES:
        task_root = candidate / task_id
        if _task_root_is_complete(task_root):
            return candidate
    for search_root in TERMINALBENCH_SEARCH_ROOTS:
        if not search_root.exists():
            continue
        for task_root in sorted(search_root.rglob(task_id), key=str):
            if task_root.is_dir() and _task_root_is_complete(task_root):
                return task_root.parent
    raise FileNotFoundError(
        "TerminalBench tasks root not found; checked "
        + ", ".join(str(path) for path in TERMINALBENCH_TASK_ROOT_CANDIDATES)
    )


def resolve_terminalbench_task_root(task_id: str = TERMINALBENCH_TASK_ID) -> Path:
    return resolve_terminalbench_tasks_root(task_id) / task_id


def _task_root_is_complete(task_root: Path) -> bool:
    return all((task_root / rel).exists() for rel in REQUIRED_TASK_FILES)
