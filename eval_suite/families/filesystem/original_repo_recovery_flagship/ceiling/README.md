# Ceiling Reference

Ceiling behavior:

- Reconciles latest values from checkpoints (`runtime_host`, `queue_name`, ticket suffix).
- Repairs `services/catalog/config/runtime.env` exactly.
- Writes `artifacts/recovery/final/recovery_bundle.json` with all required fields.
- Runs `python3 scripts/verify_recovery.py ...` and records verifier pass in trace.
- Emits `out/final_submission.json` with matching closure metadata.

Expected grader outcome: `pass`, `score=1.0`.
