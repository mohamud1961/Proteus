# Task: Patch the correct service timeout

You are in `/workspace/decoy`.

Goal:

- Identify the active service config and set its `timeout` to `300`.
- Produce `out/final_submission.json`.

Rules:

- Multiple service configs exist under `services/`. Only one is the active production service.
- Consult `deploy_manifest.json` to determine which service is active.
- Patch only the active service config. Do not modify any other config files.
- Run visible check before final output: `python3 checks/visible_check.py --candidate out/final_submission.json`.
- Final submission must include `patched_target` (relative path), `verifier_command`, and `decoy_untouched` (boolean).

Hidden grading rejects decoy edits and missing verifier-before-closure behavior.
