# Whole Harness

The whole-harness lane runs all eval families together in a single orchestrated
pass. This directory is the entry-point documentation for that lane.

## What the whole-harness lane covers

The final harness evaluation suite v1 includes 13 rows across 4 row types:

| row type    | count | examples |
|-------------|:-----:|----------|
| hard        | 8     | service_lifecycle_readiness_flagship, original_repo_recovery_flagship, tool_schema_workspace_mix |
| sentinel    | 3     | fsent_01_tool_call_composite, fsent_02_runtime_workspace_contract, fsent_04_retrieval_reduction_closure |
| composition | 2     | fsent_03_filesystem_verifier_repair, fsent_05_long_handoff_composition_smoke |

Plus 6 smoke family lanes that can be run independently or as part of the
whole-harness pass.

## Canonical board

- `eval_suite/boards/public_eval_harness_v1.json` — the primary whole-harness
  board. Lists all 6 smoke family task packs and references the whole-harness
  registries for the hard, sentinel, and composition rows.
- `eval_suite/whole_harness/final_harness_v1/final_suite_registry.yaml` — the
  authoritative YAML registry used by the harness runner. Contains all 8 hard
  rows with full metadata (failure clusters, provenance, contamination status).
- `eval_suite/whole_harness/final_harness_v1/sentinel_composition_board.yaml` —
  the sentinel/composition row registry with promotion gate policy.

## Admission levels

- `diagnostic` — rows run for measurement; failure does not block promotion
- `certified` — rows that must pass to certify the recipe stack

All 13 task-pack rows in the final harness suite target `certified` admission.
The 6 smoke lanes run at `diagnostic`.

## Runtime-Control Sub-Lane

The `eval_suite/whole_harness/runtime_control_harness_v1/` directory contains the smoke-families-only
sub-lane (`runtime_control_harness_v1.json`), which runs just the 6
smoke families without the pressure rows.

## Example scoreboard

- `eval_suite/scoreboards/public_eval_harness_v1.example.scoreboard.json` —
  example output structure showing the score fields, verdict, and failure class
  breakdown. This is a structural example only; scores are not fabricated.
