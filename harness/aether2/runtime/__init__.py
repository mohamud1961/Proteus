"""Aether-2 runtime namespace."""

from harness.aether2.runtime.cleanup_accounting import (
    CLEANUP_OUTCOMES,
    CleanupAccounting,
    ResourceCleanupRecord,
    account_for_cleanup,
    classify_unowned_state,
)
from harness.aether2.runtime.compactor import build_fact_ledger, rebase, should_rebase
from harness.aether2.runtime.bridge_harbor import (
    HarborRuntime,
    HarborRuntimeHandle,
    build_harbor_run_manifest,
    run_task_via_harbor,
)
from harness.aether2.runtime.harbor_backend import (
    DEFAULT_HARBOR_WORKSPACE_PROBE_CANDIDATES,
    DEFAULT_PROBE_TIMEOUT_SEC,
    HarborExecutor,
    HarborWorkspaceProbe,
    probe_harbor_workspace,
)
from harness.aether2.runtime.context import ContextManager, Prefix
from harness.aether2.runtime.executor import ContainerBackend, ContainerExecutor, RawResult
from harness.aether2.runtime.escalation import EscalationDecision, apply_escalation, decide_escalation
from harness.aether2.runtime.metrics import Scorecard, build_scorecard
from harness.aether2.runtime.jobs import JobRegistry, JobStatus
from harness.aether2.runtime.model_client import Aether2ModelClient, ModelResponse
from harness.aether2.runtime.adaptive_profile import AgentInitializationFailure
from harness.aether2.runtime.orientation import (
    ENV_CONTRACT_VERSION,
    HOME_PROBE_COMMAND,
    NETWORK_PROBE_COMMANDS,
    NPM_GLOBAL_PREFIX_PROBE_COMMAND,
    OrientationSnapshot,
    PIP_USER_BASE_PROBE_COMMAND,
    SHELL_LC_PROBE_COMMAND,
    SYSTEM_TMP_PROBE_COMMAND,
    TMPDIR_PROBE_COMMAND,
    orient,
)
from harness.aether2.runtime.prompts import (
    COMPLETION_REMINDER_INTRO,
    DOCTRINE_LINES,
    HANDOFF_TEMPLATE,
    MECHANICAL_SYSTEM_PROMPT,
    STRATEGY_RESET_REMINDER,
    SYSTEM_PROMPT,
    TASK_DONE_REMINDER,
)
from harness.aether2.runtime.verify import (
    CheckResult,
    DiscrepancyReport,
    RequirementResult,
    VERIFIER_TOOL_SCHEMAS,
    replay_checks,
    verify_fresh_context,
)
from harness.aether2.runtime.sessions import SessionRegistry
from harness.aether2.runtime.task_spec import TaskSpec

__all__ = [
    "CLEANUP_OUTCOMES",
    "CheckResult",
    "Aether2ModelClient",
    "AgentInitializationFailure",
    "COMPLETION_REMINDER_INTRO",
    "ContainerBackend",
    "ContainerExecutor",
    "CleanupAccounting",
    "ContextManager",
    "DOCTRINE_LINES",
    "ENV_CONTRACT_VERSION",
    "EscalationDecision",
    "DiscrepancyReport",
    "DEFAULT_HARBOR_WORKSPACE_PROBE_CANDIDATES",
    "DEFAULT_PROBE_TIMEOUT_SEC",
    "HarborRuntime",
    "HarborRuntimeHandle",
    "HarborExecutor",
    "HarborWorkspaceProbe",
    "HANDOFF_TEMPLATE",
    "HOME_PROBE_COMMAND",
    "JobRegistry",
    "JobStatus",
    "NETWORK_PROBE_COMMANDS",
    "NPM_GLOBAL_PREFIX_PROBE_COMMAND",
    "ModelResponse",
    "MECHANICAL_SYSTEM_PROMPT",
    "OrientationSnapshot",
    "PIP_USER_BASE_PROBE_COMMAND",
    "Prefix",
    "RawResult",
    "RequirementResult",
    "ResourceCleanupRecord",
    "Scorecard",
    "SHELL_LC_PROBE_COMMAND",
    "STRATEGY_RESET_REMINDER",
    "SYSTEM_PROMPT",
    "SYSTEM_TMP_PROBE_COMMAND",
    "SessionRegistry",
    "TASK_DONE_REMINDER",
    "TMPDIR_PROBE_COMMAND",
    "VERIFIER_TOOL_SCHEMAS",
    "account_for_cleanup",
    "apply_escalation",
    "build_fact_ledger",
    "TaskSpec",
    "build_harbor_run_manifest",
    "build_scorecard",
    "classify_unowned_state",
    "decide_escalation",
    "orient",
    "probe_harbor_workspace",
    "rebase",
    "replay_checks",
    "run_task_via_harbor",
    "should_rebase",
    "verify_fresh_context",
]
