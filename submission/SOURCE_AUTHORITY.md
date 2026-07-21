# Source authority

The source authority for the Aether-derived design review was the clean worktree:

- repository: `harnesseng`
- worktree: clean source checkout used for package construction (not vendored)
- branch: `codex/aether-qualified-integration`
- source commit: `679e5f174b055657fc9641db2fc74af072e2a196`
- observed status: clean at audit time

Its immediate history includes the latest evaluator-fidelity change in `679e5f17`, overlay-path virtualization in `a7c87c34`, targeted role-evaluation evidence in `ec9abf70`, provider-contract qualification evidence in `3442a1f5`, and the layered certification base in `aa800976`. The current dirty checkout at `/Users/mohamud/Downloads/harnesseng` was not copied as source authority.

The source record distinguishes implementation commits from evidence-only commits and preserves negative classifications. In particular, fresh certification evidence and model-board evidence are not silently upgraded into a public product score. The Proteus package is the full public source boundary derived from that authority: it includes the Aether-Next runtime and runner, the Aether-2 compatibility harness, public eval substrate and fixtures, tests, and scripts. Private VM/model artifacts and benchmark payloads are intentionally excluded.

Authority limitations:

- Exact private VM/model artifacts are not vendored here.
- The recorded Aether source commit is provenance metadata; it is not part of the Proteus Git history and cannot be reconstructed from this repository alone.
- The deep-synthesis mechanism map is explicitly incomplete/ exploratory in its own source record.
- The latest Aether evaluator slice is useful provenance but is not treated as proof that every live-model or benchmark gate has equivalent coverage.
