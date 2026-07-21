# Ceiling

Ceiling behavior for this task:

- Reads all 12 frame-text fixtures in order.
- Excludes stale frames (those prefixed with `[STALE - re-recorded]`).
- Applies contradiction corrections: later frame overrides earlier for same move number.
- Removes duplicates, keeping one entry per move number.
- Produces exactly 8 moves in standard algebraic notation, numbered, one per line.
- Passes readiness gate before completion.
