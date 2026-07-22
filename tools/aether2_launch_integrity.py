"""Launch-integrity preflight helpers for Aether-2 runner entrypoints."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_IMPORTS = (
    "harness.aether2",
    "harness.aether2.control.loop",
    "runner.aether2",
    "runner.aether2.bridge_harbor",
    "runner.aether2.executor",
    "runner.aether2.loop",
    "runner.aether2.metrics",
    "runner.model_client",
    "runner.schemas",
)


@dataclass(frozen=True)
class LaunchIntegrityReport:
    ok: bool
    checks: list[dict[str, Any]]
    reason_codes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": self.checks,
            "reason_codes": self.reason_codes,
        }


def run_launch_integrity_preflight(
    *,
    repo_root: Path,
    imports: Iterable[str] = DEFAULT_IMPORTS,
    run_genericity: bool = True,
) -> LaunchIntegrityReport:
    """Verify import and genericity surfaces before model-backed runs."""

    checks: list[dict[str, Any]] = []
    reason_codes: list[str] = []
    for module_name in imports:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {
                    "check": "import",
                    "module": module_name,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            reason_codes.append("launch_import_failed")
        else:
            checks.append(
                {
                    "check": "import",
                    "module": module_name,
                    "ok": True,
                    "resolved_module": getattr(module, "__name__", module_name),
                }
            )

    if run_genericity:
        cmd = [
            sys.executable,
            str(repo_root / "tools" / "aether2_genericity_check.py"),
            "--repo-root",
            str(repo_root),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            checks.append(
                {
                    "check": "genericity",
                    "ok": False,
                    "cmd": cmd,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                    "returncode": None,
                }
            )
            reason_codes.append("genericity_check_failed")
        except OSError as exc:
            checks.append(
                {
                    "check": "genericity",
                    "ok": False,
                    "cmd": cmd,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "stdout": "",
                    "stderr": "",
                    "returncode": None,
                }
            )
            reason_codes.append("genericity_check_failed")
        else:
            checks.append(
                {
                    "check": "genericity",
                    "ok": proc.returncode == 0,
                    "cmd": cmd,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "returncode": proc.returncode,
                }
            )
            if proc.returncode != 0:
                reason_codes.append("genericity_check_failed")

    return LaunchIntegrityReport(
        ok=not reason_codes,
        checks=checks,
        reason_codes=sorted(set(reason_codes)),
    )


def write_launch_integrity_report(path: Path, report: LaunchIntegrityReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
