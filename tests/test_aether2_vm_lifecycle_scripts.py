from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import spawn_with_retry


REPO_ROOT = Path(__file__).resolve().parents[1]
DEALLOCATE = REPO_ROOT / "scripts" / "deallocate_harnesseng_vm.sh"
AUTOSHUTDOWN = REPO_ROOT / "scripts" / "configure_harnesseng_vm_autoshutdown.sh"
MIRROR = REPO_ROOT / "scripts" / "mirror_harbor_vm_artifacts.sh"


def run_script(script: Path) -> subprocess.CompletedProcess[str]:
    return spawn_with_retry(
        subprocess.run,
        ["bash", str(script), "--dry-run", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )


def test_deallocate_help_mentions_command() -> None:
    result = run_script(DEALLOCATE)
    assert result.returncode == 0
    assert "az vm deallocate" in result.stdout


def test_autoshutdown_help_mentions_command() -> None:
    result = run_script(AUTOSHUTDOWN)
    assert result.returncode == 0
    assert "az vm auto-shutdown" in result.stdout


def test_mirror_help_mentions_run_command() -> None:
    result = run_script(MIRROR)
    assert result.returncode == 0
    assert "az vm run-command invoke" in result.stdout
