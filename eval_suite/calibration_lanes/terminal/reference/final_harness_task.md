# Terminal-Style Calibration Reference

This public reference describes the calibration pressure for terminal-style
harness rows without including official task material, private evaluation assets,
hidden grader internals, raw trajectories, or benchmark fixtures.

## Purpose

The lane checks whether the harness can preserve the core terminal-workflow
contract:

- launch work in the expected workspace;
- keep path resolution stable across runner, verifier, and artifact capture;
- separate environment/setup invalidity from model capability failures;
- preserve evidence needed for later diagnosis;
- avoid promoting rows from trace prose alone.

## Public Evidence Shape

The public repo may include:

- abstracted lane manifests;
- small synthetic or mutated fixtures;
- public-safe score summaries;
- grader/interface contracts that do not reveal private answers.

The public repo must not include:

- official benchmark task text or fixtures;
- private evaluation assets;
- hidden verifier source;
- raw trajectories, traces, or archived workspaces;
- benchmark answer keys or copied challenge rows.

## Continuation Notes

Future work should replace this reference with a fully original
terminal-style public task pack when the repo needs a richer calibration demo.
Until then, this file is a pointer to the calibration pressure, not a copied
task.
