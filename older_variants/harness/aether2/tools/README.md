# Tools

Canonical tool schema and dispatch surface for `harness.aether2`.

## Modules

- `native.py`
- `permissions.py`

## Notes

This subtree holds the public tool implementation used by the compatibility
package and the canonical namespace.

`permissions.py` is the Python implementation of the
permission-decision substrate. The model-visible tool
schemas stay stable; hook and permission decisions are threaded through
dispatch as audited runtime metadata instead of silent argument rewrites.
