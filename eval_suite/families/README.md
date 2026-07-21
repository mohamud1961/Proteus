# Family-Level Evals

This directory is the family index for the public eval map.

It groups the executable public packs by family and points at the canonical
board and scoreboard artifacts for each one.

## Family Map

- `environment/`
- `filesystem/`
- `orchestration/`
- `retrieval/`
- `runtime_contract/`
- `service/`
- `tooling/`
- `verification/`

## Notes

- Each mechanism-family directory contains only existing task packs or
  verifier tasks moved from the prior public-staging tree.
- Co-located `grader.py`, `grader/grade.py`, `verifier.sh`, fixture manifests,
  and solver packs are the evidence that a leaf is a real eval rather than a
  placeholder.
