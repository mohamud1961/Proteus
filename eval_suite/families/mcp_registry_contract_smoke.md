# MCP Registry Contract Smoke

Family summary for the registry and discovery contract pack.

## Public Artifacts

- `eval_suite/families/tooling/mcp_registry_contract_smoke/README.md`
- `eval_suite/families/tooling/mcp_registry_contract_smoke/task_pack.json`
- `eval_suite/families/tooling/mcp_registry_contract_smoke/grader.py`
- `eval_suite/boards/mcp_registry_contract_smoke_v1.json`
- `eval_suite/scoreboards/mcp_registry_contract_smoke_v1.example.scoreboard.json`

## Summary

- surface: registry runtime contract
- admission: diagnostic
- contamination: clean synthetic
- public role: a typed registry and discovery smoke for public MCP-facing surfaces

## Notes

The family demonstrates deterministic registry shape handling without exposing
private runtime state or hidden adapter logic.
