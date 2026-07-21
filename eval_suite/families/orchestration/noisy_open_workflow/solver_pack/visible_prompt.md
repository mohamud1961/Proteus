# fhard_08: Original Noisy Open Workflow

You are in `/workspace/case`.

Goal:
- Produce `final/closure_report.md`.
- Produce `out/final_submission.json`.

The workspace contains mixed-quality evidence. Some notes are stale or contradictory.

Required report sections:
1. `## Decision`
2. `## Evidence IDs`
3. `## Rejected Distractors`
4. `## Verification`

Rules:
- Determine the correct final ticket and owner from evidence.
- Include evidence IDs that support the decision.
- `out/final_submission.json` must include:
  - `report_path`
  - `selected_ticket`
  - `selected_owner`
  - `verifier_command`
- Run visible check:
  `python3 checks/visible_check.py --report final/closure_report.md`
- Hidden grading verifies exact decision correctness, distractor rejection, and closure discipline.
