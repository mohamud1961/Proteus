from __future__ import annotations

from harness.aether2.runtime.prompts import MECHANICAL_SYSTEM_PROMPT, SYSTEM_PROMPT, TASK_DONE_REMINDER
from harness.aether2.tools.native import TOOL_SCHEMAS


def test_task_done_reminder_requires_exact_artifact_self_check() -> None:
    assert "literal bytes" in TASK_DONE_REMINDER
    assert "ordering" in TASK_DONE_REMINDER
    assert "precision" in TASK_DONE_REMINDER
    assert "command string" in TASK_DONE_REMINDER
    assert "pytest-style test file" in TASK_DONE_REMINDER
    assert "runs no tests" in TASK_DONE_REMINDER
    assert TASK_DONE_REMINDER in SYSTEM_PROMPT


def test_system_prompt_uses_grader_evaluates_wording() -> None:
    assert "official grader evaluates after the agent terminates" in SYSTEM_PROMPT
    assert "grader decides" not in SYSTEM_PROMPT.lower()


def test_mechanical_system_prompt_is_not_the_static_behavioral_prompt() -> None:
    assert MECHANICAL_SYSTEM_PROMPT != SYSTEM_PROMPT
    assert "mechanical contract only" in MECHANICAL_SYSTEM_PROMPT
    assert "Default working loop" not in MECHANICAL_SYSTEM_PROMPT
    assert "task-specific strategy" in MECHANICAL_SYSTEM_PROMPT.lower()
    assert "must come from the architect-owned workbench prompt" in MECHANICAL_SYSTEM_PROMPT
    assert "official grader evaluates after the agent terminates" in MECHANICAL_SYSTEM_PROMPT


def test_system_prompt_rejects_plain_script_test_false_evidence() -> None:
    assert "runner that actually collects and executes the tests" in SYSTEM_PROMPT
    assert "plain script is weak evidence" in SYSTEM_PROMPT


def test_interactive_tool_contract_distinguishes_jobs_from_sessions() -> None:
    schemas_by_name = {
        schema["function"]["name"]: schema["function"]["description"]
        for schema in TOOL_SCHEMAS
    }

    assert "session_send/session_read cannot attach to a start_job process" in schemas_by_name["start_job"]
    assert "does not attach to a job that was already started with start_job" in schemas_by_name["session_start"]
    assert "dummy shell/cat proxy" in schemas_by_name["session_start"]
    assert "session_start launches a new interactive command" in SYSTEM_PROMPT
    assert "Do not use a dummy cat/shell session as a proxy" in SYSTEM_PROMPT


def test_system_prompt_marks_all_named_harness_tools_as_non_shell_calls() -> None:
    assert "Any named harness tool in the tool schema is a harness call" in SYSTEM_PROMPT
    assert "inspect_artifact" in SYSTEM_PROMPT
    assert "Do not type those names inside run_command" in SYSTEM_PROMPT


def test_system_prompt_explains_document_inspection_modes() -> None:
    assert "inspect_artifact with mode auto, pdf, or ocr" in SYSTEM_PROMPT
    assert "use metadata only when you specifically need file type or size" in SYSTEM_PROMPT


def test_system_prompt_explains_task_local_helper_trust() -> None:
    assert "task-local helper" in SYSTEM_PROMPT
    assert "smoke-test it" in SYSTEM_PROMPT
    assert "independent evidence" in SYSTEM_PROMPT
