# Schemas

Eval contract definitions, task-pack schemas, and result-row schemas for this
eval suite.

## Files in this directory

- `public_eval_map_contract.md` — prose documentation of the public eval map
  contract. Describes the eval_id / task_pack_id / row_id hierarchy, the
  `GradeResult` schema, contamination gates, and the admission_level ladder
  (diagnostic → certified).

## Schema contracts (not redistributed here)

The full YAML schema files that were used during harness construction live in
`work/ledger/final_harness_eval_suite/` and are not redistributed in this
public tree. They include:

- `current_stack_manifest.schema.yaml` — 179 lines; defines the live recipe
  manifest format (recipe_id, stack_version, row slot references)
- `family_winner_registry.schema.yaml` — 55 lines; defines the family winner
  record (family, winning_recipe, promotion_date, evidence_refs)
- `recipe_candidates.schema.yaml` — 112 lines; defines the recipe candidate
  format (candidate_id, score_vector, contamination_status, invalidity_class)

## Task-pack schema

Each task pack (`task_pack.yaml`) uses `schema_version: final_harness_task_pack.v1`
and contains:

```
task_pack_id  row_id  row_type  is_flagship  provenance_type
admission_level_target  primary_clusters  canonical_workspace_root
solver_visible_prompt_ref  fixture_manifest_ref
grader_ref  timeout_policy_ref  known_bad_ref  ceiling_ref
deterministic_grading  runtime_contract  expected_outputs  task_brief
```

## GradeResult schema

```json
{
  "schema_version": "eval_grader_result.v1",
  "eval_id": "<task_pack_id>",
  "deterministic": true,
  "grader_timeout_seconds": 45,
  "score": 1.0,
  "verdict": "pass",
  "failure_class": "none",
  "reason_codes": [],
  "artifact_mismatches": []
}
```

`failure_class` values: `none`, `tool_contract`, `verification_completion`,
`filesystem_path`, `environment_toolchain`, `retrieval_reduction`,
`long_horizon_orchestration`, `contamination`, `invalid_environment`.
