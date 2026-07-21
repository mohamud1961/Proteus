from __future__ import annotations

import json
import time
from pathlib import Path

from harness.aether2.control.loop import ExecutionContext, run_aether2_loop
from harness.aether2.hooks import HookRegistry, HookResult
from harness.aether2.runtime.executor import ContainerExecutor
from harness.aether2.runtime.jobs import JobRegistry
from harness.aether2.runtime.model_client import ModelResponse
from harness.aether2.runtime.sessions import SessionRegistry
from harness.aether2.runtime.task_spec import TaskSpec
from harness.aether2.tools import (
    FakeLocalMcpServer,
    McpServerConfig,
    McpServerConnection,
    McpToolDescriptor,
    McpToolResult,
    McpToolTimeoutError,
    ToolRegistry,
    build_mcp_tool_name,
    build_native_tool_registry,
    connect_fake_local_server,
)


def _make_execution_context(
    tmp_path: Path,
    *,
    hook_registry: HookRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> tuple[ExecutionContext, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    state_dir = tmp_path / "state"
    raw_log_dir = tmp_path / "raw_logs"
    executor = ContainerExecutor(workspace_root=workspace_root)
    job_registry = JobRegistry(state_dir, backend=executor.backend, container_path_fn=executor.to_container_path)
    session_registry = SessionRegistry(state_dir, backend=executor.backend)
    ctx = ExecutionContext(
        executor=executor,
        job_registry=job_registry,
        session_registry=session_registry,
        raw_log_dir=raw_log_dir,
        hook_registry=hook_registry,
        tool_registry=tool_registry,
    )
    return ctx, workspace_root


def _response(text: str = "", tool_calls: tuple[dict, ...] = ()) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=tool_calls,
        usage={"cached_input_tokens": 0, "fresh_input_tokens": 0},
        status="completed",
        raw_response={},
    )


def _tool_call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {"id": call_id, "type": "function", "name": name, "arguments": json.dumps(arguments)}


def _verify_response() -> ModelResponse:
    payload = {
        "requirements": [
            {
                "requirement": "task complete",
                "verdict": "satisfied",
                "evidence": "checked",
                "evidence_refs": ["checks_results[0]"],
            }
        ],
        "reason_codes": [],
        "summary": "ok",
    }
    return _response(text=json.dumps(payload))


class _ScriptedModelClient:
    def __init__(self, turns: list[ModelResponse]) -> None:
        self.turns = list(turns)
        self.tool_schema_names: list[list[str]] = []

    def call(self, messages, tools, *, cache_prefix_len):  # noqa: ANN001
        del messages, cache_prefix_len
        self.tool_schema_names.append([tool["function"]["name"] for tool in tools if isinstance(tool, dict)])
        tool_names = set(self.tool_schema_names[-1])
        if tool_names and tool_names.issubset({"run_command", "read_file", "job_status", "session_read"}):
            return _verify_response()
        if self.turns:
            return self.turns.pop(0)
        return _response(text="done")


def _make_task(tmp_path: Path, instruction: str = "use the MCP tool") -> TaskSpec:
    task_dir = tmp_path / "task"
    workspace_root = task_dir / "workspace"
    artifacts_dir = task_dir / "artifacts"
    workspace_root.mkdir(parents=True)
    artifacts_dir.mkdir(parents=True)
    return TaskSpec(
        task_id="mcp-registry-task",
        instruction=instruction,
        task_dir=task_dir,
        workspace_root=workspace_root,
        artifacts_dir=artifacts_dir,
    )


def _make_connected_server() -> McpServerConnection:
    tools = [
        McpToolDescriptor(
            name="Echo Payload",
            description="Return the provided payload and emit typed metadata.",
            input_json_schema={
                "type": "object",
                "properties": {
                    "payload": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
        ),
        McpToolDescriptor(
            name="Broken Schema",
            description="Intentionally invalid schema for mapping coverage.",
            input_json_schema={"type": "string"},
        ),
    ]
    server = FakeLocalMcpServer(
        tools=tools,
        handlers={
            "Echo Payload": lambda arguments, timeout_sec=None: McpToolResult(
                content=f"payload={arguments['payload']}",
                structured_content={"count": arguments.get("count", 0)},
                meta={"timeout_sec": timeout_sec},
            ),
            "Broken Schema": lambda _arguments, timeout_sec=None: "should not run",
        },
    )
    return connect_fake_local_server("qa server", server, config=McpServerConfig(type="fake_local", timeout_sec=5))


def test_mcp_registry_discovery_is_deterministic_and_schema_mapping_is_faithful() -> None:
    connection = _make_connected_server()
    registry = build_native_tool_registry().register_mcp_connection(connection)

    echo_name = build_mcp_tool_name("qa server", "Echo Payload")
    broken_name = build_mcp_tool_name("qa server", "Broken Schema")

    assert registry.tool_names()[-1] == echo_name
    assert broken_name not in registry.tool_names()
    assert registry.get(broken_name) is not None
    assert [issue.reason_code for issue in registry.issues()] == ["mcp_schema_mapping_error"]

    echo_schema = registry.get(echo_name)
    assert echo_schema is not None and echo_schema.schema is not None
    assert echo_schema.schema["function"]["name"] == echo_name
    assert echo_schema.schema["function"]["parameters"] == {
        "type": "object",
        "properties": {
            "payload": {"type": "string"},
            "count": {"type": "integer"},
        },
        "required": ["payload"],
        "additionalProperties": False,
    }

    second_registry = build_native_tool_registry().register_mcp_connection(_make_connected_server())
    assert registry.tool_names() == second_registry.tool_names()


def test_mcp_name_normalization_replaces_non_ascii_uniformly() -> None:
    assert build_mcp_tool_name("Acme Registry", "Δelta Tool") == "mcp__Acme_Registry___elta_Tool"


def test_registry_invokes_mcp_tool_with_hook_and_permission_evidence(tmp_path: Path) -> None:
    order: list[str] = []
    hook_registry = HookRegistry()
    hook_registry.register("permission_request", hook_name="permission", callback=lambda _ctx: order.append("permission") or HookResult())
    hook_registry.register("pre_tool_use", hook_name="pre", callback=lambda _ctx: order.append("pre") or HookResult())
    hook_registry.register("post_tool_use", hook_name="post", callback=lambda _ctx: order.append("post") or HookResult())

    registry = build_native_tool_registry().register_mcp_connection(_make_connected_server())
    ctx, _workspace_root = _make_execution_context(tmp_path, hook_registry=hook_registry, tool_registry=registry)
    tool_name = build_mcp_tool_name("qa server", "Echo Payload")

    outcome = registry.invoke(tool_name, {"payload": "hello", "count": 2}, ctx, call_id="call-1")

    assert order == ["permission", "pre", "post"]
    assert outcome.envelope.exit_code == 0
    assert "payload=hello" in outcome.envelope.stdout_head
    assert '"count": 2' in outcome.envelope.stdout_head
    assert outcome.permission_decision is not None
    assert outcome.permission_decision["behavior"] == "allow"
    assert [entry["event"] for entry in outcome.hook_trace] == [
        "permission_request",
        "pre_tool_use",
        "post_tool_use",
    ]


def test_registry_surfaces_timeout_error_unavailable_and_schema_mapping_failures(tmp_path: Path) -> None:
    timeout_server = FakeLocalMcpServer(
        tools=[
            McpToolDescriptor(
                name="Slow Tool",
                description="Simulate timeout.",
                input_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
            )
        ],
        handlers={"Slow Tool": lambda _arguments, timeout_sec=None: (_ for _ in ()).throw(McpToolTimeoutError(f"timed out after {timeout_sec}s"))},
    )
    timeout_connection = connect_fake_local_server(
        "slow server",
        timeout_server,
        config=McpServerConfig(type="fake_local", timeout_sec=3),
    )
    timeout_registry = build_native_tool_registry().register_mcp_connection(timeout_connection)
    timeout_ctx, _ = _make_execution_context(tmp_path / "timeout", tool_registry=timeout_registry)
    timeout_outcome = timeout_registry.invoke(build_mcp_tool_name("slow server", "Slow Tool"), {}, timeout_ctx)
    assert timeout_outcome.envelope.error is not None
    assert timeout_outcome.envelope.error.reason_code == "mcp_tool_timeout"

    connected = _make_connected_server()
    failed_connection = McpServerConnection(
        name=connected.name,
        type="failed",
        config=connected.config,
        server=None,
        error="offline",
        tools=connected.tools,
    )
    unavailable_registry = build_native_tool_registry().register_mcp_connection(failed_connection)
    unavailable_ctx, _ = _make_execution_context(tmp_path / "unavailable", tool_registry=unavailable_registry)
    unavailable = unavailable_registry.invoke(build_mcp_tool_name("qa server", "Echo Payload"), {"payload": "x"}, unavailable_ctx)
    assert unavailable.envelope.error is not None
    assert unavailable.envelope.error.reason_code == "mcp_server_unavailable"

    schema_registry = build_native_tool_registry().register_mcp_connection(_make_connected_server())
    schema_ctx, _ = _make_execution_context(tmp_path / "schema", tool_registry=schema_registry)
    broken = schema_registry.invoke(build_mcp_tool_name("qa server", "Broken Schema"), {}, schema_ctx)
    assert broken.envelope.error is not None
    assert broken.envelope.error.reason_code == "mcp_schema_mapping_error"


def test_loop_uses_registry_tool_schemas_and_records_mcp_tool_invocations(tmp_path: Path) -> None:
    tool_name = build_mcp_tool_name("qa server", "Echo Payload")
    registry = build_native_tool_registry().register_mcp_connection(_make_connected_server())
    hooks = HookRegistry()
    hooks.register("permission_request", hook_name="permission", callback=lambda _ctx: HookResult(note="permission"))
    hooks.register("pre_tool_use", hook_name="pre", callback=lambda _ctx: HookResult(note="pre"))
    hooks.register("post_tool_use", hook_name="post", callback=lambda _ctx: HookResult(note="post"))

    task = _make_task(tmp_path)
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    client = _ScriptedModelClient(
        [
            _response(
                text="invoke mcp",
                tool_calls=(_tool_call(tool_name, {"payload": "hello", "count": 1}),),
            ),
            _response(
                text="done",
                tool_calls=(_tool_call("task_done", {"summary": "done", "checks": ["true"]}, call_id="call-2"),),
            ),
        ]
    )

    result = run_aether2_loop(
        task,
        client,
        executor,
        deadline_ts=time.monotonic() + 60,
        hook_registry=hooks,
        tool_registry=registry,
    )

    assert result.finalize_reason == "task_done"
    assert tool_name in client.tool_schema_names[0]
    assert result.tool_invocations[0].tool_name == tool_name
    assert result.tool_invocations[0].permission_decision is not None
    assert [entry["event"] for entry in result.tool_invocations[0].hook_trace] == [
        "permission_request",
        "pre_tool_use",
        "post_tool_use",
    ]

    receipt_dir = task.task_dir / ".aether2" / "host_receipts" / "receipts"
    receipt_path = next(receipt_dir.glob("0001_action_*.json"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["action"]["tool"] == tool_name


def test_registry_keeps_native_tools_working_when_mcp_tools_are_present(tmp_path: Path) -> None:
    registry = build_native_tool_registry().register_mcp_connection(_make_connected_server())
    ctx, workspace_root = _make_execution_context(tmp_path, tool_registry=registry)

    write_outcome = registry.invoke("write_file", {"path": "note.txt", "content": "hello"}, ctx)
    read_outcome = registry.invoke("read_file", {"path": "note.txt"}, ctx)

    assert write_outcome.envelope.exit_code == 0
    assert (workspace_root / "note.txt").read_text(encoding="utf-8") == "hello"
    assert read_outcome.envelope.exit_code == 0
    assert "hello" in read_outcome.envelope.stdout_head
