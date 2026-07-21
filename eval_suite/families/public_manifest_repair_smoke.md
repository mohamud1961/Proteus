# Public Manifest Repair Smoke

Family summary for the synthetic filesystem repair pack.

## Public Artifacts

- `eval_suite/families/filesystem/public_manifest_repair_smoke/README.md`
- `eval_suite/families/filesystem/public_manifest_repair_smoke/task_pack.json`
- `eval_suite/families/filesystem/public_manifest_repair_smoke/grader.py`
- `eval_suite/boards/public_manifest_repair_smoke_v1.json`
- `eval_suite/scoreboards/public_manifest_repair_smoke_v1.example.scoreboard.json`

## Summary

- surface: verifier repair
- admission: diagnostic
- contamination: clean synthetic
- public role: the smallest executable family in the public eval map

## Notes

The pack uses a messy synthetic release workspace, a deterministic grader, and
decoy files so the public slice still feels like a real eval rather than a toy
fixture.
