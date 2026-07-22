# Known Bad Reference

Known-bad patterns:

- Selects ticket from `tmp/tool_cache/snapshot.json` (`TK-7770`).
- Writes dispatch first and reads schema later.
- Leaves `config/active_workspace.txt` stale.

Expected outcome: fail with `tool_schema_order_violation` and/or `dispatch_field_mismatch`.
