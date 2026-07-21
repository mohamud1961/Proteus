# Historical replay engine

`builder.py` builds the deterministic local replay manifest from the historical
artifacts already present in this checkout. It emits three role-separated case
classes:

* 8 Architect-only inputs;
* 6 exact Solver pre-turn checkpoints;
* 9 frozen Verifier packets.

Every case has `role_input`, `evaluator_only`, and `provenance`. The builder
rejects evaluator-only keys in role input and verifies each source SHA-256.
The generated manifest is a promotion artifact, not a claim that a model was
run. Stateful replay is marked exact only where the source contains an exact
pre-turn context or frozen packet; missing workspace/process snapshots remain
blocked or reconstructed.

Build:

```bash
PYTHONPATH=aether_next_build .venv-fix/bin/python \
  aether_next_build/replay_engine/builder.py \
  --root aether_next_build \
  --out aether_next_build/replay_engine/generated/historical_replay_manifest.json
```

Current local manifest SHA-256: `f0ab75beea9270c78a51a9e6617163d14c106a6d1086cc81291d6a90d7b7d9d6`.

The source-complete V6.1 handoff is available locally and is kept outside the
production package; its replay builder consumes the supplied historical
archives without mutating them.

The source-complete V6.1 path is `source_complete.py`. It rebuilds the
23-case manifest from the supplied `Archive.zip` and `2Archive.zip`, enforces
their expected hashes, and reproduces manifest SHA-256
`008f4fcbaf6f07cb015fb935bffbc81362c208413beb773b2a59247956bb3602`.

Promotion is separately fail-closed. Run the gate evaluator after each replay
stage; with no stage evidence it exits non-zero and writes
`NOT READY FOR UNRESTRICTED FULL RUNS`:

```bash
PYTHONPATH=aether_next_build .venv-fix/bin/python \
  aether_next_build/replay_engine/promotion.py \
  --manifest aether_next_build/replay_engine/generated/historical_replay_manifest.json \
  --out aether_next_build/replay_engine/generated/historical_replay_promotion_local.json
```
