# Final Harness V1

Harness-wide registry files for the final evaluation suite v1.

## Included files

- `final_suite_registry.yaml` — authoritative suite registry listing all 8 hard
  rows and 5 sentinel/composition rows with failure cluster assignments,
  contamination status, and promotion gate policy
- `hard_task_registry.yaml` — detailed hard-row registry with per-row metadata
  (task pack refs, fixture refs, visible verifier refs, grader refs)
- `sentinel_composition_board.yaml` — sentinel/composition gate board with row
  slot assignments pointing to `eval_suite/families/` task packs
- `pressure_family_provenance.yaml` — provenance and mutation policy for all
  pressure-family task packs
- `current_stack_manifest.yaml` — stack manifest snapshot at final suite v1
- `family_winner_registry.yaml` — family-winner tracking snapshot (empty winner
  list; control-only baseline)

## Canonical task pack location

All task pack artifacts live under `eval_suite/families/`. The
`hard_task_registry.yaml` and `final_suite_registry.yaml`
reference those packs by their neutral names.

## Hidden verifier policy

The `hidden_verifier_ref` fields that previously appeared in `hard_task_registry.yaml`
have been removed from this public tree. The hidden verifier Python files
(`reviewer_pack/hidden_verifier.py`) and truth files (`reviewer_pack/hidden_truth.json`)
are withheld from the public tree to preserve eval integrity. Each task pack's
`grader/README.md` notes this.

## Attempts

Real attempt artifacts live in `eval_suite/whole_harness/final_harness_v1/attempts/`.
