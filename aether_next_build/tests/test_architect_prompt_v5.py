from aether_next.model_prompts import (
    ARCHITECT_SYSTEM_PROMPT,
    architect_prompt_has_no_tool_selection_language,
)


def test_architect_prompt_uses_fixed_kernel_tool_surface():
    assert architect_prompt_has_no_tool_selection_language()
    lowered = ARCHITECT_SYSTEM_PROMPT.lower()
    assert "selected_capabilities" not in lowered
    assert "enabled_tools" not in lowered
    assert "tool_policy" not in lowered
    assert "fixed trusted kernel" in lowered
    assert "kernel tool surface" in lowered
    assert "kernel owns" in lowered


def test_architect_prompt_preserves_strict_json_and_materialisable_contract():
    assert "strict JSON" in ARCHITECT_SYSTEM_PROMPT
    assert "no requested field is advisory or unsupported" in ARCHITECT_SYSTEM_PROMPT
    assert "every task clause" in ARCHITECT_SYSTEM_PROMPT
    assert "direct Verifier inspection route" in ARCHITECT_SYSTEM_PROMPT
