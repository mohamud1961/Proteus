# Ceiling

Ceiling behavior for this sentinel:

- Selects required evidence IDs only from active rows.
- Computes final scalar `7421` correctly.
- Explicitly rejects stale row IDs and closes after verifier pass.
