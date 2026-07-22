# Eval Suite

Public home for custom evals, organized by capability family and whole-harness
surface. This folder contains real task packs, fixtures, graders/verifiers,
result rows, boards, scoreboards, and scorecard-style summaries where scored
evidence exists.

The public map now includes:

- executable family packs under `families/<mechanism-family>/<pack>/`
- harness-level aggregation under `whole_harness/`
- real attempt artifacts under `whole_harness/final_harness_v1/attempts/`
- public boards, scoreboards, and scorecard summaries under `boards/` and
  `scoreboards/`

## Sections

- `families/`: mechanism-family directories containing the real task packs,
  fixtures, visible prompts, and graders.
- `whole_harness/`: harness-wide registries, board entry points, and attempt
  artifacts.
- `calibration_lanes/`: public-safe calibration summaries and notes.
- `boards/`: board manifests and calibration/public summary boards.
- `schemas/`: task-pack, grader, result-row, and public map schema docs.
- `adapters/`: source and calibration adapters.
- `graders/`: deterministic grader helpers.
- `fixtures/`: reusable workspaces and task fixtures.
- `scoreboards/`: curated result rows, public scoreboard views, and compact
  scorecard-style summaries.

## Status

This is no longer only a navigation layer. The code-bearing leaves now live
under `families/`, grouped by mechanism family. Harness-wide registries and
attempt artifacts live under `whole_harness/`.
