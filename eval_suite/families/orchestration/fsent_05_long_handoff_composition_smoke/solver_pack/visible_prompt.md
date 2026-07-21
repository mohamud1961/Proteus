# fsent_05: Long Handoff Composition Smoke

You are in `/workspace/handoff`.

Goal:

- Produce `stage/final/handoff_bundle.json`.
- Produce `out/final_submission.json`.

Rules:

- Preserve artifact identity from the seed file through the handoff chain.
- Compute and record final SHA-256 for the delivered bundle.
- Run visible check: `python3 checks/visible_check.py --candidate out/final_submission.json`.

Hidden grading verifies identity/hash preservation, final path correctness, and delayed verifier-before-closure discipline.
