# Agents

Explicit local/fake subagent substrate for the Aether runtime.

Public modules:

- `loader.py`: deterministic markdown agent discovery and frontmatter parsing
- `task.py`: bounded worker task packets with explicit scope and ownership
- `handoff.py`: structured worker handoff records
- `runtime.py`: explicit in-process/fake execution boundary with visible
  skill/MCP resolution and no silent background swarms
