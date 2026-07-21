# V5 port manifest

Source archive: `/Users/mohamud/Downloads/AETHER_NEXT_EXECUTED_UPGRADE_V5_20260711.zip`

Reference test source root:
`/private/tmp/aether_v5_20260711_read/AETHER_NEXT_EXECUTED_UPGRADE_V5_20260711/tests`

Every Python test below is copied byte-for-byte from that source root.  No
reference production module was copied into the canonical tree.

| Canonical test | Reference source | Primary imported contract | Canonical status |
|---|---|---|---|
| `test_architect_prompt.py` | `tests/test_architect_prompt.py` | `ARCHITECT_SYSTEM_PROMPT`, `architect_prompt_has_no_tool_selection_language` | Prompt constant exists in `aether_next.model_prompts`; helper and V5 wording are absent |
| `test_cache_telemetry.py` | `tests/test_cache_telemetry.py` | `build_prompt_cache_key`, `parse_provider_cache_telemetry` | No matching public functions found |
| `test_config_adversarial.py` | `tests/test_config_adversarial.py` | V5 `TaskContract`, `ConfigCompileError`, compiler | No V5 compiler/runtime API match |
| `test_config_compilation.py` | `tests/test_config_compilation.py` | `compile_workbench_config`, typed `ProcessMode`, realization report | Canonical parser is a different `HarnessConfigIR` surface |
| `test_context_cache.py` | `tests/test_context_cache.py` | `HarnessRuntime`, append-only context/cache epochs | No `HarnessRuntime` equivalent |
| `test_context_realisation.py` | `tests/test_context_realisation.py` | V5 context realization API | No direct match |
| `test_context_stress.py` | `tests/test_context_stress.py` | V5 `HarnessRuntime` stress behavior | No direct match |
| `test_cross_component_scenarios.py` | `tests/test_cross_component_scenarios.py` | V5 runtime/world/verifier bundle | No direct match |
| `test_envmap_dynamic_state.py` | `tests/test_envmap_dynamic_state.py` | `StableEnvMap`, `WorldStateDeltaError`, `HarnessRuntime` | Canonical `EnvMap` exists, but V5 world/runtime API is absent |
| `test_failure_homologs.py` | `tests/test_failure_homologs.py` | V5 process/verifier/runtime homologs | No direct match |
| `test_json_schemas.py` | `tests/test_json_schemas.py` | V5 schema serialization/runtime | Canonical schemas/API differ |
| `test_process_failure_conditions.py` | `tests/test_process_failure_conditions.py` | `ProcessRegistry`, `HarnessRuntime` | No matching registry |
| `test_process_runtime.py` | `tests/test_process_runtime.py` | `ProcessRegistry`, `ProcessState`, port helpers | No matching registry API |
| `test_receipt_persistence_and_concurrency.py` | `tests/test_receipt_persistence_and_concurrency.py` | V5 `ReceiptStore` | Canonical ledger receipts are a different API |
| `test_receipts.py` | `tests/test_receipts.py` | V5 `ReceiptStore` | No matching store module |
| `test_role_isolation_and_feedback.py` | `tests/test_role_isolation_and_feedback.py` | V5 runtime role projections/findings | No direct match |
| `test_runtime_policy_enforcement.py` | `tests/test_runtime_policy_enforcement.py` | V5 runtime/process policy | No direct match |
| `test_runtime_process_wiring.py` | `tests/test_runtime_process_wiring.py` | V5 runtime/process wiring | No direct match |
| `test_runtime_variants.py` | `tests/test_runtime_variants.py` | V5 runtime variants | No direct match |
| `test_schema_examples.py` | `tests/test_schema_examples.py` | V5 compiler/schema examples | Canonical config schema differs |
| `test_verifier_routing.py` | `tests/test_verifier_routing.py` | V5 `VerificationRouter`/outcomes | Canonical verifier protocol uses different types |
| `test_verifier_semantic_recovery.py` | `tests/test_verifier_semantic_recovery.py` | V5 semantic recovery/evidence gates | No direct match |
| `test_world_atomicity_and_handles.py` | `tests/test_world_atomicity_and_handles.py` | V5 world atomic deltas/handles | No `WorldState` equivalent |

The reference `conftest.py` is copied unchanged as well; its fixture imports
are therefore intentionally unresolved until canonical wiring is implemented.
