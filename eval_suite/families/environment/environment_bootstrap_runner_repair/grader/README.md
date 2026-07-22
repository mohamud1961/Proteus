# Grader Note

The `grade.py` in this task pack imports `reviewer_pack/hidden_verifier.py` and
`reviewer_pack/hidden_truth.json` at runtime.

These private answer keys are **withheld from the public tree** to preserve the
integrity of the evaluation. The grader script is included here to document the
grading interface and the visible-side forensic logic (forbidden-access detection,
timeout classification, reason-code mapping), but it cannot be executed standalone
without the hidden verifier.

Solver-visible artifacts in this pack (task_pack.yaml, solver_pack/, visible_verifier.py,
fixture_manifest.json) are fully self-contained and do not depend on the hidden verifier.
