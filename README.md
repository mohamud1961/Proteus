# Proteus

> **A task-conditioned agent compiler that lets frontier models design the workbench they need for each terminal task.**

Most AI agents use a workflow designed in advance: fixed prompts, fixed tools, fixed memory, fixed verification, and the same control loop for every problem.

Proteus starts from a different thesis:

**Models are becoming capable enough to help design how they should work.**

Given a task, Proteus can synthesize a configurable workbench describing the strategy, prompts, action surface, context policy, evidence requirements, verification routes, and success criteria for that objective. A Solver operates through the compiled workbench, while an independent Verifier checks the resulting environment using fresh evidence rather than trusting the Solver's claim of success.

The model designs task-specific policy. Proteus keeps control of the trusted mechanisms: execution boundaries, receipts, state, provenance, and fail-closed verification.

Think of it as **skills for agent architectures**: not merely adding another tool, but configuring the workflow, context, memory, and verification strategy required by the task.

---

## Why Proteus

Terminal tasks vary dramatically. Repairing a repository, configuring a service, validating an artifact, inspecting a long-running process, and analysing media should not all be forced through one generic prompt-and-tool loop.

Proteus explores a more adaptable architecture:

```text
Task
  |
  v
Architect --------> task-specific Workbench
                         |
                         v
                      Compiler
                         |
                         v
Solver -----------> Trusted Runtime -----------> Evidence Ledger
                         |                              |
                         +------------------------------+
                                        |
                                        v
                              Independent Verifier
                                        |
                              complete / continue / block
```

The long-term goal is a general model-led runtime capable of approaching unfamiliar terminal tasks by constructing the right agent configuration for each one.

## Core design

### Architect

The Architect converts task truth into a bounded workbench. The workbench can define:

- Solver instructions and workflow;
- tools and capabilities;
- context and memory policy;
- evidence requirements;
- verification routes;
- success criteria;
- false-success risks;
- recovery and reconfiguration behaviour.

### Compiler and trusted runtime

The Compiler turns that policy into a concrete execution boundary. The runtime—not the model—owns:

- workspace and path containment;
- file and subprocess actions;
- process and service state;
- action receipts;
- durable evidence;
- bounded retries and timeouts;
- finalisation behaviour.

### Solver

The Solver proposes actions through the compiled workbench. It must observe action results before continuing and cannot treat its own output as proof that the task is complete.

### Independent Verifier

The Verifier reads the durable workspace and performs its own checks. It can accept completion, return findings for repair, or block when the environment cannot establish the required evidence.

Read the full [architecture guide](docs/architecture.md).

---

## Quick start

Proteus requires Python 3.11 or newer.

```bash
git clone https://github.com/mohamud1961/Proteus.git
cd Proteus

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

./scripts/run_demo.sh
```

The default demo is a deterministic local replay and requires no API key, Docker installation, paid model call, or private benchmark data.

It creates a temporary workbench, performs real file and subprocess actions, writes an append-only JSONL evidence ledger, and asks an independent verifier to inspect the saved workspace.

You can also use the CLI directly:

```bash
proteus demo
```

## Test and evaluation commands

Run the source-matched test inventory:

```bash
./scripts/run_tests.sh
```

Run the portable passing case and deliberately corrupted known-bad case:

```bash
./scripts/run_evals.sh
```

Run the deterministic manifest-driven harness certification:

```bash
./scripts/run_harness_certification.sh
```

The known-bad evaluation mutates the saved artifact after Solver completion. The independent Verifier must reject it.

See [evaluation methodology](docs/evaluation.md) and [verified package results](submission/VERIFIED_RESULTS.md).

---

## Current verified package results

The public package has been checked from a fresh clone:

- editable development installation: passed;
- replay demo with real file writes, reads, subprocess execution, JSONL evidence, and independent verification: passed;
- known-bad evaluation: rejected as expected;
- component certification board: **13/13 required cases passed**;
- tracked-source audit: **853/853 blobs verified** with no credential findings;
- source-matched smoke: **891 passed, 38 skipped, 3 failed**.

The three remaining source-matched failures are documented existing runtime regressions rather than hidden or converted into passes. They concern two Docker process-probe expectations and one verifier-evidence intervening-step expectation.

These are package and deterministic-substrate results. They are **not** presented as a live GPT-5.6 score, a Terminal-Bench score, or proof that Proteus can already solve every terminal task.

Read the complete [validation record](submission/VALIDATION.md) and [limitations](docs/limitations.md).

---

## Optional live-provider support

The reproducible judge path is replay-only. Optional provider dependencies can be installed separately:

```bash
pip install -e ".[live]"
export OPENAI_API_KEY="..."
```

The OpenAI provider boundary is available through `proteus.providers.OpenAIProvider` and is configured for GPT-5.6. Vision and full dependency groups are also available:

```bash
pip install -e ".[vision]"
pip install -e ".[full]"
```

The repository does not contain credentials, paid model outputs, VM state, or private benchmark payloads.

---

## Repository map

```text
proteus/                 Active Proteus runtime, compiler, roles, providers, and demo
runner/                  Evaluation CLI, schemas, model boundary, and substrate runner
eval_suite/              Task packs, graders, boards, and evaluation framework
tests/                   Unit, integration, and source-matched harness tests
scripts/                 Demo, test, eval, certification, and development commands
tools/                   Replay, integrity, and developer tooling
docs/                    Architecture, history, evaluation, and limitations
submission/              Source authority, validation, manifests, and verified results
older_variants/          Explicitly quarantined historical and compatibility surfaces
```

Historical Aether compatibility code remains clearly separated under `older_variants/`. The active public product lives under `proteus/`.

---

## Built with Codex and GPT-5.6

Proteus is derived from the longer-running Aether research project. During OpenAI Build Week, Codex and GPT-5.6 were used extensively as engineering partners for:

- repository investigation and source reconstruction;
- implementation and refactoring;
- provider-boundary debugging;
- Architect, Solver, and Verifier evaluation design;
- deterministic certification;
- VM execution and evidence collection;
- adversarial review and failure attribution;
- public packaging and documentation.

The human project owner directed the core thesis, architecture, acceptance criteria, evaluation strategy, promotion decisions, and final product story.

The evidence-backed chronology distinguishes pre-existing Aether foundations from Build Week additions. Read the [Build Week timeline](docs/build-week.md) and [project history](docs/project-history.md).

---

## Project status

Proteus is an active research and engineering project.

The current public release demonstrates:

- a portable task-conditioned runtime;
- explicit Architect, Solver, Compiler, Runtime, and Verifier boundaries;
- real local execution and evidence collection;
- independent verification;
- deterministic replay and known-bad evaluation;
- the broader harness, runner, evaluation substrate, and test surfaces.

Work still in progress includes broad live-model qualification, automatic workbench generation across unseen task families, controlled runtime reconfiguration, wider provider support, and large-scale Terminal-Bench evaluation.

## Roadmap

- strengthen automatic workbench synthesis;
- qualify Architect output against downstream task success;
- expand model and provider support;
- improve long-horizon context and memory policies;
- test controlled reconfiguration after evidence of no progress;
- convert historical failures into predictive offline evaluations;
- evaluate progressively on broader terminal-task boards and Terminal-Bench.

## Research lineage

Aether is the six-month research and engineering lineage behind Proteus. Proteus is the clean public product and package boundary created from that work. Internal compatibility names are retained only where required for source provenance or import stability.

See:

- [Project history](docs/project-history.md)
- [Build Week timeline](docs/build-week.md)
- [Research synthesis](submission/RESEARCH_SYNTHESIS.md)
- [Source authority](submission/SOURCE_AUTHORITY.md)

## Licence

See [LICENSE](LICENSE).

---

**Proteus asks a simple question: if models can now reason about how work should be done, why force every task through an agent architecture designed in advance?**