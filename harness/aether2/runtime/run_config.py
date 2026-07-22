"""Typed harness configuration for Aether-2 runs.

This module is the narrow contract between task adaptation (AHP or baseline)
and the core harness. The solver loop should consume this object, not raw AHP
profile dictionaries.

Design rules:
- Baseline and AHP both compile into HarnessRunConfig.
- AHP may configure policy knobs, but must not fork the core loop.
- Hard visible requirements and inferred/watchpoint guidance remain separate.
- Unknown/unused AHP fields should be either mapped here or deleted upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

Authority = Literal["base", "hard", "inferred", "watchpoint"]


@dataclass(frozen=True)
class RequirementSpec:
    """A requirement/focus item with explicit authority."""

    text: str
    authority: Authority
    source: str = ""

    def normalized(self) -> str:
        return " ".join(str(self.text).split())

    def verifier_text(self) -> str:
        text = self.normalized()
        if not text:
            return ""
        if self.authority == "inferred":
            return f"[inferred] {text}"
        if self.authority == "watchpoint":
            return f"[watchpoint] {text}"
        return text


def _clean_items(values: Any, *, limit: int | None = None) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    out: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if text and text not in out:
            out.append(text)
        if limit is not None and len(out) >= limit:
            break
    return tuple(out)


def _tool_name(schema: Mapping[str, Any]) -> str:
    func = schema.get("function", schema)
    if not isinstance(func, Mapping):
        return ""
    return str(func.get("name", "") or "")


def tool_names_from_schemas(schemas: list[dict[str, Any]]) -> list[str]:
    return [name for schema in schemas if (name := _tool_name(schema))]


@dataclass(frozen=True)
class ToolPolicy:
    all_tool_names: tuple[str, ...]
    selected_tool_names: tuple[str, ...]
    active_tool_schemas: tuple[dict[str, Any], ...]
    mandatory_tool_names: tuple[str, ...] = ()
    reserve_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPolicy:
    extra_prefix_messages: tuple[dict[str, Any], ...] = ()
    preserve: tuple[str, ...] = ()
    deprioritise: tuple[str, ...] = ()


ALLOWED_CONTEXT_PACK_SECTIONS = frozenset({
    "success_contract",
    "task_operating_contract",
    "current_plan",
    "open_requirements",
    "recent_steps",
    "recent_failures",
    "verifier_feedback",
    "task_local_tools",
    "artifact_observations",
    "evidence_refs",
    "active_jobs",
})

INVARIANT_CONTEXT_PACK_SECTIONS = frozenset({
    "current_plan",
    "recent_steps",
    "recent_failures",
    "verifier_feedback",
    "artifact_observations",
    "evidence_refs",
})

FORBIDDEN_CONTEXT_PACK_SECTIONS = frozenset({
    "hidden_grader_refs",
    "external_history",
    "private_reasoning",
    "raw_unrestricted_transcript",
    "raw_full_transcript",
})


@dataclass(frozen=True)
class ContextPackPolicy:
    include_sections: tuple[str, ...] = (
        "success_contract",
        "task_operating_contract",
        "current_plan",
        "recent_steps",
        "recent_failures",
        "verifier_feedback",
        "task_local_tools",
        "active_jobs",
    )
    always_include: tuple[str, ...] = ("success_contract", "task_operating_contract", "current_plan", "verifier_feedback")
    exclude_sections: tuple[str, ...] = ()
    full_previous_steps: int = 4
    receipt_event_budget: int = 12
    failure_event_budget: int = 6
    tool_result_budget: int = 8
    verifier_feedback_budget: int = 3
    artifact_observation_budget: int = 5


def validate_context_pack_policy(raw: Any) -> ContextPackPolicy:
    """Clamp architect context policy to safe, model-visible sections only.

    Architect policy may prioritize context, but it cannot remove the harness
    invariant floor for recent evidence continuity.
    """
    if not isinstance(raw, Mapping):
        raw = {}

    def _sections(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        cleaned: list[str] = []
        for item in _clean_items(raw.get(name), limit=24):
            section = item.strip().lower()
            if section in FORBIDDEN_CONTEXT_PACK_SECTIONS:
                continue
            if section in ALLOWED_CONTEXT_PACK_SECTIONS and section not in cleaned:
                cleaned.append(section)
        return tuple(cleaned) or default

    def _clamped_int(name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(raw.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

    default = ContextPackPolicy()
    exclude = tuple(
        section
        for section in _sections("exclude_sections")
        if section not in INVARIANT_CONTEXT_PACK_SECTIONS
    )
    include = tuple(
        dict.fromkeys(
            [
                *(
                    section
                    for section in _sections("include_sections", default.include_sections)
                    if section not in exclude
                ),
                *sorted(INVARIANT_CONTEXT_PACK_SECTIONS),
            ]
        )
    )
    always = tuple(
        dict.fromkeys(
            [
                *(
                    section
                    for section in _sections("always_include", default.always_include)
                    if section not in exclude
                ),
                *sorted(INVARIANT_CONTEXT_PACK_SECTIONS),
            ]
        )
    )
    return ContextPackPolicy(
        include_sections=include,
        always_include=always,
        exclude_sections=exclude,
        full_previous_steps=_clamped_int("full_previous_steps", default.full_previous_steps, 2, 8),
        receipt_event_budget=_clamped_int("receipt_event_budget", default.receipt_event_budget, 6, 30),
        failure_event_budget=_clamped_int("failure_event_budget", default.failure_event_budget, 1, 12),
        tool_result_budget=_clamped_int("tool_result_budget", default.tool_result_budget, 2, 20),
        verifier_feedback_budget=_clamped_int("verifier_feedback_budget", default.verifier_feedback_budget, 1, 5),
        artifact_observation_budget=_clamped_int("artifact_observation_budget", default.artifact_observation_budget, 1, 10),
    )


@dataclass(frozen=True)
class CompletionPolicy:
    hard_requirements: tuple[str, ...] = ()
    required_final_evidence: tuple[str, ...] = ()
    weak_evidence_policy: str = (
        "task_done requires externally observable evidence, not only a solver claim."
    )


@dataclass(frozen=True)
class VerifierPolicy:
    system_prompt: str = ""
    base_requirements: tuple[str, ...] = ()
    hard_requirements: tuple[str, ...] = ()
    inferred_requirements: tuple[str, ...] = ()
    focus: tuple[str, ...] = ()
    do_not_assume: tuple[str, ...] = ()
    required_final_evidence: tuple[str, ...] = ()
    max_rounds: int = 1
    immediate_feedback_rounds: int = 1
    final_rounds: int = 1

    def stated_requirements_for_ledger(self) -> list[str]:
        """Only hard/base requirements should seed hard completion coverage."""
        out: list[str] = []
        for item in [*self.base_requirements, *self.hard_requirements]:
            text = " ".join(str(item).split())
            if text and text not in out:
                out.append(text)
        return out

    def stated_requirements_for_verifier(self) -> list[str]:
        """Authority-tagged verifier requirements, preserving order and authority."""
        out: list[str] = []
        for spec in [
            *(RequirementSpec(text=x, authority="base") for x in self.base_requirements),
            *(RequirementSpec(text=x, authority="hard") for x in self.hard_requirements),
            *(RequirementSpec(text=x, authority="inferred") for x in self.inferred_requirements),
        ]:
            text = spec.verifier_text()
            if text and text not in out:
                out.append(text)
        return out

    def render_contract_text(self, base_contract: str) -> str:
        """Render a compact verifier task contract without losing authority."""
        sections: list[str] = []
        base = str(base_contract or "").strip()
        if base:
            sections.append(base)
        if self.hard_requirements:
            sections.append("[hard_visible_requirements]\n" + "\n".join(f"- {x}" for x in self.hard_requirements))
        if self.inferred_requirements:
            sections.append("[inferred_requirements_lower_authority]\n" + "\n".join(f"- {x}" for x in self.inferred_requirements))
        if self.focus:
            sections.append("[verification_focus_not_pass_fail]\n" + "\n".join(f"- {x}" for x in self.focus))
        if self.do_not_assume:
            sections.append("[do_not_assume]\n" + "\n".join(f"- {x}" for x in self.do_not_assume))
        if self.required_final_evidence:
            sections.append("[required_final_evidence]\n" + "\n".join(f"- {x}" for x in self.required_final_evidence))
        return "\n\n".join(sections)


@dataclass(frozen=True)
class RepeatPolicy:
    guidance: str = ""
    blind_repeat_policy: str = "block_same_failed_command_without_state_change"


@dataclass(frozen=True)
class CompactionPolicy:
    preserve: tuple[str, ...] = ()
    deprioritise: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoopPolicy:
    step_cap: int = 120
    context_window_tokens: int = 128_000


@dataclass(frozen=True)
class HarnessRunConfig:
    """The typed, central run configuration consumed by the harness."""

    system_prompt: str
    task_block: str = ""
    tools: ToolPolicy = field(default_factory=lambda: ToolPolicy((), (), ()))
    context: ContextPolicy = field(default_factory=ContextPolicy)
    context_pack: ContextPackPolicy = field(default_factory=ContextPackPolicy)
    completion: CompletionPolicy = field(default_factory=CompletionPolicy)
    verifier: VerifierPolicy = field(default_factory=VerifierPolicy)
    repeat: RepeatPolicy = field(default_factory=RepeatPolicy)
    compaction: CompactionPolicy = field(default_factory=CompactionPolicy)
    loop: LoopPolicy = field(default_factory=LoopPolicy)
    initial_plan: tuple[dict[str, Any], ...] = ()
    frozen_success_contract: dict[str, Any] | None = None
    used_fallback: bool = False
    profile_result: Any | None = None
    fallback_reason: str | None = None

    @property
    def active_tool_schemas(self) -> list[dict[str, Any]]:
        return list(self.tools.active_tool_schemas)

    @property
    def selected_tool_names(self) -> list[str]:
        return list(self.tools.selected_tool_names)

    @property
    def all_tool_names(self) -> list[str]:
        return list(self.tools.all_tool_names)

    @property
    def completion_contract_items(self) -> list[str]:
        return list(self.completion.hard_requirements)

    @property
    def verifier_stated_requirements(self) -> list[str]:
        return self.verifier.stated_requirements_for_verifier()

    @property
    def verifier_focus(self) -> list[str]:
        return list(self.verifier.focus)

    @property
    def verifier_do_not_assume(self) -> list[str]:
        return list(self.verifier.do_not_assume)

    @property
    def verifier_system_prompt(self) -> str:
        return self.verifier.system_prompt

    @property
    def extra_prefix_messages(self) -> list[dict[str, Any]]:
        return list(self.context.extra_prefix_messages)


def make_harness_run_config(
    *,
    system_prompt: str,
    task_block: str = "",
    active_tool_schemas: list[dict[str, Any]],
    selected_tool_names: list[str] | None = None,
    all_tool_names: list[str] | None = None,
    mandatory_tool_names: list[str] | None = None,
    reserve_capabilities: list[str] | None = None,
    base_requirements: list[str] | None = None,
    hard_requirements: list[str] | None = None,
    inferred_requirements: list[str] | None = None,
    verifier_system_prompt: str = "",
    verifier_focus: list[str] | None = None,
    verifier_do_not_assume: list[str] | None = None,
    required_final_evidence: list[str] | tuple[str, ...] | None = None,
    verifier_max_rounds: int = 1,
    verifier_immediate_feedback_rounds: int | None = None,
    verifier_final_rounds: int | None = None,
    extra_prefix_messages: list[dict[str, Any]] | None = None,
    context_pack_policy: Any = None,
    context_preserve: list[str] | None = None,
    context_deprioritise: list[str] | None = None,
    repeat_action_guidance: str = "",
    compaction_preserve: list[str] | None = None,
    compaction_deprioritise: list[str] | None = None,
    initial_plan: list[dict[str, Any]] | None = None,
    frozen_success_contract: dict[str, Any] | None = None,
    used_fallback: bool = False,
    profile_result: Any | None = None,
    fallback_reason: str | None = None,
) -> HarnessRunConfig:
    """Factory that keeps the mapping explicit and deduplicated."""

    all_names = tuple(all_tool_names or tool_names_from_schemas(active_tool_schemas))
    selected = tuple(selected_tool_names or list(all_names))
    if not set(selected).issubset(set(all_names)):
        missing = sorted(set(selected) - set(all_names))
        raise ValueError(f"selected_tool_names must be a subset of all_tool_names: {missing}")
    selected_set = set(selected)
    filtered_schemas = tuple(
        schema for schema in active_tool_schemas if _tool_name(schema) in selected_set
    )
    required_evidence = _clean_items(required_final_evidence)
    hard = _clean_items(hard_requirements)
    max_rounds = max(1, int(verifier_max_rounds))
    immediate_seed = max_rounds if verifier_immediate_feedback_rounds is None else verifier_immediate_feedback_rounds
    final_seed = max_rounds if verifier_final_rounds is None else verifier_final_rounds
    immediate_rounds = max(1, min(int(immediate_seed), 3))
    final_rounds = max(1, min(int(final_seed), 3))
    return HarnessRunConfig(
        system_prompt=system_prompt,
        task_block=task_block,
        tools=ToolPolicy(
            all_tool_names=all_names,
            selected_tool_names=selected,
            active_tool_schemas=filtered_schemas,
            mandatory_tool_names=tuple(mandatory_tool_names or ()),
            reserve_capabilities=_clean_items(reserve_capabilities),
        ),
        context=ContextPolicy(
            extra_prefix_messages=tuple(extra_prefix_messages or ()),
            preserve=_clean_items(context_preserve),
            deprioritise=_clean_items(context_deprioritise),
        ),
        context_pack=validate_context_pack_policy(context_pack_policy),
        completion=CompletionPolicy(
            hard_requirements=hard,
            required_final_evidence=required_evidence,
        ),
        verifier=VerifierPolicy(
            system_prompt=" ".join(str(verifier_system_prompt).split()),
            base_requirements=_clean_items(base_requirements),
            hard_requirements=hard,
            inferred_requirements=_clean_items(inferred_requirements),
            focus=_clean_items(verifier_focus),
            do_not_assume=_clean_items(verifier_do_not_assume),
            required_final_evidence=required_evidence,
            max_rounds=max_rounds,
            immediate_feedback_rounds=immediate_rounds,
            final_rounds=final_rounds,
        ),
        repeat=RepeatPolicy(guidance=" ".join(str(repeat_action_guidance).split())),
        compaction=CompactionPolicy(
            preserve=_clean_items(compaction_preserve),
            deprioritise=_clean_items(compaction_deprioritise),
        ),
        initial_plan=tuple(initial_plan or ()),
        frozen_success_contract=frozen_success_contract,
        used_fallback=used_fallback,
        profile_result=profile_result,
        fallback_reason=fallback_reason,
    )


def build_baseline_run_config(
    *,
    system_prompt: str,
    base_tool_schemas: list[dict[str, Any]],
    base_stated_requirements: list[str],
) -> HarnessRunConfig:
    names = tool_names_from_schemas(base_tool_schemas)
    return make_harness_run_config(
        system_prompt=system_prompt,
        active_tool_schemas=list(base_tool_schemas),
        selected_tool_names=names,
        all_tool_names=names,
        base_requirements=list(base_stated_requirements),
    )
