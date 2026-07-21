from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conftest import spawn_with_retry


SCRIPT = Path("/Users/mohamud/Downloads/harnesseng/tools/aether2_genericity_check.py")


def _run_checker(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return spawn_with_retry(
        subprocess.run,
        [sys.executable, str(SCRIPT), "--repo-root", str(repo_root)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_genericity_checker_passes_on_clean_tree(tmp_path: Path) -> None:
    repo_root = tmp_path
    aether2_root = repo_root / "aether"
    aether2_root.mkdir(parents=True)
    (aether2_root / "prompts.py").write_text(
        '"""Generic harness prompt."""\nSYSTEM_PROMPT = "stay generic"\nDOCTRINE_LINES = ["line one", "line two"]\n',
        encoding="utf-8",
    )
    (aether2_root / "tools.py").write_text(
        '"""Generic tool wiring for the harness."""\nTOOL_SCHEMAS = []\n',
        encoding="utf-8",
    )

    result = _run_checker(repo_root)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_genericity_checker_fails_when_mechanism_lacks_description(tmp_path: Path) -> None:
    repo_root = tmp_path
    aether2_root = repo_root / "aether"
    aether2_root.mkdir(parents=True)
    (aether2_root / "prompts.py").write_text(
        '"""Generic harness prompt."""\nSYSTEM_PROMPT = "stay generic"\n',
        encoding="utf-8",
    )
    (aether2_root / "router.py").write_text(
        "def route():\n    return True\n",
        encoding="utf-8",
    )

    result = _run_checker(repo_root)

    assert result.returncode == 1
    assert "missing top-level one-sentence description" in result.stderr


def test_genericity_checker_allows_harbor_in_non_prompt_module(tmp_path: Path) -> None:
    repo_root = tmp_path
    aether2_root = repo_root / "aether"
    aether2_root.mkdir(parents=True)
    (aether2_root / "bridge_harbor.py").write_text(
        '"""Generic task mounting adapter."""\nMODULE_NAME = "bridge_harbor"\n',
        encoding="utf-8",
    )

    result = _run_checker(repo_root)

    assert result.returncode == 0, result.stderr


def test_genericity_checker_fails_without_active_aether2_root(tmp_path: Path) -> None:
    result = _run_checker(tmp_path)

    assert result.returncode == 1
    assert "missing active Aether-2 implementation root" in result.stderr
