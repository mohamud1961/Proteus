# Evaluation

The package has two evidence lanes plus the full harness substrate:

1. `pytest -q` checks unit and integration contracts.
2. `./scripts/run_evals.sh` runs a passing replay case and a deliberately corrupted known-bad case. The bad case must fail the independent verifier.
3. `./scripts/run_harness_certification.sh` invokes the real Aether-Next manifest-driven certification runner over the checked-in deterministic cases.

The judge demo is a usability/capability demonstration, not a model score. It proves that a bounded task can be compiled, acted on, evidenced, and independently verified. It does not prove broad TerminalBench performance or live GPT-5.6 capability.

## Required gates

- Fresh installation from `pyproject.toml`.
- Real file writes and a real subprocess invocation in the replay path.
- A verifier check that reads the saved artifact independently.
- A known-bad mutation that fails closed.
- No credential or network requirement for the required path.

## Research-derived evaluation discipline

The source Aether workspace is the provenance authority; Proteus is the public product name. It treats benchmark-derived and custom evals as promotion authority, with separate task contracts, fixtures, verifiers, result rows, contamination labels, and scoreboards. This package now carries the public Proteus source for that runner, the eval suite, the runtime, and their tests. Generated run outputs and official/private task payloads remain excluded; no copied source tree is itself proof that every gate passes in every environment.

The retained V5 port tests are explicitly documented as an integration boundary and are not silently made green. The historical replay tests are collected but skipped when their external trace corpus is absent. In the source-matched smoke run, 891 tests passed and 38 were skipped; 3 source-matched runtime tests remain failing and are recorded as existing Aether regressions rather than packaging failures.
