# Runtime Policy Hook Smoke

Family summary for the policy-hook and visibility-ordering smoke pack.

## Public Artifacts

- `eval_suite/families/environment/runtime_policy_hook_smoke/README.md`
- `eval_suite/families/environment/runtime_policy_hook_smoke/task_pack.json`
- `eval_suite/families/environment/runtime_policy_hook_smoke/grader.py`
- `eval_suite/boards/runtime_policy_hook_smoke_v1.json`
- `eval_suite/scoreboards/runtime_policy_hook_smoke_v1.example.scoreboard.json`

## Summary

- surface: synthetic substrate smoke
- admission: diagnostic
- contamination: clean synthetic
- public role: a visible-denial and ordering guard for runtime policy behavior

## Notes

This family stays narrow on purpose: the public value is in making the guard
surface reviewable, not in claiming broad coverage.
