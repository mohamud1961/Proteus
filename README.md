# Proteus

Proteus is a task-conditioned agent compiler: an Architect turns task truth into a bounded workbench, a Solver operates through that workbench, and an independent Verifier checks the resulting workspace. The runtime records action receipts and evidence so a judge can inspect what actually happened.

Proteus is the public Build Week product derived from the longer-running Aether research project. Aether is the history and source of the execution, evidence, evaluation, and role-separation ideas; Proteus is the portable package and demo surface.

## Run it from a fresh clone

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
./scripts/run_demo.sh
```

The demo is deterministic replay by design. It creates a temporary workbench, executes real file and subprocess actions, writes an append-only evidence ledger, and runs an independent verifier against the saved workspace. It does not require an API key, Docker, or a paid model run.

## What a judge should inspect

1. Run `./scripts/run_demo.sh` and follow the printed `workbench`, `actions`, `evidence_path`, and `verification` fields.
2. Read [the architecture](docs/architecture.md) to see which layer owns each decision.
3. Read [the evaluation contract](docs/evaluation.md) and run `./scripts/run_evals.sh` to see a passing case and a known-bad case.
4. Read [the Build Week timeline](docs/build-week.md) and [the project history](docs/project-history.md) for evidence-backed provenance.

## GPT-5.6 and Codex

The optional live provider boundary is `proteus.providers.OpenAIProvider`, configured for `gpt-5.6` and guarded by `OPENAI_API_KEY`. The checked-in judge path is replay-only so the core claim is reproducible. Build Week work was performed with Codex/GPT-5.6-assisted repository investigation, implementation, evaluation design, and review; the exact historical boundary is documented rather than inferred from generated prose.

## Limitations

Proteus is not claiming that the replay demo measures frontier-model capability. It demonstrates the execution and verification substrate. Live-provider scoring, broad benchmark promotion, and production hardening remain follow-up work. See [limitations](docs/limitations.md) and [verified results](submission/VERIFIED_RESULTS.md).

## License and source boundary

The public package contains no private benchmark tasks, credentials, or VM artifacts. See [the source manifest](submission/SOURCE_MANIFEST.json) and [source authority](submission/SOURCE_AUTHORITY.md).
