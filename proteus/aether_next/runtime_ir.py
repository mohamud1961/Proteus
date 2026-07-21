from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import posixpath
from typing import Any, Mapping


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)


def normalize_relpath(path: str, workspace_root: str = "") -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    root = workspace_root.rstrip("/")
    if root and raw == root:
        return "."
    if root and raw.startswith(root + "/"):
        raw = raw[len(root) + 1 :]
    if raw.startswith("/app/"):
        raw = raw[5:]
    elif raw.startswith("app/"):
        raw = raw[4:]
    elif raw in ("/app", "app"):
        return "."
    normalized = posixpath.normpath(raw)
    if normalized == "/":
        return "."
    if normalized.startswith("/"):
        normalized = normalized[1:]
    return normalized or "."


WORKFLOW_MODES = frozenset({
    "direct_build",
    "explore_first",
    "debug_repair",
    "service_stabilize",
    "artifact_extract",
    "optimize_search",
    "reverse_engineer_local",
    "long_build_bootstrap",
})

MODEL_TIERS = frozenset({"mini", "strong", "codex", "vision", "default"})


ACTION_SCHEMA: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("read_file", ("path",)),
    ("read_file_page", ("path",)),
    ("observe_batch", ("operations",)),
    ("read_output", ("handle",)),
    ("grep_output", ("handle", "pattern")),
    ("write_file", ("path", "content")),
    ("run_command", ("command",)),
    ("bootstrap_acquire", ("manager",)),
    ("launch_process", ("service_name", "command")),
    ("probe_service", ("target",)),
    ("stop_process", ("target",)),
    ("inspect_artifact", ("path", "mode")),
    ("register_candidate", ("candidate_id", "summary")),
    ("run_experiment", ("candidate_id", "command")),
    ("query_memory", ("query",)),
    ("query_artifact_history", ("path",)),
    ("inspect_diff", ("path",)),
    ("record_observation", ("observation",)),
    ("report_blocker", ("blocked_component", "observed_evidence", "attempted_actions", "why_current_tools_or_config_prevent_progress", "requested_harness_change")),
    ("inspect_checks", ()),
    ("run_check", ("check_id",)),
)

ALWAYS_AVAILABLE_ACTION_KINDS = frozenset({
    "observe_batch",
    "query_memory",
    "query_artifact_history",
    "inspect_diff",
    "record_observation",
    "report_blocker",
    "inspect_checks",
    "run_check",
    # Information-retrieval primitives: full outputs and files must always be
    # retrievable by handle regardless of the configured tool subset -- no
    # configuration may reintroduce information destruction.
    "read_output",
    "grep_output",
    "read_file_page",
})
KERNEL_INTERNAL_ACTION_KINDS = frozenset({"register_candidate", "run_experiment"})

# Only these operations may be children of one observation batch.  No shell
# command, HTTP request, check execution, process action, write, or unknown
# helper is assumed read-only merely from its name.
CERTIFIED_READ_ONLY_ACTION_KINDS = frozenset({
    "read_file",
    "read_file_page",
    "read_output",
    "grep_output",
    "query_memory",
    "query_artifact_history",
    "inspect_diff",
    "inspect_artifact",
    "inspect_checks",
    "probe_service",
})

# One generic action surface is owned by the kernel.  Architect output may
# describe workflow and evidence, but it cannot add, remove, or select these
# actions.  Keep this derived from the canonical action schema so prompts,
# compilation, and receipts cannot drift into separate tool inventories.
FIXED_KERNEL_TOOL_SURFACE: tuple[str, ...] = tuple(
    name for name, _args in ACTION_SCHEMA
    if name not in KERNEL_INTERNAL_ACTION_KINDS
)


def action_schema_for_kinds(kinds: set[str] | frozenset[str]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((name, args) for name, args in ACTION_SCHEMA if name in kinds)


@dataclass(frozen=True)
class CapabilityDescriptor:
    capability_id: str
    summary: str
    available: bool = True
    tool_names: tuple[str, ...] = ()
    cost_hint: str = "cheap"


@dataclass(frozen=True)
class EnvMap:
    task_prompt: str
    workspace_root: str
    visible_files: tuple[str, ...] = ()
    visible_dirs: tuple[str, ...] = ()
    capabilities: Mapping[str, CapabilityDescriptor] = field(default_factory=dict)
    services: Mapping[str, Any] = field(default_factory=dict)
    resource_limits: Mapping[str, Any] = field(default_factory=dict)
    permissions: Mapping[str, Any] = field(default_factory=dict)
    grader_hints: Mapping[str, Any] = field(default_factory=dict)
    interactive_features: Mapping[str, Any] = field(default_factory=dict)
    task_metadata: Mapping[str, Any] = field(default_factory=dict)
    network_scope: str = "unknown"
    file_tree: str = ""
    file_map_summary: Mapping[str, Any] = field(default_factory=dict)

    def digest(self) -> str:
        payload = {
            "workspace_root": self.workspace_root,
            "visible_files": sorted(normalize_relpath(path, self.workspace_root) for path in self.visible_files),
            "visible_dirs": sorted(normalize_relpath(path, self.workspace_root) for path in self.visible_dirs),
            "capabilities": {
                key: asdict(self.capabilities[key]) for key in sorted(self.capabilities)
            },
            "services": dict(self.services),
            "resource_limits": dict(self.resource_limits),
            "permissions": dict(self.permissions),
            "grader_hints": dict(self.grader_hints),
            "interactive_features": dict(self.interactive_features),
            "task_metadata": dict(self.task_metadata),
            "network_scope": self.network_scope,
            "file_tree": self.file_tree,
            "file_map_summary": dict(self.file_map_summary),
        }
        return sha256(stable_json(payload).encode("utf-8")).hexdigest()[:16]

    def capability_index(self) -> tuple[dict[str, Any], ...]:
        items = sorted(self.capabilities.values(), key=lambda cap: cap.capability_id)
        return tuple(
            {
                "capability_id": cap.capability_id,
                "summary": cap.summary,
                "available": cap.available,
                "cost_hint": cap.cost_hint,
                "tool_names": list(cap.tool_names),
            }
            for cap in items
        )

    def compact_summary(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root,
            "visible_file_count": len(self.visible_files),
            "visible_dir_count": len(self.visible_dirs),
            "network_scope": self.network_scope,
            "capabilities": [cap["capability_id"] for cap in self.capability_index()],
            "environment_probe_available": bool(self.task_metadata.get("environment_probe")),
            "file_tree_available": bool(self.file_tree),
        }


@dataclass(frozen=True)
class DeliverableSpec:
    path: str
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class ServiceRequirement:
    name: str
    port: int | None = None
    must_be_live: bool = True
    proof_kind: str = "probe"


@dataclass(frozen=True)
class MetricThreshold:
    name: str
    comparator: str
    target: float | int | str


@dataclass(frozen=True)
class ProofObligation:
    obligation_id: str
    kind: str
    description: str
    target: str = ""


@dataclass(frozen=True)
class ObjectiveGraph:
    deliverables: tuple[DeliverableSpec, ...] = ()
    protected_paths: tuple[str, ...] = ()
    allowed_edit_roots: tuple[str, ...] = (".",)
    service_requirements: tuple[ServiceRequirement, ...] = ()
    package_requirements: tuple[str, ...] = ()
    thresholds: tuple[MetricThreshold, ...] = ()
    output_schema: Mapping[str, str] = field(default_factory=dict)
    output_schema_target: str = ""
    obligations: tuple[ProofObligation, ...] = ()

    def summary(self) -> str:
        return stable_json(
            {
                "deliverables": [asdict(item) for item in self.deliverables],
                "protected_paths": list(self.protected_paths),
                "allowed_edit_roots": list(self.allowed_edit_roots),
                "service_requirements": [asdict(item) for item in self.service_requirements],
                "package_requirements": list(self.package_requirements),
                "thresholds": [asdict(item) for item in self.thresholds],
                "output_schema": dict(self.output_schema),
                "output_schema_target": self.output_schema_target,
            }
        )


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    label: str
    command: str
    origin: str
    authoritative: bool = True


@dataclass(frozen=True)
class EvalIndex:
    checks: tuple[CheckSpec, ...] = ()

    def authoritative_checks(self) -> tuple[CheckSpec, ...]:
        return tuple(check for check in self.checks if check.authoritative)

    def get(self, check_id: str) -> CheckSpec | None:
        for check in self.checks:
            if check.check_id == check_id:
                return check
        return None

    def summary(self) -> str:
        return stable_json([asdict(check) for check in self.checks])


@dataclass(frozen=True)
class ContextRecipeRecent:
    selector: str
    count: int


@dataclass(frozen=True)
class ContextRecipe:
    always_include: tuple[str, ...] = ()
    include_recent: tuple[ContextRecipeRecent, ...] = ()
    include_last_failure: int = 0
    preserve_exact: tuple[str, ...] = ()
    make_queryable_not_inline: tuple[str, ...] = ()
    unsupported_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPolicy:
    mode: str = "default_bounded"
    include_sections: tuple[str, ...] = (
        "open_obligations",
        "obligation_status",
        "monitor_alerts",
        "live_processes",
        "recent_progress",
        "failure_clusters",
        "artifacts_present",
        "candidate_leaderboard",
        "installed_capabilities",
        "planned_checks",
        "pending_checks",
        "command_results",
    )
    max_recent_receipts: int = 8
    max_failure_clusters: int = 4
    max_alerts: int = 4
    max_candidates: int = 3
    # Working-context view budget. 50k is a ceiling, not a target: the volatile
    # packet is uncached per step, so content still has to earn inclusion. The
    # old 8000 default silently starved 200k-class models (~4.8k tokens before
    # compression kicked in) and was not architect-overridable -- the same
    # hidden-harness-constraint class P2i fixed for wall-clock budgets.
    model_context_window_tokens: int = 50_000
    compression_trigger_ratio: float = 0.60
    recipe: ContextRecipe | None = None


AUTOMATIC_MEMORY_POLICY_MODES = frozenset({
    "off",
    "advisory",
    "require_justification",
    "soft_block_exact_repeat",
})


@dataclass(frozen=True)
class AutomaticMemoryPolicy:
    mode: str = "advisory"

    def __post_init__(self) -> None:
        if self.mode not in AUTOMATIC_MEMORY_POLICY_MODES:
            raise ValueError(f"unknown automatic memory policy mode: {self.mode}")


@dataclass(frozen=True)
class ProcessPolicy:
    mode: str = "stateless_shell"
    protect_candidates: bool = False
    require_fresh_probe: bool = False
    destructive_restart_requires_replacement: bool = True


@dataclass(frozen=True)
class HelperToolPolicy:
    allow_creation: bool = True
    require_smoke_test: bool = True
    trust_for_completion: bool = False
    task_local_dir: str = ".aether_next/tools"


@dataclass(frozen=True)
class BootstrapPolicy:
    allow_acquisition: bool = True
    allowed_managers: tuple[str, ...] = (
        "apt",
        "pip",
        "uv",
        "npm",
        "cargo",
        "opam",
        "git",
        "wget",
        "curl",
        "hf",
    )
    refresh_env_after_success: bool = True


@dataclass(frozen=True)
class CompletionPolicy:
    require_authoritative_check: bool = True
    allow_evidence_fallback: bool = True
    require_all_obligations: bool = True
    require_recent_progress: bool = True
    require_clean_integrity: bool = True


@dataclass(frozen=True)
class ModelVerifierPolicy:
    enabled: bool = True
    runs_on: tuple[str, ...] = (
        "solver_submit",
    )


@dataclass(frozen=True)
class RefusalPolicy:
    allowed_local_categories: tuple[str, ...] = ()
    forbid_external_targets: bool = True


@dataclass(frozen=True)
class WorkflowPolicy:
    mode: str = "direct_build"
    max_explore_steps: int = 3
    max_experiments: int = 5
    require_plan_before_edit: bool = False
    require_evidence_before_destructive_action: bool = True


@dataclass(frozen=True)
class ReconfigurePolicy:
    max_reconfigurations: int = 2
    allowed_owners: tuple[str, ...] = ("harness_config",)
    typed_triggers: tuple[str, ...] = (
        "missing_capability",
        "mode_mismatch",
        "no_progress",
        "service_not_ready",
        "bootstrap_required",
        "perception_required",
    )


@dataclass(frozen=True)
class RuntimeConfigIR:
    architect_summary: str
    solver_identity_prompt: str
    selected_capabilities: tuple[str, ...]
    context_policy: ContextPolicy = field(default_factory=ContextPolicy)
    automatic_memory_policy: AutomaticMemoryPolicy = field(default_factory=AutomaticMemoryPolicy)
    process_policy: ProcessPolicy = field(default_factory=ProcessPolicy)
    helper_tool_policy: HelperToolPolicy = field(default_factory=HelperToolPolicy)
    bootstrap_policy: BootstrapPolicy = field(default_factory=BootstrapPolicy)
    completion_policy: CompletionPolicy = field(default_factory=CompletionPolicy)
    model_verifier_policy: ModelVerifierPolicy = field(default_factory=ModelVerifierPolicy)
    refusal_policy: RefusalPolicy = field(default_factory=RefusalPolicy)
    reconfigure_policy: ReconfigurePolicy = field(default_factory=ReconfigurePolicy)
    workflow_policy: WorkflowPolicy = field(default_factory=WorkflowPolicy)
    architect_model_tier: str = "strong"
    solver_model_tier: str = "mini"
    verifier_model_tier: str = "mini"
    perception_model_tier: str = "vision"
    escalation_triggers: tuple[str, ...] = ()
    inspection_plan: tuple[str, ...] = ()
    proof_plan: tuple[str, ...] = ()
    check_plan: tuple[str, ...] = ()
    compiler_injected_checks: tuple[CheckSpec, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    advisory_notes: tuple[str, ...] = ()
    success_definition: str = ""
    local_verification_limits: tuple[str, ...] = ()
    verifier_identity_prompt: str = ""
    evidence_requirements: tuple[str, ...] = ()
    false_positive_risks: tuple[str, ...] = ()
    minimum_completion_evidence: tuple[str, ...] = ()
    # Architect-flagged claims that are machine-re-derivable (counts, frame
    # indices, decoded/parsed values, hashes): when non-empty, the runtime
    # requires a completed verdict to cite at least one inspection_ref that
    # resolves to an independent-derivation inspection kind (overlay
    # execution, a live probe, or the verifier's own perception), not only a
    # read of a solver-produced artifact. Optional; empty means unflagged
    # (unchanged legacy behavior).
    re_derivable_claims: tuple[str, ...] = ()
    # Structured semantic evidence contract. Empty means legacy prose-only
    # evidence and is explicitly marked uncompiled by ConfigCompiler.
    semantic_clause_coverage: tuple[Mapping[str, Any], ...] = ()
    semantic_verifier_checks: tuple[Mapping[str, Any], ...] = ()
    semantic_false_positive_traps: tuple[str, ...] = ()

    def prompt_summary(self) -> str:
        payload = {
            "architect_summary": self.architect_summary,
            "selected_capabilities": list(self.selected_capabilities),
            "process_policy": asdict(self.process_policy),
            "completion_policy": asdict(self.completion_policy),
            "refusal_policy": asdict(self.refusal_policy),
            "inspection_plan": list(self.inspection_plan),
            "proof_plan": list(self.proof_plan),
            "check_plan": list(self.check_plan),
            "forbidden_paths": list(self.forbidden_paths),
        }
        return stable_json(payload)


@dataclass(frozen=True)
class ConfigIssue:
    code: str
    message: str
    severity: str = "error"
    fatal: bool = True


@dataclass(frozen=True)
class CompiledRuntime:
    task_prompt: str
    env_digest: str
    objective_graph: ObjectiveGraph
    eval_index: EvalIndex
    selected_capabilities: tuple[CapabilityDescriptor, ...]
    stable_prefix_sections: tuple[tuple[str, str], ...]
    context_policy: ContextPolicy
    process_policy: ProcessPolicy
    helper_tool_policy: HelperToolPolicy
    bootstrap_policy: BootstrapPolicy
    completion_policy: CompletionPolicy
    refusal_policy: RefusalPolicy
    reconfigure_policy: ReconfigurePolicy
    enforced_monitors: tuple[str, ...]
    check_plan_ids: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    automatic_memory_policy: AutomaticMemoryPolicy = field(default_factory=AutomaticMemoryPolicy)
    model_verifier_policy: ModelVerifierPolicy = field(default_factory=ModelVerifierPolicy)
    workflow_policy: WorkflowPolicy = field(default_factory=WorkflowPolicy)
    architect_model_tier: str = "strong"
    solver_model_tier: str = "mini"
    verifier_model_tier: str = "mini"
    perception_model_tier: str = "vision"
    action_schema: tuple[tuple[str, tuple[str, ...]], ...] = ACTION_SCHEMA
    solver_identity_prompt: str = ""
    success_definition: str = ""
    local_verification_limits: tuple[str, ...] = ()
    verifier_identity_prompt: str = ""
    evidence_requirements: tuple[str, ...] = ()
    false_positive_risks: tuple[str, ...] = ()
    minimum_completion_evidence: tuple[str, ...] = ()
    re_derivable_claims: tuple[str, ...] = ()
    proof_contract: tuple[Mapping[str, Any], ...] = ()
    config_realization: Mapping[str, Any] = field(default_factory=dict)

    def prefix_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": f"[{section_name}]\n{section_body}"}
            for section_name, section_body in self.stable_prefix_sections
        ]

    def selected_capability_ids(self) -> set[str]:
        return {cap.capability_id for cap in self.selected_capabilities}

    def planned_checks(self) -> tuple[CheckSpec, ...]:
        ordered: list[CheckSpec] = []
        for check_id in self.check_plan_ids:
            check = self.eval_index.get(check_id)
            if check is not None:
                ordered.append(check)
        return tuple(ordered)


@dataclass(frozen=True)
class ActionRequest:
    action_id: str
    kind: str
    capability_id: str
    arguments: Mapping[str, Any]
    intent: str
    expected_observation: str
    if_fail_next: str
    candidate_id: str = ""
    track_as_proof: bool = False
    target: Mapping[str, Any] = field(default_factory=dict)

    def validate(self, action_schema: tuple[tuple[str, tuple[str, ...]], ...] = ACTION_SCHEMA) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.action_id.strip():
            errors.append("action_id is required")
        required = dict(action_schema).get(self.kind)
        if required is None:
            errors.append(f"unknown action kind: {self.kind}")
        else:
            missing = [name for name in required if name not in self.arguments]
            if missing:
                errors.append(f"{self.kind} missing required arguments: {', '.join(missing)}")
        # capability_id / intent / expected_observation / if_fail_next are
        # audit metadata, not dispatch inputs.  Demanding boilerplate prose per
        # action only burns turns on protocol retries (observed live: dozens of
        # wasted solver turns per run); missing values default to empty and the
        # receipts still record exactly what happened.
        return tuple(errors)


@dataclass(frozen=True)
class SolverTurn:
    kind: str
    summary: str
    actions: tuple[ActionRequest, ...] = ()
    requested_check_ids: tuple[str, ...] = ()
    claimed_artifacts: tuple[str, ...] = ()
    evidence_gap: str = ""

    def validate(self, action_schema: tuple[tuple[str, tuple[str, ...]], ...] = ACTION_SCHEMA) -> tuple[str, ...]:
        errors: list[str] = []
        if self.kind not in {"act", "submit_outcome"}:
            errors.append(f"unknown turn kind: {self.kind}")
        if not self.summary.strip():
            errors.append("summary is required")
        if self.kind == "act" and len(self.actions) != 1:
            errors.append("act turns require exactly one action frontier")
        if self.kind != "act" and self.actions:
            errors.append(f"{self.kind} turns may not carry actions")
        for action in self.actions:
            errors.extend(action.validate(action_schema))
        return tuple(errors)
