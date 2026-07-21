"""Eval substrate fixture/workspace setup and verifier+grader contract.

Synthetic, non-certifying helpers for contract development and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import subprocess
from typing import Any

from harness.aether2.runtime.route_schemas import SchemaValidationError

CANONICAL_CWD = "/app"


@dataclass(frozen=True)
class EvalEnvironmentRefs:
    environment_manifest_ref: str
    fixture_root_host: str
    fixture_root_container: str = CANONICAL_CWD
    python_command_contract: str = "python3"


def setup_fixture_workspace(*, output_root: str, fixture_name: str, certified: bool) -> dict[str, Any]:
    """Prepare fixture workspace with canonical /app semantics.

    Local filesystem staging is debug-only. Certified workspaces must be emitted
    by the certified sandbox runner so a host fixture cannot masquerade as
    benchmark-native container evidence.
    """
    if certified:
        raise SchemaValidationError(
            "certified fixture workspaces must be produced by certified_sandbox, not local staging"
        )
    root = Path(output_root).resolve()
    host_fixture = root / "fixture_workspace" / fixture_name
    host_fixture.mkdir(parents=True, exist_ok=True)

    debug_local_staging = True
    (host_fixture / "README.debug_local_fixture.txt").write_text(
        "debug-only local fixture staging; non-certifying run\n", encoding="utf-8"
    )

    manifest_path = root / "artifacts" / "environment_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if not manifest_path.exists():
        manifest_path.write_text(
            json.dumps(
                {
                    "certification_mode": "debug",
                    "container_workspace_path": CANONICAL_CWD,
                    "initial_cwd": CANONICAL_CWD,
                    "host_fixture_root": str(host_fixture),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    refs = EvalEnvironmentRefs(
        environment_manifest_ref=str(manifest_path),
        fixture_root_host=str(host_fixture),
    )
    return {
        "canonical_cwd": CANONICAL_CWD,
        "debug_local_fixture_staging": debug_local_staging,
        "environment_refs": refs.__dict__,
    }


def _run_verifier(command: str, *, cwd: str, timeout_seconds: int) -> dict[str, Any]:
    try:
        cp = subprocess.run(
            ["sh", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "command": command,
            "cwd": cwd,
            "stdout": cp.stdout,
            "stderr": cp.stderr,
            "exit_code": cp.returncode,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "cwd": cwd,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            "exit_code": None,
            "timeout": True,
        }


def execute_verifier_with_records(
    *,
    command: str,
    cwd: str,
    timeout_seconds: int = 30,
    hidden_checks_ref: str = "checks://synthetic-non-certifying",
) -> dict[str, Any]:
    """Run verifier and emit visible+hidden records.

    Hidden record preserves references to internal checks without exposing check details.
    """
    execution = _run_verifier(command, cwd=cwd, timeout_seconds=timeout_seconds)
    visible = {
        "command": execution["command"],
        "cwd": execution["cwd"],
        "stdout": execution["stdout"],
        "stderr": execution["stderr"],
        "exit_code": execution["exit_code"],
        "timeout": execution["timeout"],
    }
    hidden = {
        "shape_version": "v1",
        "checks_ref": hidden_checks_ref,
        "execution_record_ref": "verifier_execution.visible_record",
        "check_ids": ["hidden_check_ref_only"],
        "checks_materialized": False,
    }
    return {"visible_record": visible, "hidden_record": hidden}


def deterministic_grade(*, verifier_record: dict[str, Any], verifier_truth_passed: bool) -> dict[str, Any]:
    """Grade deterministically and separate execution failure from task-truth failure."""
    timeout = bool(verifier_record.get("timeout"))
    exit_code = verifier_record.get("exit_code")
    exec_failed = timeout or exit_code is None or (exit_code != 0 and verifier_truth_passed)

    if exec_failed:
        outcome = "verifier_execution_failure"
        passed = False
    elif verifier_truth_passed:
        outcome = "pass"
        passed = True
    else:
        outcome = "task_truth_failure"
        passed = False

    return {
        "passed": passed,
        "outcome": outcome,
        "verifier_execution_failure": exec_failed,
        "task_truth_failure": (not exec_failed) and (not verifier_truth_passed),
        "verifier_exit_code": exit_code,
        "verifier_timeout": timeout,
    }
