# MCP Registry Contract Smoke

This public smoke pack checks the Aether MCP registry/runtime
slice at the harness substrate level only.

It is diagnostic evidence, not external-suite evidence.

The contract requires:

- deterministic MCP discovery order;
- faithful MCP schema mapping into function-call schemas;
- visible typed outcomes for success, timeout, error, unavailable, and
  schema-mapping-failure states;
- preserved hook/permission evidence;
- no native tool regression.
