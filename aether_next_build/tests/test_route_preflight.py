from __future__ import annotations

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import CommandResult
from aether_next.route_preflight import preflight_proof_routes
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    EnvMap,
    RuntimeConfigIR,
)


class ParityExecutor:
    def __init__(
        self,
        *,
        solver_python: bool = True,
        verifier_python: bool = True,
        in_overlay: bool = False,
    ) -> None:
        self.solver_python = solver_python
        self.verifier_python = verifier_python
        self.in_overlay = in_overlay

    def for_workspace(self, workspace_root: str) -> "ParityExecutor":
        assert ".verifier_overlay_" in workspace_root
        return ParityExecutor(
            solver_python=self.solver_python,
            verifier_python=self.verifier_python,
            in_overlay=True,
        )

    def run_command(self, command: str, *, cwd: str | None = None, timeout_s: int = 30) -> CommandResult:
        del timeout_s, cwd
        if command.startswith("rm -rf ") or command.startswith("cp -a ") or " && cp -a " in command:
            return CommandResult(command, 0, "", "")
        if command.startswith("command -v python3"):
            available = self.verifier_python if self.in_overlay else self.solver_python
            return CommandResult(command, 0 if available else 127, "/usr/bin/python3\n" if available else "", "not found" if not available else "")
        return CommandResult(command, 0, "", "")

    def exists(self, path: str) -> bool:
        del path
        return True

    def read_file(self, path: str) -> str:
        del path
        return ""

    def probe_process(self, target: str):
        del target
        return None



def _compiled(route: str):
    env = EnvMap(
        task_prompt="Prove a public protocol.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "files", tool_names=("read_file", "write_file")),
        },
    )
    ir = RuntimeConfigIR(
        architect_summary="Prove the protocol.",
        solver_identity_prompt="Use an independent client.",
        selected_capabilities=("shell", "filesystem"),
        inspection_plan=("inspect contract",),
        proof_plan=("run client",),
        semantic_clause_coverage=({
            "clause_id": "protocol",
            "solver_handling": "implement exact contract",
            "verifier_check": "independent client round trip",
        },),
        semantic_verifier_checks=({
            "clause_id": "protocol",
            "inspection_route": route,
            "fallback_route": None,
            "falsification_check": "client request fails",
            "required_evidence_class": "exact_contract",
        },),
        semantic_false_positive_traps=("open port",),
    )
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
    return compiled, env


def test_route_preflight_passes_when_solver_and_verifier_have_python() -> None:
    compiled, env = _compiled("overlay_run_command:python3 independent_client.py")
    rows, issues = preflight_proof_routes(compiled, ParityExecutor(), env)
    assert issues == ()
    assert rows[0].solver_available is True
    assert rows[0].verifier_available is True


def test_route_preflight_rejects_solver_verifier_interpreter_mismatch() -> None:
    compiled, env = _compiled("overlay_run_command:python3 independent_client.py")
    rows, issues = preflight_proof_routes(
        compiled,
        ParityExecutor(solver_python=True, verifier_python=False),
        env,
    )
    assert rows[0].solver_available is True
    assert rows[0].verifier_available is False
    codes = {issue.code for issue in issues}
    assert "verifier_route_preflight_failed" in codes
    assert "solver_verifier_route_parity_failed" in codes


def test_route_preflight_rejects_missing_solver_and_verifier_interpreter() -> None:
    compiled, env = _compiled("overlay_run_command:python3 independent_client.py")
    _rows, issues = preflight_proof_routes(
        compiled,
        ParityExecutor(solver_python=False, verifier_python=False),
        env,
    )
    codes = [issue.code for issue in issues]
    assert codes == ["solver_route_preflight_failed", "verifier_route_preflight_failed"]
