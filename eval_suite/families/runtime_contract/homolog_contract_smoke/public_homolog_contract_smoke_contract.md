# Homolog Contract Smoke Schema

This page describes the public-safe shapes used by
`eval_suite/families/runtime_contract/homolog_contract_smoke/`.

## Task Pack

Required fields:

- `schema_version`
- `family_id`
- `source_note`
- `contamination_policy`
- `tasks`

Each task requires:

- `task_id`
- `task_type`
- `failure_family`
- `task_prompt`
- `deliverable_path`
- `admission_level`
- `surface_type`
- `public_notes`

## Board

The board manifest should:

- point at the task pack;
- point at the example scoreboard;
- identify itself as a public example;
- keep contamination status clean and synthetic;
- avoid benchmark-row claims.

## Example Scoreboard

The scoreboard should:

- set `example_only` to `true`;
- explain that it is public example output, not benchmark evidence;
- include result rows and aggregate totals;
- stay free of private paths, hidden grader details, and raw trajectories.
