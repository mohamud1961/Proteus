# Harness Registry Smoke Pack

**Group:** whole_harness / harness_registry_smoke

## What this tests

Whole-harness level smoke test verifying that the harness registry report
is produced correctly with the right shape and content. This pack tests the
harness as a whole (not a single capability family), confirming that:

- The harness registry ID and scope are correct.
- The expected number of family packs (6) are registered.
- The registry validity flag is set.
- All family packs are marked as present.

## Grader

Offline deterministic grader (`grader.py`) — no network, no docker required.
Produces a JSON score offline. Run via:

```
python3.11 -m runner run-eval eval_suite/whole_harness/harness_registry_smoke/task_pack.json
```

## Offline status

Fully offline. No external dependencies. Part of the standard offline suite.
