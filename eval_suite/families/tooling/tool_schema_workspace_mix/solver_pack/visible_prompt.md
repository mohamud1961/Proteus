# fhard_07: Original Tool-Schema Workspace Mix

You are in `/workspace/project`.

Deliverables:
- `out/dispatch_plan.json`
- `out/final_submission.json`
- update `config/active_workspace.txt`

Constraints:
- Follow the operation order in `ops/contracts/tool_schema.json`.
- Use latest state from `data/live_snapshot.ndjson`.
- `tmp/tool_cache/snapshot.json` is not authoritative.
- Run visible verifier before final answer:
  `python3 scripts/visible_verify.py --dispatch out/dispatch_plan.json --workspace config/active_workspace.txt`

`out/final_submission.json` must include:
- `dispatch_plan_path`
- `selected_workspace_id`
- `selected_ticket`
- `verifier_command`
