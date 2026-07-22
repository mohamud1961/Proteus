# Skill Loader Contract Smoke

Public-safe smoke eval pack for the Aether skills loader and
registry slice.

The candidate workspace must produce `skill_audit.json` proving:

- skill discovery is path-based and deterministic;
- skill frontmatter metadata parses faithfully;
- duplicate same-file and same-name collisions surface explicit reason codes;
- selected skill text is rendered into a visible bounded context block;
- missing or invalid skill refs fail truthfully with reason codes;
- retained hook and MCP-linked metadata does not trigger hidden behavior;
- no hidden prompt mutation occurs outside the recorded context block.

This is a deterministic substrate smoke surface, not external-suite evidence.
