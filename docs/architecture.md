# Architecture

Proteus has four ownership boundaries:

```text
TaskSpec -> Architect -> Workbench -> Compiler -> Runtime
                                      ^           |
                                      |           v
                                  Solver ----> EvidenceLedger
                                      |           |
                                      +------> IndependentVerifier
```

The Architect may synthesize a strategy and capability contract, but it does not execute actions. The Compiler turns that contract into a concrete workspace-root boundary and fixed action surface. The Solver proposes actions. The Runtime owns path containment, subprocess execution, and receipts. The Verifier reads the durable workspace and reruns its own checks; it does not accept the Solver's success claim as proof.

## Why this shape

The Aether research record repeatedly separated execution control, completion verification, state/recovery, artifact hygiene, and role separation. The design is a compact public implementation of those boundaries, not a claim that the entire research harness has been reproduced here. See the [mechanism synthesis](../submission/RESEARCH_SYNTHESIS.md) for the evidence boundary.

## Trust boundary

Only the runtime can write action receipts. Relative paths are resolved and rejected if they escape the compiled workspace. Verification operates on current filesystem state and command output. Evidence is append-only JSONL for inspection and replay; timestamps are diagnostic and not a pass condition.

## Providers

Replay is the only required provider. The OpenAI provider is an explicit optional boundary and reports unavailable when credentials are absent. No credentials, network calls, private benchmark files, or paid model outputs are required for the judge path.
