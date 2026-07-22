from aether_next import ARCHITECT_SYSTEM_PROMPT, architect_prompt_has_no_tool_selection_language


def test_architect_prompt_has_no_tool_selection_fields():
    assert architect_prompt_has_no_tool_selection_language()
    lowered = ARCHITECT_SYSTEM_PROMPT.lower()
    assert "enabled_tools" not in lowered
    assert "selected_capabilities" not in lowered
    assert "tool_policy" not in lowered


def test_architect_prompt_assigns_fixed_surface_to_kernel():
    assert "fixed trusted kernel" in ARCHITECT_SYSTEM_PROMPT
    assert "kernel owns" in ARCHITECT_SYSTEM_PROMPT


def test_architect_prompt_requires_materialisable_config():
    assert "no requested field is advisory or unsupported" in ARCHITECT_SYSTEM_PROMPT


def test_architect_prompt_requires_clause_and_verifier_coverage():
    assert "every task clause" in ARCHITECT_SYSTEM_PROMPT
    assert "direct Verifier inspection route" in ARCHITECT_SYSTEM_PROMPT
