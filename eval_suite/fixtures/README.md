# Fixtures

Reusable task workspace fixtures and reference assets.

Fixtures that are bundled with a specific task pack live inside that pack's
`solver_pack/workspace/` directory. This top-level `fixtures/` directory
holds cross-pack or standalone fixture collections.

## What is a fixture

A fixture is a deterministic, pre-populated workspace that the eval runner
copies into the solver's sandbox before the task starts. It provides:

- Input files (config files, data files, seed repos, evidence)
- Reference artifacts (known-good outputs for visible-verifier comparison)
- Decoy files (stale docs, wrong-path files) used to test target selection

## Smoke Family Fixtures

Each smoke family with a fixture lives at:

```
families/<mechanism-family>/<family>/fixture/
  reference/   <- the expected output the grader checks against
  candidate/   <- the initial workspace the solver receives
```

Families with fixtures:
- `public_manifest_repair_smoke` — SHA-256 manifest normalization workspace
- `mcp_registry_contract_smoke` — MCP registry schema workspace
- `runtime_policy_hook_smoke` — hook policy activation workspace
- `skill_loader_contract_smoke` — skill loader contract workspace
- `subagent_handoff_contract_smoke` — handoff discipline workspace

## Task Pack Fixtures

Each pressure-family pack includes its fixtures within its `solver_pack/workspace/`
and documents them in `fixture_manifest.json`. Examples:

- `families/service/service_lifecycle_readiness_flagship/solver_pack/service/` —
  launcher, probe, cleanup, and service config files for the orchestration task
- `families/filesystem/original_repo_recovery_flagship/solver_pack/workspace/repo/` —
  checkpoint files, incident postmortem, ops chat excerpt, handoff manifest,
  runtime env files

## Adapter fixture note

The fallback fixture files for the adapter-driven benchmark lanes (sample
question sets, reference CSVs, dataset files) are not redistributed here —
they are either subject to the source benchmark's license terms or are
fetched from the live corpus at runtime.
