# Skills

Owned Python skill loading and execution interface for the Aether runtime.

These modules handle parsing frontmatter instructions, discovering skills in the workspace, and registering skill-based tools with the runtime.

Public modules:

- `loader.py`: ast-parsed Python skill file loading and signature discovery
- `registry.py`: MCPSkillBuilders mapping and execution target dispatch
- `invocation.py`: structured input/output wrapping for skill tools
