"""Owned MCP registry/runtime substrate for the Aether harness."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol
import json
import time


ConfigScope = Literal["local", "user", "project", "dynamic", "enterprise", "managed"]
McpTransport = Literal["fake_local", "stdio", "sse", "http", "sdk"]
McpConnectionState = Literal["connected", "failed", "needs-auth", "pending", "disabled"]

_ASCII_MCP_CHARSET = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


@dataclass(frozen=True)
class McpServerConfig:
    """Subset of the scoped MCP config for the Python runtime slice."""

    type: McpTransport
    scope: ConfigScope = "dynamic"
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    timeout_sec: float | None = None


@dataclass(frozen=True)
class McpToolDescriptor:
    """Tool description returned by an MCP server."""

    name: str
    description: str
    input_json_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpToolResult:
    """Visible result payload for a fake/local MCP tool call."""

    content: str = ""
    structured_content: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
    is_error: bool = False


@dataclass(frozen=True)
class McpSchemaMappingIssue:
    server_name: str
    tool_name: str
    qualified_name: str
    reason_code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "qualified_name": self.qualified_name,
            "reason_code": self.reason_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class McpDiscoveredTool:
    descriptor: McpToolDescriptor
    qualified_name: str
    schema: dict[str, Any] | None = None
    mapping_issue: McpSchemaMappingIssue | None = None


class LocalMcpServer(Protocol):
    def list_tools(self) -> list[McpToolDescriptor]:
        ...

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_sec: float | None = None,
    ) -> McpToolResult:
        ...


@dataclass(frozen=True)
class McpServerConnection:
    """Connection-state abstraction over the connected/failed/pending/disabled lifecycle."""

    name: str
    type: McpConnectionState
    config: McpServerConfig
    server: LocalMcpServer | None = None
    error: str | None = None
    tools: tuple[McpDiscoveredTool, ...] = ()
    reconnect_attempt: int | None = None
    max_reconnect_attempts: int | None = None


class McpToolExecutionError(RuntimeError):
    pass


class McpToolTimeoutError(TimeoutError):
    pass


class McpServerUnavailableError(RuntimeError):
    pass


def normalize_name_for_mcp(name: str) -> str:
    """Normalize a server or tool name into the MCP-safe identifier charset."""

    return "".join(character if character in _ASCII_MCP_CHARSET else "_" for character in name)


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp__{normalize_name_for_mcp(server_name)}__{normalize_name_for_mcp(tool_name)}"


def map_mcp_tool_to_schema(
    server_name: str,
    descriptor: McpToolDescriptor,
) -> tuple[dict[str, Any] | None, McpSchemaMappingIssue | None]:
    """Map an MCP tool's input schema into the harness tool schema surface."""

    qualified_name = build_mcp_tool_name(server_name, descriptor.name)
    schema = descriptor.input_json_schema
    if not isinstance(schema, Mapping):
        return None, McpSchemaMappingIssue(
            server_name=server_name,
            tool_name=descriptor.name,
            qualified_name=qualified_name,
            reason_code="mcp_schema_mapping_error",
            message="MCP input schema must be an object-shaped mapping",
        )
    if schema.get("type") != "object":
        return None, McpSchemaMappingIssue(
            server_name=server_name,
            tool_name=descriptor.name,
            qualified_name=qualified_name,
            reason_code="mcp_schema_mapping_error",
            message="MCP input schema must declare type=object for function-call mapping",
        )
    return {
        "type": "function",
        "function": {
            "name": qualified_name,
            "description": descriptor.description,
            "parameters": deepcopy(dict(schema)),
        },
    }, None


def discover_mcp_tools(connection: McpServerConnection) -> tuple[McpDiscoveredTool, ...]:
    """Deterministically discover tools for one connection without touching network state."""

    if connection.type != "connected" or connection.server is None:
        return ()
    discovered: list[McpDiscoveredTool] = []
    for descriptor in connection.server.list_tools():
        schema, issue = map_mcp_tool_to_schema(connection.name, descriptor)
        discovered.append(
            McpDiscoveredTool(
                descriptor=descriptor,
                qualified_name=build_mcp_tool_name(connection.name, descriptor.name),
                schema=schema,
                mapping_issue=issue,
            )
        )
    return tuple(discovered)


def connect_fake_local_server(
    name: str,
    server: LocalMcpServer,
    *,
    config: McpServerConfig | None = None,
) -> McpServerConnection:
    resolved_config = config or McpServerConfig(type="fake_local")
    connection = McpServerConnection(name=name, type="connected", config=resolved_config, server=server)
    return McpServerConnection(
        name=connection.name,
        type=connection.type,
        config=connection.config,
        server=connection.server,
        tools=discover_mcp_tools(connection),
    )


def disabled_mcp_server(name: str, *, config: McpServerConfig | None = None) -> McpServerConnection:
    return McpServerConnection(name=name, type="disabled", config=config or McpServerConfig(type="fake_local"))


def failed_mcp_server(name: str, error: str, *, config: McpServerConfig | None = None) -> McpServerConnection:
    return McpServerConnection(name=name, type="failed", config=config or McpServerConfig(type="fake_local"), error=error)


class FakeLocalMcpServer:
    """Deterministic in-process MCP server fixture used by tests and eval smoke packs."""

    def __init__(
        self,
        *,
        tools: list[McpToolDescriptor],
        handlers: Mapping[str, Any],
    ) -> None:
        self._tools = list(tools)
        self._handlers = dict(handlers)

    def list_tools(self) -> list[McpToolDescriptor]:
        return list(self._tools)

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_sec: float | None = None,
    ) -> McpToolResult:
        handler = self._handlers.get(tool_name)
        if handler is None:
            raise McpServerUnavailableError(f'MCP tool "{tool_name}" is not available on the fake local server')
        try:
            value = handler(dict(arguments), timeout_sec=timeout_sec)
        except TypeError as exc:
            if "timeout_sec" not in str(exc):
                raise
            value = handler(dict(arguments))
        return _coerce_tool_result(value)


def invoke_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
    ctx: Any,
    *,
    connection: McpServerConnection,
    discovered_tool: McpDiscoveredTool,
) -> Any:
    """Call one discovered MCP tool and surface every outcome as a typed observation."""

    started_at = time.monotonic()
    cwd = str(getattr(getattr(ctx, "executor", None), "workspace_root", "."))
    if connection.type != "connected" or connection.server is None:
        return ctx.observe_synthetic(
            _mcp_error_raw(
                tool_name=tool_name,
                message=f'MCP server "{connection.name}" is {connection.type}',
                reason_code="mcp_server_unavailable",
                started_at=started_at,
                cwd=cwd,
                failure_class="mcp_connection",
                details={"server_name": connection.name, "connection_type": connection.type},
            )
        )
    if discovered_tool.mapping_issue is not None:
        return ctx.observe_synthetic(
            _mcp_error_raw(
                tool_name=tool_name,
                message=discovered_tool.mapping_issue.message,
                reason_code=discovered_tool.mapping_issue.reason_code,
                started_at=started_at,
                cwd=cwd,
                failure_class="schema_mapping",
                details=discovered_tool.mapping_issue.as_dict(),
            )
        )
    try:
        result = connection.server.call_tool(
            discovered_tool.descriptor.name,
            dict(arguments),
            timeout_sec=connection.config.timeout_sec,
        )
        if result.is_error:
            return ctx.observe_synthetic(
                _mcp_error_raw(
                    tool_name=tool_name,
                    message=result.content or "MCP tool returned an error result",
                    reason_code="mcp_tool_error",
                    started_at=started_at,
                    cwd=cwd,
                    failure_class="mcp_runtime",
                    details={
                        "server_name": connection.name,
                        "original_tool_name": discovered_tool.descriptor.name,
                        "meta": result.meta,
                    },
                )
            )
        return ctx.observe_synthetic(
            {
                "tool": tool_name,
                "exit_code": 0,
                "duration_sec": time.monotonic() - started_at,
                "cwd": cwd,
                "stdout": _render_result_stdout(result),
                "stderr": "",
            }
        )
    except McpToolTimeoutError as exc:
        return ctx.observe_synthetic(
            _mcp_error_raw(
                tool_name=tool_name,
                message=str(exc),
                reason_code="mcp_tool_timeout",
                started_at=started_at,
                cwd=cwd,
                failure_class="timeout",
                details={"server_name": connection.name, "original_tool_name": discovered_tool.descriptor.name},
            )
        )
    except McpServerUnavailableError as exc:
        return ctx.observe_synthetic(
            _mcp_error_raw(
                tool_name=tool_name,
                message=str(exc),
                reason_code="mcp_server_unavailable",
                started_at=started_at,
                cwd=cwd,
                failure_class="mcp_connection",
                details={"server_name": connection.name, "original_tool_name": discovered_tool.descriptor.name},
            )
        )
    except McpToolExecutionError as exc:
        return ctx.observe_synthetic(
            _mcp_error_raw(
                tool_name=tool_name,
                message=str(exc),
                reason_code="mcp_tool_error",
                started_at=started_at,
                cwd=cwd,
                failure_class="mcp_runtime",
                details={"server_name": connection.name, "original_tool_name": discovered_tool.descriptor.name},
            )
        )
    except Exception as exc:  # noqa: BLE001 - visible typed error, not a hidden exception
        return ctx.observe_synthetic(
            _mcp_error_raw(
                tool_name=tool_name,
                message=str(exc),
                reason_code="mcp_tool_runtime_error",
                started_at=started_at,
                cwd=cwd,
                failure_class="mcp_runtime",
                details={"server_name": connection.name, "original_tool_name": discovered_tool.descriptor.name},
            )
        )


def _coerce_tool_result(value: Any) -> McpToolResult:
    if isinstance(value, McpToolResult):
        return value
    if isinstance(value, str):
        return McpToolResult(content=value)
    if isinstance(value, Mapping):
        payload = dict(value)
        if "content" in payload or "structured_content" in payload or "meta" in payload or "is_error" in payload:
            return McpToolResult(
                content=str(payload.get("content", "")),
                structured_content=_coerce_optional_mapping(payload.get("structured_content")),
                meta=_coerce_optional_mapping(payload.get("meta")),
                is_error=bool(payload.get("is_error", False)),
            )
        return McpToolResult(content=json.dumps(payload, sort_keys=True), structured_content=dict(payload))
    return McpToolResult(content=str(value))


def _coerce_optional_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    return {"value": deepcopy(value)}


def _render_result_stdout(result: McpToolResult) -> str:
    chunks: list[str] = []
    if result.content:
        chunks.append(result.content)
    if result.structured_content is not None:
        chunks.append(json.dumps(result.structured_content, sort_keys=True))
    if result.meta is not None:
        chunks.append(json.dumps({"_meta": result.meta}, sort_keys=True))
    return "\n".join(chunk for chunk in chunks if chunk)


def _mcp_error_raw(
    *,
    tool_name: str,
    message: str,
    reason_code: str,
    started_at: float,
    cwd: str,
    failure_class: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tool": tool_name,
        "exit_code": 1,
        "duration_sec": time.monotonic() - started_at,
        "cwd": cwd,
        "stdout": "",
        "stderr": message,
        "error": {
            "kind": reason_code,
            "message": message,
            "reason_code": reason_code,
            "failure_class": failure_class,
            "details": None if details is None else json.dumps(dict(details), sort_keys=True),
            "tool_name": tool_name,
        },
    }


__all__ = [
    "ConfigScope",
    "FakeLocalMcpServer",
    "LocalMcpServer",
    "McpConnectionState",
    "McpDiscoveredTool",
    "McpSchemaMappingIssue",
    "McpServerConfig",
    "McpServerConnection",
    "McpServerUnavailableError",
    "McpToolDescriptor",
    "McpToolExecutionError",
    "McpToolResult",
    "McpToolTimeoutError",
    "McpTransport",
    "build_mcp_tool_name",
    "connect_fake_local_server",
    "disabled_mcp_server",
    "discover_mcp_tools",
    "failed_mcp_server",
    "invoke_mcp_tool",
    "map_mcp_tool_to_schema",
    "normalize_name_for_mcp",
]
