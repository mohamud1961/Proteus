# Operator Notes

- Fast path (old): read cache first for speed.  # stale guidance
- Correct path: schema then live snapshot.
- Pick smallest `priority` among open tickets from production workspace.
