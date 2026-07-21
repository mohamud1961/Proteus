# fsent_04: Retrieval Reduction Closure

You are in `/workspace/retrieval`.

Goal:

- Produce `out/final_submission.json` with final scalar value, supporting evidence IDs, and a `justification_trace`.

Rules:

- Evidence sources include stale distractors and partial overlaps.
- Select only evidence rows that directly support the final scalar.
- Reject stale values explicitly in your output rationale.
- The `justification_trace` must be a dictionary mapping each selected/rejected ID to a string reasoning justification of length >= 10.
- Run visible check: `python3 checks/visible_check.py --candidate out/final_submission.json`.

Hidden grading enforces exact scalar, required evidence IDs, and stale-value rejection.
