# fhard_06: Original Repo Recovery Flagship

You are in a degraded repo at `/workspace/repo`.

Objective:
1) Repair `services/catalog/config/runtime.env` using the latest trustworthy evidence.
2) Build `artifacts/recovery/final/recovery_bundle.json`.
3) Produce `out/final_submission.json` describing your closure.

Required closure fields in `out/final_submission.json`:
- `output_path`
- `closure_ticket`
- `runtime_host`
- `queue_name`
- `verifier_command`

Rules:
- Use only solver-visible files.
- Do not edit any file under `reviewer_pack` or `grader`.
- Visible verifier command: `python3 scripts/verify_recovery.py --bundle artifacts/recovery/final/recovery_bundle.json --config services/catalog/config/runtime.env`
- The hidden grader checks final truth, not only visible verifier success.

Messiness notes:
- Some docs and backups are stale.
- At least one draft report contains a wrong closure ticket.
- Latest evidence is split across multiple checkpoint files.
