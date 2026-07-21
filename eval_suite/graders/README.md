# Graders

Deterministic graders and verifier helpers for this eval suite.

## Where graders live

Graders are co-located with their task packs, not centralized here. Each task
pack directory contains a `grader/grade.py` that is the authoritative grader
for that pack.

| task pack location                           | grader line count | hidden verifier? |
|----------------------------------------------|:-----------------:|:----------------:|
| `families/filesystem/public_manifest_repair_smoke/` | 116 lines | No — fully self-contained |
| `families/tooling/mcp_registry_contract_smoke/` | 77 lines | No — fully self-contained |
| `families/environment/runtime_policy_hook_smoke/` | 63 lines | No — fully self-contained |
| `families/tooling/skill_loader_contract_smoke/` | 72 lines | No — fully self-contained |
| `families/orchestration/subagent_handoff_contract_smoke/` | 68 lines | No — fully self-contained |
| `families/environment/environment_bootstrap_runner_repair/` | 28 lines | Yes — hidden verifier withheld |
| `families/service/service_lifecycle_readiness_flagship/` | 28 lines | Yes — withheld |
| `families/filesystem/filesystem_decoy_target_selection/` | 28 lines | Yes — withheld |
| `families/verification/hidden_verifier_repair/` | 28 lines | Yes — withheld |
| `families/retrieval/structured_retrieval_reduction/` | 28 lines | Yes — withheld |
| `families/filesystem/original_repo_recovery_flagship/` | 195 lines | Yes — withheld |
| `families/tooling/tool_schema_workspace_mix/` | 161 lines | Yes — withheld |
| `families/orchestration/noisy_open_workflow/` | 161 lines | Yes — withheld |
| `families/tooling/fsent_01_tool_call_composite/` | 161 lines | Yes — withheld |
| `families/environment/fsent_02_runtime_workspace_contract/` | ~160 lines | Yes — withheld |
| `families/filesystem/fsent_03_filesystem_verifier_repair/` | ~90 lines | Yes — withheld |
| `families/retrieval/fsent_04_retrieval_reduction_closure/` | ~100 lines | Yes — withheld |
| `families/orchestration/fsent_05_long_handoff_composition_smoke/` | ~90 lines | Yes — withheld |

## Smoke Family Grader Pattern

Each self-contained smoke grader:

1. Parses a JSON candidate output from a fixed path (`--candidate`)
2. Runs deterministic checks against a known fixture reference
3. Returns a `GradeResult` JSON to `--output` with `score` (0.0 or 1.0),
   `verdict` (pass/fail), `failure_class`, and `reason_codes`

The graders contain no hidden state — all check logic is visible in source.

## Hidden Verifier Pattern

Pressure-family graders split into two layers:

- **Visible layer** (in this tree): forbidden-access detection, timeout
  classification, reason-code mapping, result schema emission
- **Hidden layer** (withheld): `reviewer_pack/hidden_verifier.py` that checks
  artifact content against `hidden_truth.json`

The `grade.py` in each pressure-family pack calls
`reviewer_pack/hidden_verifier.py` at runtime via `runpy.run_path`. Without
the hidden verifier the grader will raise `FileNotFoundError`.

See `grader/README.md` inside each affected pack for details.
