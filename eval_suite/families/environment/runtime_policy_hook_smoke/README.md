# Runtime Policy Hook Smoke

Public-safe smoke eval pack for the first Aether hook and permission port
slice.

It models a small synthetic contract where the candidate workspace must produce
`policy_audit.json` proving:

- `permission_request`, `pre_tool_use`, and `post_tool_use` fire in order;
- denied actions remain visible as ordinary observations;
- denied actions do not mutate workspace state;
- hook execution does not silently rewrite tool arguments.

This is a deterministic smoke surface for substrate validation, not
external-suite evidence.
