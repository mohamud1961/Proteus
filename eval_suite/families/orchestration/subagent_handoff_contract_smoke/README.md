# Subagent Handoff Contract Smoke

Deterministic public smoke pack for the Aether subagent
loader + handoff slice.

This smoke checks:

- deterministic agent definition loading;
- visible skill-ref and MCP-ref resolution or failure;
- bounded worker task packet fields;
- structured worker handoff fields;
- parent-visible unresolved risks;
- no silent background execution assumption.

This is a synthetic substrate smoke only. It is not external-suite evidence.
