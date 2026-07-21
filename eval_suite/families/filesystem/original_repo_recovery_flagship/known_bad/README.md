# Known Bad Reference

Known-bad behaviors for this row:

- Writes `recovery_bundle.json` but keeps `RUNTIME_HOST=catalog-old.internal`.
- Uses draft closure ticket `RCV-2026-318` from `postmortem_draft.md`.
- Skips visible verifier and submits `out/final_submission.json` directly.

Expected grader outcome: `fail` with `runtime_env_mismatch`, `bundle_field_mismatch`, or `missing_verifier_pass_event`.
