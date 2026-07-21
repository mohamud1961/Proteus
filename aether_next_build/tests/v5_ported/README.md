# V5 deterministic test port (pending canonical wiring)

These files are an assertion-preserving port of the deterministic tests shipped
in `AETHER_NEXT_EXECUTED_UPGRADE_V5_20260711.zip`.  The source paths are listed
in `V5_PORT_MANIFEST.md`.  Only the tests were copied; the V5 package, wheel,
build output, caches, and `__pycache__` were intentionally not copied.

The reference tests import `aether_next_upgrade`, while the canonical runtime
is `aether_next_build/aether_next`.  The reference APIs are not currently
present under the canonical package, so these tests intentionally expose the
integration boundary as collection failures rather than adding shims or
weakening assertions.  Do not treat a missing import as a passing result.

Once each contract is implemented in the canonical modules, adapt only the
corresponding import/fixture path and run the unchanged assertions.
