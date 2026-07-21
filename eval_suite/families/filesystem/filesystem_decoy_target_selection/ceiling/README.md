# Ceiling

Ceiling behavior for this composition row:

- Reads `deploy_manifest.json` to identify `services/beta` as active.
- Patches only `services/beta/config.yaml` to set `timeout: 300`.
- Leaves all decoy configs untouched.
- Runs verifier and records pass before completion.
