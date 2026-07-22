"""Mechanical task-container network policy tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.envmap_builder import build_envmap_from_task
from aether_next.monitors import LocalOnlySafetyGuard
from aether_next.network_policy import (
    EXTERNAL_UNRESTRICTED,
    LOOPBACK_ONLY,
    resolve_network_policy,
)
from aether_next.runners.docker_runner import _build_task_container_command
from aether_next.runtime_ir import ActionRequest, CapabilityDescriptor, EnvMap, RuntimeConfigIR


def test_default_policy_is_mechanically_isolated_loopback() -> None:
    policy = resolve_network_policy({}, environ={})
    assert policy.scope == LOOPBACK_ONLY
    assert policy.source == "certified_default"
    assert policy.docker_args == ("--network", "none")


def test_explicit_external_scope_is_supported_not_universally_blocked() -> None:
    policy = resolve_network_policy({}, explicit_scope="external_unrestricted", environ={})
    assert policy.scope == EXTERNAL_UNRESTRICTED
    assert policy.source == "runner_argument"
    assert policy.docker_args == ()


def test_public_environment_and_operator_scopes_have_deterministic_precedence() -> None:
    metadata = {"environment": {"network_scope": "external_unrestricted"}}
    assert resolve_network_policy(metadata, environ={}).source == "task_environment.network_scope"
    assert resolve_network_policy(
        metadata,
        explicit_scope="loopback_only",
        environ={"AETHER_TASK_NETWORK_SCOPE": "external_unrestricted"},
    ).scope == LOOPBACK_ONLY
    assert resolve_network_policy(
        {}, environ={"AETHER_TASK_NETWORK_SCOPE": "external_unrestricted"},
    ).scope == EXTERNAL_UNRESTRICTED


def test_unknown_or_endpoint_claim_scope_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported network scope"):
        resolve_network_policy({}, explicit_scope="declared_endpoints")


def test_production_container_command_contains_real_docker_boundary() -> None:
    isolated = resolve_network_policy({}, explicit_scope="loopback_only")
    open_policy = resolve_network_policy({}, explicit_scope="external_unrestricted")
    isolated_cmd = _build_task_container_command(
        image="task-image", workspace_dir="/tmp/work", network_policy=isolated,
    )
    open_cmd = _build_task_container_command(
        image="task-image", workspace_dir="/tmp/work", network_policy=open_policy,
    )
    assert isolated_cmd[:5] == ["docker", "run", "-d", "--network", "none"]
    assert "--network" not in open_cmd
    assert isolated_cmd[-3:] == ["task-image", "sleep", "infinity"]


def test_envmap_reports_enforced_policy_not_post_start_probe(tmp_path: Path) -> None:
    envmap = build_envmap_from_task(
        str(tmp_path),
        "Complete the task.",
        network_scope=LOOPBACK_ONLY,
        task_metadata={
            "network_policy": resolve_network_policy({}, explicit_scope=LOOPBACK_ONLY).as_dict(),
            "environment_probe": {"network": {"status": "probed_true"}},
        },
    )
    assert envmap.network_scope == LOOPBACK_ONLY
    request = __import__("aether_next.kernel_messages", fromlist=["build_architect_request"]).build_architect_request(
        envmap,
        ConfigCompiler(CapabilityRegistry.from_envmap(envmap)),
    )
    assert request["envmap"]["network_scope"] == LOOPBACK_ONLY
    assert request["envmap"]["task_metadata"]["network_policy"]["mechanical_boundary"] == "docker_network_none"


def _compiled():
    return SimpleNamespace()


def test_structured_external_target_blocked_in_loopback_scope() -> None:
    action = ActionRequest(
        action_id="probe",
        kind="probe_service",
        capability_id="service_probe",
        arguments={"target": "example.com:443"},
        intent="probe",
        expected_observation="status",
        if_fail_next="stop",
    )
    guard = LocalOnlySafetyGuard()
    assert guard.violation(_compiled(), action, network_scope=LOOPBACK_ONLY)
    assert guard.violation(_compiled(), action, network_scope=EXTERNAL_UNRESTRICTED) is None


def test_shell_text_is_not_misrepresented_as_network_enforcement() -> None:
    action = ActionRequest(
        action_id="command",
        kind="run_command",
        capability_id="shell",
        arguments={"command": "python3 -c 'import socket; socket.create_connection((\"example.com\",443))'"},
        intent="attempt egress",
        expected_observation="network namespace decides",
        if_fail_next="stop",
    )
    assert LocalOnlySafetyGuard().violation(
        _compiled(), action, network_scope=LOOPBACK_ONLY,
    ) is None
