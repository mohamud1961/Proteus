"""Native + MCP tool registration/discovery/invocation boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import harness.aether2.tools.mcp as mcp_runtime
from harness.aether2.tools.native import TOOL_NAMES, TOOL_SCHEMAS, ToolDispatchOutcome, dispatch, dispatch_with_hooks


ToolKind = Literal["native", "mcp"]


@dataclass(frozen=True)
class ToolRegistryIssue:
    tool_name: str
    kind: str
    reason_code: str
    message: str
    server_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "kind": self.kind,
            "reason_code": self.reason_code,
            "message": self.message,
            "server_name": self.server_name,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    kind: ToolKind
    handler: Callable[[str, dict[str, Any], Any], Any]
    schema: dict[str, Any] | None = None
    server_name: str | None = None
    original_name: str | None = None
    discovery_issue: ToolRegistryIssue | None = None


class ToolRegistry:
    """Canonical registry surface for native tools plus deterministic MCP additions."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._issues: list[ToolRegistryIssue] = []

    def register_native_tool(
        self,
        *,
        name: str,
        schema: dict[str, Any],
        handler: Callable[[str, dict[str, Any], Any], Any] | None = None,
    ) -> RegisteredTool:
        registration = RegisteredTool(
            name=name,
            kind="native",
            handler=dispatch if handler is None else handler,
            schema=schema,
        )
        self._tools[name] = registration
        return registration

    def register_native_defaults(self) -> "ToolRegistry":
        for name, schema in zip(TOOL_NAMES, TOOL_SCHEMAS, strict=True):
            self.register_native_tool(name=name, schema=schema)
        return self

    def register_mcp_connection(self, connection: mcp_runtime.McpServerConnection) -> "ToolRegistry":
        for discovered_tool in connection.tools or mcp_runtime.discover_mcp_tools(connection):
            issue = None
            if discovered_tool.mapping_issue is not None:
                issue = ToolRegistryIssue(
                    tool_name=discovered_tool.qualified_name,
                    kind="mcp",
                    reason_code=discovered_tool.mapping_issue.reason_code,
                    message=discovered_tool.mapping_issue.message,
                    server_name=connection.name,
                    metadata=discovered_tool.mapping_issue.as_dict(),
                )
                self._issues.append(issue)
            self._tools[discovered_tool.qualified_name] = RegisteredTool(
                name=discovered_tool.qualified_name,
                kind="mcp",
                schema=discovered_tool.schema,
                server_name=connection.name,
                original_name=discovered_tool.descriptor.name,
                discovery_issue=issue,
                handler=lambda tool_name, arguments, ctx, *, _connection=connection, _tool=discovered_tool: mcp_runtime.invoke_mcp_tool(
                    tool_name,
                    arguments,
                    ctx,
                    connection=_connection,
                    discovered_tool=_tool,
                ),
            )
        return self

    def tool_names(self, *, discoverable_only: bool = True) -> list[str]:
        registrations = self._tools.values()
        if discoverable_only:
            registrations = [tool for tool in registrations if tool.schema is not None]
        return [tool.name for tool in registrations]

    def all(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def server_names(self) -> list[str]:
        names = {
            tool.server_name
            for tool in self._tools.values()
            if tool.server_name is not None
        }
        return sorted(names)

    def tool_names_for_server(
        self,
        server_name: str,
        *,
        discoverable_only: bool = False,
    ) -> list[str]:
        names: list[str] = []
        for tool in self._tools.values():
            if tool.server_name != server_name:
                continue
            if discoverable_only and tool.schema is None:
                continue
            names.append(tool.name)
        return names

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [dict(tool.schema) for tool in self._tools.values() if tool.schema is not None]

    def issues(self) -> list[ToolRegistryIssue]:
        return list(self._issues)

    def get(self, tool_name: str) -> RegisteredTool | None:
        if tool_name == "query_history":
            return self._tools.get("query_evidence")
        return self._tools.get(tool_name)

    def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: Any,
        *,
        call_id: str | None = None,
    ) -> ToolDispatchOutcome:
        registration = self.get(tool_name)
        if registration is None:
            raise KeyError(f"unknown tool: {tool_name}")
        return dispatch_with_hooks(
            tool_name,
            arguments,
            ctx,
            call_id=call_id,
            handler=registration.handler,
        )


def build_native_tool_registry() -> ToolRegistry:
    return ToolRegistry().register_native_defaults()


__all__ = [
    "RegisteredTool",
    "ToolKind",
    "ToolRegistry",
    "ToolRegistryIssue",
    "build_native_tool_registry",
]
