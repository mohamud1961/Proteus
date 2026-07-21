# Validation record

Validation was run from the Proteus packaging worktree and then repeated from a fresh clone of branch `codex/proteus-hackathon-submission`. The final package dependency commits were authored and committed on July 21, 2026 at 23:59 BST.

## Fresh install

```text
python3 -m venv /private/tmp/proteus-light-venv-2
/private/tmp/proteus-light-venv-2/bin/python -m pip install -e ".[dev]"
```

The default `.[dev]` install is the lightweight deterministic path, including
the HTTP client used by the provider-boundary tests. The
optional `.[live]`, `.[vision]`, and `.[full]` extras add model-provider and
OCR/PDF dependencies without making the judge path depend on them.

## Tests and evals

```text
/private/tmp/proteus-light-venv-2/bin/python - <<'PY'
import proteus, aether_next, harness.aether2, runner.cli
import eval_suite.schemas.eval_substrate_contracts, evals.framework
print("imports: ok")
PY
imports: ok

/private/tmp/proteus-light-venv-2/bin/python -m evals.run_evals
baseline: passed=True expected=True
known_bad: passed=False expected=False

./scripts/run_tests.sh
887 passed, 38 skipped, 3 failed
```

## Demo

```text
/private/tmp/proteus-light-venv-2/bin/python -m proteus.cli demo --output-dir /private/tmp/proteus-fresh-full-demo
```

Result: exit 0. The result included three successful Solver action receipts (write, read, subprocess), a retained JSONL evidence ledger, and an independent Verifier result with all checks passing.

## Integrity checks

- `git diff --check`: passed.
- No API key values, private benchmark payloads, Azure VM artifacts, or legacy stub demo output were copied into the package.
- The known-bad mutation changes the saved artifact after Solver completion; the independent Verifier rejects it.

## Pushed-branch cold clone

The final pushed branch was cloned again from GitHub into a separate directory and repeated the editable install, import smoke, replay eval, certification board, and source-matched test command.

The exact-remote results were:

- editable `.[dev]` install: passed
- imports for `proteus`, `aether_next`, `harness.aether2`, `runner`, `eval_suite`, and `evals`: passed
- replay baseline: passed; known-bad case: rejected as expected
- component certification board: passed, all 13 required cases passed
- exact tracked-source audit: passed, 853 tracked blobs, no credential-path/content findings
- source-matched smoke: 887 passed, 38 skipped, 3 failed


The repository's Codex review helper was also invoked. Its test lane passed, but the helper could not start its bundled native review binary (`ENOENT`) on this host. That review limitation is recorded rather than presented as a clean automated review.

The full source-matched smoke run (`python3 -m pytest -q tests aether_next_build/tests --ignore=aether_next_build/tests/v5_ported`) produced 887 passed, 38 skipped, and 3 failed. The 3 failures reproduce in the authoritative Aether worktree at `tests/test_aether_next_runtime_integrity.py` and are existing runtime regressions: two Docker process-probe expectations and one verifier-evidence-intervening-step expectation. The failures are not hidden or converted into passes.

The V5 port suite remains present and intentionally exposes its documented API integration boundary when run directly. It is not included in the passing smoke count.

These checks establish package/source readiness only. They do not establish live-model performance, a TerminalBench score, or a Devpost submission after the published deadline.
