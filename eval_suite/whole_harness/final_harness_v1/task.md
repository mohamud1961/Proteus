# Whole-Harness Evaluation Reference

This public reference records the intended whole-harness evaluation shape
without publishing official tasks, private evaluation assets, hidden graders, raw
trajectories, or archived benchmark workspaces.

## Scope

The whole-harness lane should exercise the complete run loop:

- environment/workspace setup;
- tool and shell execution;
- artifact capture;
- verifier/grader invocation;
- result-row classification;
- contamination and invalid-row labeling;
- scoreboard aggregation.

## Public Contract

Public artifacts may include:

- family and whole-harness board manifests;
- synthetic or mutated task packs with visible graders;
- example scoreboards that explain result-row shape;
- contamination labels and invalid-row categories;
- continuation notes for missing private evidence.

Public artifacts must exclude:

- official benchmark tasks or copied challenge rows;
- hidden grader implementation details;
- answer keys and private constants;
- raw traces, trajectories, and VM workspaces;
- mirrored source codebases used only for research.

## Current Status

The public tree exposes the evaluation structure and multiple code-bearing
family leaves. It does not claim that the public reference file is itself a
benchmark task. Whole-harness promotion still depends on certified runs in the
private evaluation environment and on public-safe summaries derived from those
runs.
