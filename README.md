# Proteus

Proteus is the full public harness: an Architect turns task truth into a bounded workbench, a Solver operates through that workbench, and an independent Verifier checks the resulting workspace. The runtime records action receipts and evidence so a judge can inspect what actually happened.

Proteus is the public product name for this repository and runtime. It is derived from the longer-running Aether research project; Aether remains the provenance/history label and is retained only in internal compatibility paths and source records. Proteus is the portable package and demo surface.

## Run it from a fresh clone

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
./scripts/run_tests.sh
./scripts/run_demo.sh
```

The default development install is intentionally lightweight: it covers the
deterministic runner, public eval substrate, demo, and tests without pulling
large model or OCR stacks. Install `.[full]` for the optional live-provider and
vision dependencies, or `.[live]` / `.[vision]` separately when needed.

The demo is deterministic replay by design. It creates a temporary workbench, executes real file and subprocess actions, writes an append-only evidence ledger, and runs an independent verifier against the saved workspace. It does not require an API key, Docker, or a paid model run.

## Full harness surfaces

- `aether_next_build/aether_next/` — latest canonical Proteus runtime and verifier implementation.
- `aether_next/` — source-compatible mirror of the latest canonical runtime.
- `runner/` — eval CLI, model-client boundary, schemas, and substrate execution.
- `harness/aether2/` — **OLD / REFERENCE ONLY** Aether-2 compatibility runtime.
- `aether/` — **OLD / COMPATIBILITY ONLY** import alias for the canonical runtime.
- `aether_next_build/reference_legacy/` — **OLD / QUARANTINED** reference implementations for historical replay.
- `aether_next_build/` — runner entrypoints, deterministic eval runners, role-eval scripts, and the complete source-matched test suite.
- `eval_suite/`, `evals/`, and `tests/` — public task packs, graders, schemas, boards, tests, and manifest-driven eval framework.

The source package excludes generated run directories, credentials, VM state, and official/private task payloads. Historical results remain provenance rather than silently becoming live scores. The public-facing product is Proteus; aether_next_build/aether_next/ is canonical, aether_next/ is its source-compatible mirror, and the explicitly marked old paths are retained only for compatibility and historical reference.

For the full source-matched test inventory use `./scripts/run_tests.sh`. It keeps the documented V5 integration-boundary tests visible in the tree but excludes them from the source-matched smoke command; historical replay tests skip explicitly when their external trace corpus is absent. The current source-matched Proteus baseline, synchronized to the latest clean Aether-Next source, still has three known runtime regressions, recorded in [validation](submission/VALIDATION.md).

## What a judge should inspect

1. Run `./scripts/run_demo.sh` and follow the printed `workbench`, `actions`, `evidence_path`, and `verification` fields.
2. Read [the architecture](docs/architecture.md) to see which layer owns each decision.
3. Read [the evaluation contract](docs/evaluation.md) and run `./scripts/run_evals.sh` for the portable replay case. Run `./scripts/run_harness_certification.sh` for the deterministic manifest-driven harness gates.
4. Read [the Build Week timeline](docs/build-week.md) and [the project history](docs/project-history.md) for evidence-backed provenance.

## GPT-5.6 and Codex

The optional live provider boundary is `proteus.providers.OpenAIProvider`, configured for `gpt-5.6` and guarded by `OPENAI_API_KEY`. The checked-in judge path is replay-only so the core claim is reproducible. Build Week work was performed with Codex/GPT-5.6-assisted repository investigation, implementation, evaluation design, and review; the exact historical boundary is documented rather than inferred from generated prose.

## Limitations

Proteus is not claiming that the replay demo measures frontier-model capability. It demonstrates the execution and verification substrate. Live-provider scoring, broad benchmark promotion, and production hardening remain follow-up work. See [limitations](docs/limitations.md) and [verified results](submission/VERIFIED_RESULTS.md).

## License and source boundary

The public package contains no private benchmark tasks, credentials, or VM artifacts. See [the source manifest](submission/SOURCE_MANIFEST.json) and [source authority](submission/SOURCE_AUTHORITY.md).
