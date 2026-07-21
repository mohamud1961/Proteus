# Public Eval Map Contract

This document describes the public shapes used by the family-level eval map,
the whole-harness summary, and the renamed calibration/adaptation surfaces.

It is a documentation contract for review and navigation. It does not expose
private verifier internals, raw traces, or private task-pack contents.

## Family Index Entry

Required fields:

- `family_id`
- `summary_ref`
- `task_pack_ref`
- `board_ref`
- `scoreboard_ref`
- `surface_type`
- `admission_level`
- `contamination_status`
- `provenance_note`

## Whole Harness Board

Required fields:

- `board_id`
- `board_type`
- `scope_label`
- `family_index_ref`
- `family_summary_refs`
- `calibration_lane_board_ref`
- `pressure_family_board_ref`
- `scoreboard_ref`
- `contamination_policy`
- `notes`

## Calibration Lane Board

Required fields:

- `board_id`
- `board_type`
- `scope_label`
- `lane_groups`
- `scoreboard_ref`
- `contamination_policy`
- `notes`

Each lane group should state:

- `group_id`
- `public_name`
- `surface_type`
- `admission_level`
- `contamination_status`
- `provenance_note`

## Adapted Pressure Board

Required fields:

- `board_id`
- `board_type`
- `scope_label`
- `family_groups`
- `scoreboard_ref`
- `contamination_policy`
- `notes`

Each family group should state:

- `group_id`
- `public_name`
- `surface_type`
- `admission_level`
- `contamination_status`
- `provenance_note`

## Scoreboard Guidance

Public example scoreboards should:

- set `example_only` to `true`;
- keep the rows summary-level and public-safe;
- avoid raw run references, hidden verifier details, and private workspace
  paths;
- distinguish summary rows from actual execution rows in the row metadata;
- keep any provenance note at the public-summary level rather than inside a
  hidden contract.

## Provenance Guidance

Public-safe provenance should say where the summary came from in broad terms,
such as a private collab registry or a sanitized board summary, without
exporting raw evidence bundles or hidden task internals.
