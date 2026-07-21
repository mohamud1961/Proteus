# fsent_03: Filesystem Verifier Repair

You are in `/workspace/fsverify`.

Goal:

- Patch the correct target file.
- Produce `out/final_submission.json`.

Rules:

- Correct target is in `app/config/`, while similarly named decoys exist in `docs/`.
- Desired repair is to set service retry policy to `retries: 5` in the true target.
- Run visible check before final output: `python3 checks/visible_check.py --candidate out/final_submission.json`.
- Final submission must include patched target path, verifier command, and decoy-untouched claim.

Hidden grading rejects decoy edits and missing verifier-before-closure behavior.
