from __future__ import annotations

from types import SimpleNamespace

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.execution import CommandResult
from aether_next.kernel_dispatch import dispatch_action
from aether_next.ledger import ExecutionLedger
from aether_next.runtime_ir import (
    ActionRequest,
    CapabilityDescriptor,
    EnvMap,
    RuntimeConfigIR,
)


class FakeExecutor:
    def run_command(self, command: str, *, cwd: str | None = None, timeout_s: int = 30):
        del cwd, timeout_s
        return CommandResult(
            command=command,
            exit_code=0,
            stdout="done",
            removed_paths=("critical.asset",),
            state_delta={
                "before_digest": "before",
                "after_digest": "after",
                "removed_paths": ["critical.asset"],
                "created_paths": [],
                "content_changed_paths": [],
                "metadata_changed_paths": [],
                "mutation_actor_status": "mutation_actor_unknown",
            },
        )


class IntegrityGuard:
    def __init__(self) -> None:
        self.seen: tuple[str, ...] = ()

    def validate_modified_paths(self, objective, paths):
        del objective
        self.seen = tuple(paths)
        return "protected path removed" if "critical.asset" in self.seen else None


def _compiled():
    env = EnvMap(
        task_prompt="preserve state",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor("filesystem", "files", tool_names=("read_file", "write_file")),
        },
    )
    ir = RuntimeConfigIR(
        architect_summary="run command",
        solver_identity_prompt="work carefully",
        selected_capabilities=("shell", "filesystem"),
        inspection_plan=("inspect",),
        proof_plan=("check",),
    )
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env), env


def test_run_command_receipt_retains_removed_paths_and_state_delta() -> None:
    compiled, env = _compiled()
    guard = IntegrityGuard()
    kernel = SimpleNamespace(
        failure_parser=SimpleNamespace(classify=lambda *args, **kwargs: ""),
        integrity_guards=guard,
    )
    action = ActionRequest(
        action_id="cmd",
        kind="run_command",
        capability_id="shell",
        arguments={"command": "mutate"},
        intent="exercise command",
        expected_observation="state delta",
        if_fail_next="inspect",
    )
    receipt = dispatch_action(
        kernel, action, 1, compiled, FakeExecutor(), env, ExecutionLedger()
    )[0]
    assert receipt.state_change is True
    assert receipt.payload["removed_paths"] == ("critical.asset",)
    assert receipt.payload["state_delta"]["before_digest"] == "before"
    assert receipt.payload["state_delta"]["mutation_actor_status"] == "mutation_actor_unknown"
    assert guard.seen == ("critical.asset",)
    assert receipt.payload["integrity_violation"] == "protected path removed"
