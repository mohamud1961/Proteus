from __future__ import annotations

import subprocess
from pathlib import Path

from tools.aether2_launch_integrity import run_launch_integrity_preflight


def test_launch_integrity_preflight_records_genericity_timeout(monkeypatch, tmp_path: Path) -> None:
    def fake_run(*args, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=kwargs.get("args", args[0] if args else []), timeout=60)

    monkeypatch.setattr(subprocess, "run", fake_run)

    report = run_launch_integrity_preflight(repo_root=tmp_path, imports=(), run_genericity=True)

    assert report.ok is False
    assert report.reason_codes == ["genericity_check_failed"]
    assert report.checks[0]["check"] == "genericity"
    assert report.checks[0]["ok"] is False
    assert report.checks[0]["error_type"] == "TimeoutExpired"


def test_launch_integrity_preflight_records_genericity_spawn_error(monkeypatch, tmp_path: Path) -> None:
    def fake_run(*args, **kwargs):  # noqa: ANN001, ARG001
        raise OSError("spawn denied")

    monkeypatch.setattr(subprocess, "run", fake_run)

    report = run_launch_integrity_preflight(repo_root=tmp_path, imports=(), run_genericity=True)

    assert report.ok is False
    assert report.reason_codes == ["genericity_check_failed"]
    assert report.checks[0]["error_type"] == "OSError"
    assert report.checks[0]["error"] == "spawn denied"
