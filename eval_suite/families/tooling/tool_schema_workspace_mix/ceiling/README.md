# Ceiling Reference

Ceiling behavior:

- Reads contract first.
- Extracts latest production ticket from `live_snapshot.ndjson`.
- Writes `out/dispatch_plan.json` with exact required fields and commands.
- Updates `config/active_workspace.txt` to `ws-prod-7`.
- Runs `python3 scripts/visible_verify.py ...` and records verifier pass.

Expected grader result: pass with score 1.0.
