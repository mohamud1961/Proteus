# Homolog Contract Smoke

This is a public-safe smoke eval family distilled from the private G2 homolog
shape.

It keeps the engineering pressure of the original family without copying
private fixtures, raw runs, or hidden verifiers.

The family spans five small contract shapes:

- file artifact creation;
- detached service survival;
- interactive session handling;
- package installation;
- long-running job completion.

The checked-in artifacts show how HarnessEng packages a realistic but
public-safe cross-surface smoke family. They are diagnostic evidence, not
benchmark evidence.

## Public Artifacts

- `task_pack.json`: family-level task contract and sanitized task rows.
- `public_homolog_contract_smoke_contract.md`: schema reference for the pack,
  board, and scoreboard shapes.
- `eval_suite/boards/homolog_contract_smoke_v1.json`: board manifest for the
  public family example.
- `eval_suite/scoreboards/homolog_contract_smoke_v1.example.scoreboard.json`:
  example scoreboard with mixed pass, fail, and invalid rows.
