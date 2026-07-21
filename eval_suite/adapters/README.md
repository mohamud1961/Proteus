# Benchmark Adapters

These adapters convert external benchmarks into the eval-substrate contract
(`eval_suite/schemas/eval_substrate_contracts.py`) so the harness can be scored
against them with the same grader/verifier machinery as the native eval families.
They are real, tested code (`tests/test_benchmark_adapter_*.py`).

| Benchmark | Adapter module | Maps to | Capability |
|---|---|---|---|
| BFCL (Berkeley Function-Calling Leaderboard) | `bfcl.py`, `bfcl_native.py`, `bfcl_assets.py` | `families/tooling/` | tool-call correctness |
| ContextBench | `contextbench.py`, `contextbench_native.py` | retrieval family | retrieval / context reduction |
| Letta | `letta.py`, `letta_native.py`, `letta_context_bench.py` | `families/filesystem/` | filesystem agent |
| TerminalBench | `terminalbench.py`, `terminalbench_native.py`, `terminalbench_paths.py` | `families/environment/` | terminal / environment |
| ACEBench | `acebench.py` | whole-harness | whole-harness tool use |

Each adapter provides the standard surface: validate/build task packs and result
rows against `contracts.py`, and grade candidate outputs into a `GradeResult`.

## Running a benchmark-derived eval

Running a benchmark end-to-end requires the **upstream benchmark's own dataset**,
which is **not redistributed in this repository** for licensing reasons. To run one:

1. Obtain the upstream dataset from its source project.
2. Point the adapter at it via its documented env var / path (see the adapter
   module's docstring).
3. The adapter emits eval-substrate task packs; run them with
   `python -m runner run-eval <task_pack>`.

The native families under `families/` and `whole_harness/` ship with
self-contained, **offline-runnable** packs (e.g.
`families/tooling/mcp_registry_contract_smoke`,
`whole_harness/harness_registry_smoke`) and need no external data. The benchmark
adapters are integration surfaces, not bundled datasets.
