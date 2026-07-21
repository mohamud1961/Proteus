# Validation record

Validation was run from a clean temporary clone of the one-commit Proteus origin on branch `codex/proteus-hackathon-submission`.

## Fresh install

```text
python3 -m venv /private/tmp/proteus-venv
/private/tmp/proteus-venv/bin/python -m pip install -e ".[dev]"
```

Result: install completed successfully.

## Tests and evals

```text
/private/tmp/proteus-venv/bin/python -m pytest -q
5 passed in 0.17s

/private/tmp/proteus-venv/bin/python -m evals.run_evals
baseline: passed=True expected=True
known_bad: passed=False expected=False
```

## Demo

```text
/private/tmp/proteus-venv/bin/python -m proteus.cli demo --output-dir /private/tmp/proteus-fresh-demo
```

Result: exit 0. The result included three successful Solver action receipts (write, read, subprocess), a retained JSONL evidence ledger, and an independent Verifier result with all checks passing.

## Integrity checks

- `git diff --check`: passed.
- No API key values, private benchmark payloads, Azure VM artifacts, or legacy stub demo output were copied into the package.
- The known-bad mutation changes the saved artifact after Solver completion; the independent Verifier rejects it.

These checks establish package readiness only. They do not establish live-model performance, a TerminalBench score, or a Devpost submission after the published deadline.
