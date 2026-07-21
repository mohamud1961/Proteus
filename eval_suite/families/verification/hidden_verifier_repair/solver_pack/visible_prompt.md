# Task: Repair behavior beyond visible tests

Visible checks pass intermittently, but production behavior still fails.

Goal:
- diagnose hidden-failure risk,
- apply repair in the correct module,
- rerun verification,
- output `candidate/fix_report.json`.

Required report fields:
- `visible_tests_pass`
- `hidden_case_pass`
- `hidden_case_hypothesis`
- `regression_guard`
- `final_verifier_rerun`

Field typing contract:
- `visible_tests_pass`, `hidden_case_pass`, `regression_guard`, and `final_verifier_rerun` must be boolean values.
- `hidden_case_hypothesis` must be a short string explanation.

Do not claim completion without a rerun after your repair.
