# OLD / REFERENCE ONLY: Aether-2

This directory is retained for compatibility and historical comparison. It is not the current Proteus harness. The current canonical runtime is `aether_next_build/aether_next/`; the public product facade is `proteus/`.

## Implemented Code

- `agents/`: agent definition, loader, structured task packets, and handoffs.
- `hooks/`: hook lifecycle primitives, registries, and built-in hooks.
- `skills/`: skill loader, registry, and execution invocation wrappers.
- `tools/`: tool registry, schema definitions, and dispatch handling.
- `traces/`: evidence, delta, envelope, mirror, and receipt capture.
- `runtime/`: session management, execution jobs, and client orientation.
- `control/`: loop orchestration and execution context boundary.
