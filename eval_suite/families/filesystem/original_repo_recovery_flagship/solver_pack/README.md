# Solver Pack

- Task pack: `fhard_06_original_repo_recovery_flagship`
- Canonical root: `/workspace/repo`
- Required artifact: `artifacts/recovery/final/recovery_bundle.json`
- Required candidate json: `out/final_submission.json`

Suggested workflow:
1. Inspect `ops/release/handoff_manifest.json` for contract requirements.
2. Reconcile data from `data/checkpoints/` while rejecting stale drafts.
3. Patch runtime config and build final bundle.
4. Run visible verifier before finalizing the submission.
