"""Manifest-driven, evidence-retaining harness evaluation runner."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

from aether_next.evidence_finalization import (
    executing_source_identity,
    finalize_evidence_directory,
    sha256_file,
)
from .checks import BUILTINS


@dataclass(frozen=True)
class EvalCaseResult:
    case_id: str
    layer: str
    kind: str
    required: bool
    status: str
    passed: bool
    duration_s: float
    covers: tuple[str, ...]
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    summary: str = ""
    findings: tuple[str, ...] = ()
    metrics: Mapping[str, Any] | None = None
    stdout_path: str = ""
    stderr_path: str = ""
    stdout_sha256: str = ""
    stderr_sha256: str = ""


@dataclass(frozen=True)
class EvalRunResult:
    status: str
    passed: bool
    output_dir: str
    manifest_path: str
    manifest_sha256: str
    source_identity: Mapping[str, Any]
    cases: tuple[EvalCaseResult, ...]
    required_failures: tuple[str, ...]
    final_marker: Mapping[str, Any]


def _stable_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _write_text(path: Path, text: str) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path), sha256_file(path)


def _case_selected(case: Mapping[str, Any], layers: set[str] | None,
                   case_ids: set[str] | None) -> bool:
    if case_ids is not None and str(case.get("id", "")) not in case_ids:
        return False
    if layers is not None and str(case.get("layer", "")) not in layers:
        return False
    return True


def _validate_targets(build_root: Path, case: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for target in case.get("targets", []) or []:
        if not (build_root / str(target)).exists():
            missing.append(str(target))
    return missing


def _run_pytest_case(
    build_root: Path,
    case: Mapping[str, Any],
    case_dir: Path,
) -> EvalCaseResult:
    case_id = str(case["id"])
    kind = str(case["kind"])
    missing = _validate_targets(build_root, case)
    if missing:
        return EvalCaseResult(
            case_id=case_id,
            layer=str(case.get("layer", "")),
            kind=kind,
            required=bool(case.get("required", False)),
            status="error",
            passed=False,
            duration_s=0.0,
            covers=tuple(str(item) for item in case.get("covers", [])),
            summary="missing eval targets",
            findings=tuple(f"missing target: {item}" for item in missing),
        )

    # Certification executes from a source checkout.  Pytest's cache is
    # disposable runner state, not source evidence, so keep it out of that
    # checkout and retain the authoritative case outputs under ``case_dir``.
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
    if kind == "pytest_collect":
        command.extend(["--collect-only", "-q"])
    else:
        command.append("-q")
    command.extend(str(item) for item in case.get("targets", []))
    command.extend(str(item) for item in case.get("pytest_args", []) or [])
    timeout_s = max(1, int(case.get("timeout_s", 180)))
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=build_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            env={**os.environ, "PYTHONPATH": str(build_root)},
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
        status = "passed" if exit_code == 0 else "failed"
        findings = () if exit_code == 0 else (f"pytest exit code {exit_code}",)
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = _decode_timeout(exc.stdout)
        stderr = _decode_timeout(exc.stderr) + f"\n[harness-eval] timeout after {timeout_s}s"
        status = "timeout"
        findings = (f"case timed out after {timeout_s}s",)
    duration = time.monotonic() - started
    stdout_path, stdout_hash = _write_text(case_dir / "stdout.txt", stdout)
    stderr_path, stderr_hash = _write_text(case_dir / "stderr.txt", stderr)
    return EvalCaseResult(
        case_id=case_id,
        layer=str(case.get("layer", "")),
        kind=kind,
        required=bool(case.get("required", False)),
        status=status,
        passed=status == "passed",
        duration_s=round(duration, 3),
        covers=tuple(str(item) for item in case.get("covers", [])),
        command=tuple(command),
        exit_code=exit_code,
        summary=("pytest targets passed" if status == "passed" else "pytest targets did not pass"),
        findings=findings,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_sha256=stdout_hash,
        stderr_sha256=stderr_hash,
    )


def _decode_timeout(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _run_builtin_case(
    build_root: Path,
    manifest: Mapping[str, Any],
    case: Mapping[str, Any],
    case_dir: Path,
) -> EvalCaseResult:
    case_id = str(case["id"])
    builtin_name = str(case.get("builtin", ""))
    started = time.monotonic()
    findings: list[str] = []
    metrics: Mapping[str, Any] = {}
    try:
        check = BUILTINS[builtin_name]
    except KeyError:
        payload = {"passed": False, "summary": "unknown builtin", "findings": [builtin_name], "metrics": {}}
    else:
        try:
            payload = check(build_root, manifest)
        except Exception as exc:  # fail closed with retained type/detail
            payload = {
                "passed": False,
                "summary": f"builtin raised {type(exc).__name__}",
                "findings": [str(exc)],
                "metrics": {},
            }
    duration = time.monotonic() - started
    findings.extend(str(item) for item in payload.get("findings", []) or [])
    metrics = dict(payload.get("metrics", {}) or {})
    output = {
        "builtin": builtin_name,
        "passed": bool(payload.get("passed", False)),
        "summary": str(payload.get("summary", "")),
        "findings": findings,
        "metrics": metrics,
    }
    stdout_path, stdout_hash = _write_text(case_dir / "builtin_result.json", _stable_json(output))
    return EvalCaseResult(
        case_id=case_id,
        layer=str(case.get("layer", "")),
        kind="builtin",
        required=bool(case.get("required", False)),
        status="passed" if output["passed"] else "failed",
        passed=output["passed"],
        duration_s=round(duration, 3),
        covers=tuple(str(item) for item in case.get("covers", [])),
        command=("builtin", builtin_name),
        exit_code=0 if output["passed"] else 1,
        summary=output["summary"],
        findings=tuple(findings),
        metrics=metrics,
        stdout_path=stdout_path,
        stdout_sha256=stdout_hash,
    )


def _plan_case(case: Mapping[str, Any], *, allow_model: bool, allow_vm: bool) -> EvalCaseResult:
    gate = str(case.get("gate", ""))
    allowed = (gate == "model" and allow_model) or (gate == "vm" and allow_vm)
    status = "ready_for_external_runner" if allowed else "planned"
    return EvalCaseResult(
        case_id=str(case["id"]),
        layer=str(case.get("layer", "")),
        kind="plan",
        required=bool(case.get("required", False)),
        status=status,
        passed=True,
        duration_s=0.0,
        covers=tuple(str(item) for item in case.get("covers", [])),
        summary=(
            "explicit gate enabled; execute with the dedicated runner"
            if allowed else f"{gate or 'external'} execution not enabled"
        ),
        metrics={
            "gate": gate,
            "board": case.get("board"),
            "samples": case.get("samples"),
        },
    )


def _report_markdown(results: Sequence[EvalCaseResult], *, source: Mapping[str, Any],
                     required_failures: Sequence[str]) -> str:
    lines = [
        "# Aether-Next Harness Evaluation Report",
        "",
        f"Source commit: `{source.get('commit', '')}`",
        f"Source tree: `{source.get('tree', '')}`",
        f"Source clean at start: `{source.get('clean', False)}`",
        "",
        "| Case | Layer | Required | Status | Duration | Scorecard |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in results:
        lines.append(
            f"| {row.case_id} | {row.layer} | {str(row.required).lower()} | {row.status} | "
            f"{row.duration_s:.3f}s | {', '.join(row.covers)} |"
        )
    lines.extend(["", "## Verdict", ""])
    if required_failures:
        lines.append("**NOT READY.** Required failures: " + ", ".join(required_failures) + ".")
    else:
        lines.append("**DETERMINISTIC GATES PASS for the selected manifest scope.** Model and VM plans remain separate gates.")
    lines.append("")
    for row in results:
        if row.findings:
            lines.append(f"### {row.case_id}")
            lines.extend(f"- {item}" for item in row.findings)
            lines.append("")
    return "\n".join(lines)


def run_manifest(
    manifest_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    layers: Iterable[str] | None = None,
    case_ids: Iterable[str] | None = None,
    allow_model: bool = False,
    allow_vm: bool = False,
) -> EvalRunResult:
    manifest_path = Path(manifest_path).resolve()
    build_root = manifest_path.parents[1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected_layers = {str(item) for item in layers} if layers is not None else None
    selected_ids = {str(item) for item in case_ids} if case_ids is not None else None
    if output_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_root = build_root / f"harness_eval_{stamp}"
    else:
        output_root = Path(output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    cases_root = output_root / "cases"
    cases_root.mkdir(parents=True, exist_ok=True)

    source_identity = executing_source_identity(build_root)
    (output_root / "source_identity.json").write_text(
        _stable_json(source_identity), encoding="utf-8"
    )
    manifest_copy = output_root / "manifest.json"
    manifest_copy.write_text(_stable_json(manifest), encoding="utf-8")

    results: list[EvalCaseResult] = []
    for case in manifest.get("cases", []):
        if not _case_selected(case, selected_layers, selected_ids):
            continue
        case_id = str(case.get("id", "")).strip()
        if not case_id:
            continue
        case_dir = cases_root / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        kind = str(case.get("kind", ""))
        if kind in {"pytest", "pytest_collect"}:
            result = _run_pytest_case(build_root, case, case_dir)
        elif kind == "builtin":
            result = _run_builtin_case(build_root, manifest, case, case_dir)
        elif kind == "plan":
            result = _plan_case(case, allow_model=allow_model, allow_vm=allow_vm)
        else:
            result = EvalCaseResult(
                case_id=case_id,
                layer=str(case.get("layer", "")),
                kind=kind,
                required=bool(case.get("required", False)),
                status="error",
                passed=False,
                duration_s=0.0,
                covers=tuple(str(item) for item in case.get("covers", [])),
                summary="unknown case kind",
                findings=(kind,),
            )
        results.append(result)
        (case_dir / "result.json").write_text(_stable_json(asdict(result)), encoding="utf-8")

    required_failures = tuple(
        row.case_id for row in results if row.required and not row.passed
    )
    summary_payload = {
        "schema": "aether.harness_eval_result.v1",
        "status": "passed" if not required_failures else "failed",
        "passed": not required_failures,
        "required_failures": required_failures,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "source_identity": source_identity,
        "cases": [asdict(row) for row in results],
    }
    summary_path = output_root / "summary.json"
    summary_path.write_text(_stable_json(summary_payload), encoding="utf-8")
    report_path = output_root / "REPORT.md"
    report_path.write_text(
        _report_markdown(results, source=source_identity, required_failures=required_failures),
        encoding="utf-8",
    )
    final_marker = finalize_evidence_directory(
        output_root,
        required_paths=(
            output_root / "source_identity.json",
            manifest_copy,
            cases_root,
            summary_path,
            report_path,
        ),
        metadata={
            "schema": "aether.harness_eval_result.v1",
            "status": summary_payload["status"],
            "required_failure_count": len(required_failures),
            "source_commit": source_identity.get("commit", ""),
            "source_tree": source_identity.get("tree", ""),
            "source_clean": source_identity.get("clean", False),
        },
    )
    return EvalRunResult(
        status=summary_payload["status"],
        passed=not required_failures,
        output_dir=str(output_root),
        manifest_path=str(manifest_path),
        manifest_sha256=sha256_file(manifest_path),
        source_identity=source_identity,
        cases=tuple(results),
        required_failures=required_failures,
        final_marker=final_marker,
    )
