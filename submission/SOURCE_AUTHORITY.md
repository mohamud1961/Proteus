# Source authority

The source authority for the Aether-derived design review was the clean worktree:

- repository: `harnesseng`
- worktree: `/private/tmp/aether-qualified-integration`
- branch: `codex/aether-qualified-integration`
- source commit: `adc210c3fa339790594cd965fc25397321235055`
- observed status: clean at audit time

Its immediate history includes the evaluator-fidelity change in `adc210c3`, provider-contract qualification evidence in `3442a1f5`, and the layered certification base in `aa800976`. The current dirty checkout at `/Users/mohamud/Downloads/harnesseng` was not copied as source authority.

The source record distinguishes implementation commits from evidence-only commits and preserves negative classifications. In particular, fresh certification evidence and model-board evidence are not silently upgraded into a public product score. The Proteus package is a clean, dependency-free implementation of the public boundary, not an archive of the full harnesseng tree.

Authority limitations:

- Exact private VM/model artifacts are not vendored here.
- The deep-synthesis mechanism map is explicitly incomplete/ exploratory in its own source record.
- The latest Aether evaluator slice is useful provenance but is not treated as proof that this smaller Proteus package has equivalent coverage.
