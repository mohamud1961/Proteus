"""Preflight certified proof routes in Solver and Verifier environments."""
from __future__ import annotations

from dataclasses import dataclass
import shlex
from typing import Any, Mapping, Sequence

from .runtime_ir import CompiledRuntime
from .verifier_overlay import VerifierOverlay


@dataclass(frozen=True)
class RoutePreflightIssue:
    code: str
    clause_id: str
    route: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "clause_id": self.clause_id,
            "route": self.route,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RoutePreflightRow:
    clause_id: str
    route: str
    route_kind: str
    solver_available: bool
    verifier_available: bool
    solver_detail: str
    verifier_detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "route": self.route,
            "route_kind": self.route_kind,
            "solver_available": self.solver_available,
            "verifier_available": self.verifier_available,
            "solver_detail": self.solver_detail,
            "verifier_detail": self.verifier_detail,
        }


def _route_parts(route: str) -> tuple[str, str]:
    kind, separator, target = str(route or "").strip().partition(":")
    return kind.strip(), target.strip() if separator else ""


def _command_executable(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    if not parts:
        return ""
    while parts and "=" in parts[0] and not parts[0].startswith(("/", "./")):
        name, _, _value = parts[0].partition("=")
        if not name.replace("_", "").isalnum():
            break
        parts.pop(0)
    return parts[0] if parts else ""


def _command_available(executor: Any, executable: str, *, cwd: str | None = None) -> tuple[bool, str]:
    if not executable:
        return False, "route command has no executable"
    quoted = shlex.quote(executable)
    result = executor.run_command(f"command -v {quoted}", cwd=cwd, timeout_s=30)
    detail = (result.stdout or result.stderr).strip()[:500]
    return bool(result.success), detail or f"command -v {executable} exit={result.exit_code}"


def _overlay_command_available(overlay: VerifierOverlay, executable: str) -> tuple[bool, str]:
    if not executable:
        return False, "route command has no executable"
    result = overlay.run_command(f"command -v {shlex.quote(executable)}", timeout_s=30)
    if result.get("error"):
        return False, str(result["error"])
    detail = str(result.get("stdout") or result.get("stderr") or "").strip()[:500]
    return bool(result.get("success")), detail or f"command -v {executable} exit={result.get('exit_code')}"


def preflight_proof_routes(
    compiled: CompiledRuntime,
    executor: Any,
    envmap: Any,
) -> tuple[tuple[RoutePreflightRow, ...], tuple[RoutePreflightIssue, ...]]:
    """Prove that every certified route is executable before Solver work."""
    if not compiled.proof_contract:
        return (), ()
    rows: list[RoutePreflightRow] = []
    issues: list[RoutePreflightIssue] = []
    overlay = VerifierOverlay(executor, envmap.workspace_root, max_command_timeout_s=60)
    try:
        for clause in compiled.proof_contract:
            clause_id = str(clause.get("clause_id", "")).strip()
            route = str(clause.get("verifier_route", "")).strip()
            kind, target = _route_parts(route)
            solver_available = False
            verifier_available = False
            solver_detail = ""
            verifier_detail = ""

            if kind == "overlay_run_command":
                executable = _command_executable(target)
                solver_available, solver_detail = _command_available(
                    executor, executable, cwd=envmap.workspace_root,
                )
                verifier_available, verifier_detail = _overlay_command_available(overlay, executable)
            elif kind in {"read_file", "inspect_artifact"}:
                solver_available = all(hasattr(executor, name) for name in ("exists", "read_file"))
                verifier_available = solver_available
                solver_detail = "filesystem inspection methods available" if solver_available else "filesystem inspection method missing"
                verifier_detail = solver_detail
            elif kind == "rerun_check":
                check = compiled.eval_index.get(target)
                solver_available = check is not None and hasattr(executor, "run_command")
                verifier_available = solver_available
                solver_detail = "compiled check available" if solver_available else f"compiled check not found: {target}"
                verifier_detail = solver_detail
            elif kind in {"probe_port", "probe_http", "probe_process"}:
                available = hasattr(executor, "probe_process")
                solver_available = verifier_available = available
                solver_detail = verifier_detail = "probe method available" if available else "probe method missing"
            elif kind == "perceive_artifact":
                available = bool(getattr(envmap, "capabilities", {}).get("perception"))
                solver_available = verifier_available = available
                solver_detail = verifier_detail = "perception capability available" if available else "perception capability unavailable"
            else:
                solver_detail = verifier_detail = f"unsupported preflight route kind: {kind}"

            row = RoutePreflightRow(
                clause_id=clause_id,
                route=route,
                route_kind=kind,
                solver_available=solver_available,
                verifier_available=verifier_available,
                solver_detail=solver_detail,
                verifier_detail=verifier_detail,
            )
            rows.append(row)
            if not solver_available:
                issues.append(RoutePreflightIssue(
                    "solver_route_preflight_failed", clause_id, route, solver_detail,
                ))
            if not verifier_available:
                issues.append(RoutePreflightIssue(
                    "verifier_route_preflight_failed", clause_id, route, verifier_detail,
                ))
            if solver_available != verifier_available:
                issues.append(RoutePreflightIssue(
                    "solver_verifier_route_parity_failed",
                    clause_id,
                    route,
                    f"solver_available={solver_available} verifier_available={verifier_available}",
                ))
    finally:
        overlay.teardown()
    return tuple(rows), tuple(issues)
