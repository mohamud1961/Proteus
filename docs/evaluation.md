# Evaluation

The package has two evidence lanes:

1. `pytest -q` checks unit and integration contracts.
2. `./scripts/run_evals.sh` runs a passing replay case and a deliberately corrupted known-bad case. The bad case must fail the independent verifier.

The judge demo is a usability/capability demonstration, not a model score. It proves that a bounded task can be compiled, acted on, evidenced, and independently verified. It does not prove broad TerminalBench performance or live GPT-5.6 capability.

## Required gates

- Fresh installation from `pyproject.toml`.
- Real file writes and a real subprocess invocation in the replay path.
- A verifier check that reads the saved artifact independently.
- A known-bad mutation that fails closed.
- No credential or network requirement for the required path.

## Research-derived evaluation discipline

The source Aether workspace treats benchmark-derived and custom evals as promotion authority, with separate task contracts, fixtures, verifiers, result rows, contamination labels, and scoreboards. This public package includes a small portable slice only. The deeper historical boards remain source material and are explicitly not presented as a Proteus leaderboard.
