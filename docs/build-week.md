# Build Week timeline

This timeline is reconstructed from the Aether ledger, commit history, and saved evaluation artifacts. It does not claim that Aether began during Build Week. Aether is the pre-existing research and engineering project; Proteus is the public package and product framing created from that work.

The source files below are ledger evidence paths in the development workspace. They are cited so the chronology can be audited; private traces, credentials, and benchmark task data are not copied into this repository.

## Before Build Week: Aether foundations

Aether already contained the runtime, verifier, evidence, evaluator, and research-synthesis work that motivated Proteus. The deep-synthesis mechanism map marks its own status honestly as an exploratory anchor rather than a completed research artifact. Its reusable anchors were terminal realism and interrupt recovery, layered completion gating, cleanup/repo hygiene, role separation, and the difference between tool-contract claims and backend reality. See `research/synthesis/mechanism-map.md` and `research/methodology/deep_synthesis_protocols/DEEP_SYNTHESIS_LANE_CLOSURE_CRITERIA.md` in the source workspace.

## Build Week evidence window

### July 14 — reconstruct and qualify the Architect path

Ledger entries show fresh-VM runner setup, exact-source hash correction, Architect progress/failure analysis, and early role-board work:

- `tracking/ledger/inbox/20260714T0125_exact_source_vm_runner_raw.md`
- `tracking/ledger/inbox/20260714T0130_exact_source_runner_hash_correction_raw.md`
- `tracking/ledger/inbox/20260714T0432Z_fd76b67_architect_high_failure_raw.md`
- `tracking/ledger/inbox/20260714T0448Z_fd76b67_architect_causal_analysis_raw.md`

Interpretation: the work was qualification and diagnosis, not a claim of a finished public product.

### July 15 — make Verifier lifecycle and authority observable

The ledger records verifier lifecycle, generation authority, deterministic runner, stage acceptance/recovery, and adversarial closeout work:

- `tracking/ledger/inbox/20260715T_verifier_lifecycle_protocol_raw.md`
- `tracking/ledger/inbox/20260715T_verifier_generation_authority_raw.md`
- `tracking/ledger/inbox/20260715T_verifier_vm_deterministic_runner_raw.md`
- `tracking/ledger/inbox/20260715T_native_verifier_stage_b_acceptance_recovery.raw.md`

Interpretation: these entries support the Verifier/evidence story; they do not by themselves establish a public benchmark score.

### July 16 — harden execution and state boundaries

The ledger includes generation-aware process identity, real-executor bridges, service reconciliation, and fail-closed behavior:

- `tracking/ledger/inbox/20260716_generation_aware_process_registry.raw.md`
- `tracking/ledger/inbox/20260716_real_executor_bridge_fail_closed.raw.md`
- `tracking/ledger/inbox/20260716_real_executor_service_bridge_promoted.raw.md`
- `tracking/ledger/inbox/20260716_service_identity_reconciliation.raw.md`

Interpretation: this is the substrate lineage behind Proteus's insistence that a receipt or green check must correspond to real state.

### July 17 — qualify Solver control and evidence transfer

The ledger records native Solver control reliability, permission behavior, budget/handle accounting, and a pass plus failure-and-fix sequence:

- `tracking/ledger/inbox/20260717_native_solver_control_reliability_v1_gold_pass.raw.md`
- `tracking/ledger/inbox/20260717_native_solver_control_reliability_v1_gold_failure_and_fix.raw.md`
- `tracking/ledger/inbox/20260717_native_solver_control_reliability_v1_permissions.raw.md`
- `tracking/ledger/inbox/20260717_native_solver_control_reliability_v1_budget_log_handle.raw.md`

Interpretation: the Solver lane was treated as an evaluated control surface, not as a prose-only role.

### July 18–20 — recovery and certification evidence

The ledger records recovery work on July 18, then the layered certification/evidence handoffs and the Aether evaluator commits on July 20:

- `tracking/ledger/inbox/20260718_69c83_gold_recovery_launch.md`
- `tracking/ledger/inbox/20260721_aa800976_fresh_vm_certification_takeover.raw.md`
- Aether commit `aa800976fe862af3cbdf3c10c13a8fd3254a1228` (`feat(aether): add layered harness certification evals`)

The source record reports deterministic certification evidence, but also preserves unresolved clean-end hygiene and non-promotable model-board classifications. Proteus therefore uses the deterministic replay path for its judge demo and does not publish those internal results as a product leaderboard.

### July 21 — package and publicize the product boundary

The source branch continued evaluator-fidelity and provider-contract work through commit `adc210c3fa339790594cd965fc25397321235055`. The current Proteus package was then constructed on its own submission branch, with a fresh install path, replay demo, source manifest, validation record, and explicit limitations.

## Post-deadline work

Any work after the Devpost submission deadline is labeled as post-deadline packaging or verification. This repository must not imply that later cleanup was part of the original submission. See [validation](../submission/VALIDATION.md) for the actual package verification timestamp and branch state.
