# Judge demo script

1. Say: “Proteus is the public Build Week product derived from Aether. The Architect compiles task truth into a workbench; the Solver acts; the Verifier checks state independently.”
2. Run `./scripts/run_demo.sh`.
3. Point to `mode: replay` and explain that replay makes this path deterministic and credential-free.
4. Point to the `workbench` fields: task fingerprint, workspace root, action surface, deliverables, and evidence requirements.
5. Point to the three action receipts: write, read, and real subprocess execution.
6. Open the printed `evidence_path` and show `workbench_compiled`, `action_receipt`, and `verification` events.
7. Point out that the Verifier reads the saved file and reruns the command rather than trusting Solver narration.
8. Run `./scripts/run_evals.sh` and show that the known-bad mutation is rejected.
9. Close with: “The next step is live GPT-5.6 provider qualification and larger task packs; those are not hidden behind this deterministic demo.”
