"""Eval-factory v0 schema helpers.

These functions keep Packet 01 contracts executable without tying them to a
specific benchmark runner or model provider.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

RUN_RECORD_VERSION = "run_record.v0"
RUN_EVENTS_VERSION = "run_events.v0"
SCORE_ENVELOPE_VERSION = "score_envelope.v0"

LAYER_IDS = (
    "L0_inline_assertion",
    "L1_verifier_artifact",
    "L2_replay_or_state_grader",
    "L3_judge_layer",
    "L4_final_acceptance",
)
LAYER_STATUSES = ("pass", "fail", "unavailable", "not_applicable")
FINAL_VERDICTS = ("pass", "fail", "unresolved", "blocked_non_promotable")
PROVIDER_ROUTES = (
    "openai_api",
    "openrouter",
    "local_stub",
    "none",
)
AUTH_MODES = ("api_key", "none")
PHASES = ("orient", "tool", "execute", "context", "verify", "recover", "eval")

EXECUTION_MODES = (
    "deterministic_no_model",
    "one_shot_batchable",
    "multistep_batchable",
    "sync_interactive",
    "offline_judge_batchable",
)
EVALUATION_LANES = ("guardrail_debug", "bounded_diagnostic", "promotion")
EVALUATION_LANE_ALIASES = {
    "guardrail_debug": "guardrail_debug",
    "guardrail_debug_only": "guardrail_debug",
    "bounded_diagnostic": "bounded_diagnostic",
    "bounded_diagnostic_only": "bounded_diagnostic",
    "promotion": "promotion",
    "promotion_grade": "promotion",
}
LANE_BLOCKER_CODES = (
    "forced_probe_dependency",
    "standin_dependency",
    "bounded_l3_dependency",
    "schema_missing_required_fields",
    "mechanism_visibility_incomplete",
    "lane_policy_restriction",
    "legacy_stability_lane_artifact",
    "guardrail_debug_non_promotable",
    "bounded_diagnostic_non_promotable",
)
COMPARABILITY_FIELDS = (
    "effective_settings_id",
    "invariant_fingerprint",
    "grader_version",
    "budget_used",
    "budget_cap",
    "stability_metrics_summary",
    "execution_mode",
)
GOVERNED_TERMINAL_STATUSES = (
    "tool_eval_completed",
    "tool_eval_incomplete",
    "tool_eval_execution_error",
    "not_applicable",
    "missing",
)
GOVERNED_TRUTH_COMPLETION_SCOPES = ("case_coverage_only", "not_applicable")
GOVERNED_TRUTH_AUTHORITY_COMPLETENESS = ("complete", "incomplete", "not_applicable")

PACKET03_MODEL_TIER_DEFAULT = "gpt-5.4-nano"
PACKET03_MODEL_TIER_FALLBACK = "gpt-5.4-mini"
PACKET03_MODEL_TIER_PROMOTION = "gpt-5.3-codex"
MODEL_TIER_POLICY_KEYS = ("screening_default", "screening_fallback", "promotion_tier")

REASON_CODE_FLOOR = (
    "grader_unavailable",
    "verifier_artifact_missing",
    "replay_data_gap",
    "judge_config_unpinned",
    "final_projection_fallback",
    "capture_only_non_promotable",
)
ROUTE_MANIFEST_VERSION = "packet04_route_manifest.v1"
OWNERSHIP_BUCKETS = ("support_infra", "baseline_hardening", "candidate_variant")
ROUTE_RUNTIME_KEYS = (
    "orientation",
    "tools_getter",
    "tool_executor",
    "execution",
    "context",
    "verification",
    "recovery",
    "terminal_guard",
)
SUCCESSOR_RHV1_REFERENCE_VARIANT_ID = "rhv1_ref_01"
SUCCESSOR_PRIMARY_COMPARATOR_VARIANT_ID = "spb_01"
SUCCESSOR_RHV1_OBSERVED_MARKER_IDS = (
    "environment_aware_orientation",
    "target_state_updates",
    "evidence_state_ledger_entries",
    "structured_state_context_summaries",
    "evidence_backed_completion_gate",
    "verification_before_completion_decision",
    "failure_source_typing",
)


class SchemaValidationError(ValueError):
    """Raised when an eval-factory v0 payload violates the contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaValidationError(f"{path} must be an object")
    return value


def _require_string(value: Any, path: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise SchemaValidationError(f"{path} must be a non-empty string")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaValidationError(f"{path} must be a list")
    return value


def _require_enum(value: Any, path: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise SchemaValidationError(f"{path} must be one of {allowed}")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaValidationError(f"{path} must be a boolean")
    return value


def default_score(kind: str = "categorical", value: Any = None) -> dict[str, Any]:
    return {"kind": kind, "value": value}


def default_layer(status: str = "not_applicable") -> dict[str, Any]:
    _require_enum(status, "status", LAYER_STATUSES)
    return {
        "status": status,
        "score": default_score(),
        "reason_codes": [],
        "evidence_refs": [],
    }


def default_layers() -> dict[str, dict[str, Any]]:
    layers = {layer_id: default_layer() for layer_id in LAYER_IDS}
    layers["L1_verifier_artifact"]["artifact_ref"] = None
    layers["L2_replay_or_state_grader"]["grader_id"] = None
    layers["L2_replay_or_state_grader"]["replay_data_gap_count"] = 0
    layers["L3_judge_layer"]["judge_config"] = {
        "judge_type": "none",
        "model": None,
        "prompt_fingerprint": None,
        "schema_fingerprint": None,
        "mode": None,
    }
    layers["L4_final_acceptance"]["final_gate"] = {
        "gate_type": "benchmark_assert",
        "gate_value": None,
    }
    layers["L4_final_acceptance"]["projection_fallback_reason"] = None
    return layers


def make_score_envelope(
    *,
    run_id: str,
    benchmark_id: str,
    case_id: str,
    scoring_contract_version: str = SCORE_ENVELOPE_VERSION,
    layers: dict[str, dict[str, Any]] | None = None,
    adapter: dict[str, Any] | None = None,
    final_verdict: str = "unresolved",
) -> dict[str, Any]:
    envelope = {
        "score_envelope_version": SCORE_ENVELOPE_VERSION,
        "run_id": run_id,
        "benchmark_id": benchmark_id,
        "case_id": case_id,
        "scoring_contract_version": scoring_contract_version,
        "adapter": adapter
        or {
            "adapter_id": None,
            "adapter_contract_version": None,
            "benchmark_family": None,
            "case_id": case_id,
        },
        "layers": layers or default_layers(),
        "aggregate": {
            "final_verdict": final_verdict,
            "unresolved_layers": [],
            "substitution_guard_violations": [],
            "carry_forward_warnings": [],
        },
    }
    validate_score_envelope(envelope)
    return envelope


def validate_model_route(model_route: dict[str, Any]) -> dict[str, Any]:
    route = _require_mapping(model_route, "model_route")
    for key in ("model_client_id", "provider_route", "adapter_id", "model_name"):
        _require_string(route.get(key), f"model_route.{key}")
    _require_enum(route.get("provider_route"), "model_route.provider_route", PROVIDER_ROUTES)
    _require_enum(route.get("auth_mode"), "model_route.auth_mode", AUTH_MODES)
    if "access_token" in route or "refresh_token" in route:
        raise SchemaValidationError("model_route must not contain token material")
    if not isinstance(route.get("request_settings", {}), dict):
        raise SchemaValidationError("model_route.request_settings must be an object")
    _require_string(
        route.get("request_settings_fingerprint"),
        "model_route.request_settings_fingerprint",
    )
    return route


def validate_run_header(header: dict[str, Any]) -> dict[str, Any]:
    data = _require_mapping(header, "run_header")
    for key in ("run_id", "started_at_utc", "task_id", "benchmark_family", "seed_id"):
        _require_string(data.get(key), f"run_header.{key}")
    blocks = _require_mapping(data.get("block_selection"), "run_header.block_selection")
    for key in ("orientation", "tools", "execution", "context", "verification", "recovery"):
        _require_string(blocks.get(key), f"run_header.block_selection.{key}")
    environment = _require_mapping(data.get("environment"), "run_header.environment")
    _require_string(environment.get("sandbox_type"), "run_header.environment.sandbox_type")
    _require_string(environment.get("cwd"), "run_header.environment.cwd")
    if not isinstance(environment.get("timeout_sec"), int):
        raise SchemaValidationError("run_header.environment.timeout_sec must be an int")
    validate_model_route(_require_mapping(data.get("model_route"), "run_header.model_route"))
    scoring = _require_mapping(data.get("scoring_contract"), "run_header.scoring_contract")
    _require_string(
        scoring.get("scoring_contract_version"),
        "run_header.scoring_contract.scoring_contract_version",
    )
    routed_modules = data.get("routed_modules")
    if routed_modules is not None:
        _require_list(routed_modules, "run_header.routed_modules")
        for index, entry in enumerate(routed_modules):
            validate_routed_module_entry(entry, f"run_header.routed_modules[{index}]")
        _require_string(
            data.get("route_manifest_fingerprint"),
            "run_header.route_manifest_fingerprint",
        )
        _require_string(data.get("route_manifest_ref"), "run_header.route_manifest_ref")
    if "evaluation_lane" in data:
        validate_eval_run_header_metadata(data, "run_header")
    return data


def validate_event(event: dict[str, Any]) -> dict[str, Any]:
    data = _require_mapping(event, "event")
    if not isinstance(data.get("seq"), int) or data["seq"] < 0:
        raise SchemaValidationError("event.seq must be a non-negative int")
    _require_string(data.get("ts_utc"), "event.ts_utc")
    _require_enum(data.get("phase"), "event.phase", PHASES)
    _require_string(data.get("event_type"), "event.event_type")
    payload = _require_mapping(data.get("payload"), "event.payload")
    if data["event_type"] == "eval_signal":
        signal = _require_mapping(payload.get("signal"), "event.payload.signal")
        _require_string(signal.get("signal_name"), "event.payload.signal.signal_name")
        _require_enum(signal.get("status"), "event.payload.signal.status", LAYER_STATUSES)
        _require_string(signal.get("reason_code"), "event.payload.signal.reason_code")
        _require_enum(signal.get("source_layer"), "event.payload.signal.source_layer", LAYER_IDS)
    _require_mapping(payload.get("details", {}), "event.payload.details")
    _require_list(data.get("artifact_refs", []), "event.artifact_refs")
    return data


def validate_event_sequence(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index, event in enumerate(events):
        validate_event(event)
        if event["seq"] != index:
            raise SchemaValidationError("run_events must be gap-free and zero-indexed")
    return events


def validate_layer(layer_id: str, layer: dict[str, Any]) -> dict[str, Any]:
    data = _require_mapping(layer, f"layers.{layer_id}")
    _require_enum(data.get("status"), f"layers.{layer_id}.status", LAYER_STATUSES)
    score = _require_mapping(data.get("score"), f"layers.{layer_id}.score")
    _require_enum(score.get("kind"), f"layers.{layer_id}.score.kind", ("boolean", "numeric", "categorical"))
    _require_list(data.get("reason_codes"), f"layers.{layer_id}.reason_codes")
    _require_list(data.get("evidence_refs"), f"layers.{layer_id}.evidence_refs")
    return data


def validate_score_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    data = _require_mapping(envelope, "score_envelope")
    if data.get("score_envelope_version") != SCORE_ENVELOPE_VERSION:
        raise SchemaValidationError("score_envelope_version must be score_envelope.v0")
    for key in ("run_id", "benchmark_id", "case_id", "scoring_contract_version"):
        _require_string(data.get(key), f"score_envelope.{key}")
    adapter = _require_mapping(data.get("adapter"), "score_envelope.adapter")
    for key in ("adapter_id", "adapter_contract_version", "benchmark_family", "case_id"):
        value = adapter.get(key)
        if value is not None:
            _require_string(value, f"score_envelope.adapter.{key}")
    layers = _require_mapping(data.get("layers"), "score_envelope.layers")
    if set(layers) != set(LAYER_IDS):
        raise SchemaValidationError("score_envelope.layers must contain exactly L0-L4")
    for layer_id in LAYER_IDS:
        validate_layer(layer_id, layers[layer_id])
    aggregate = _require_mapping(data.get("aggregate"), "score_envelope.aggregate")
    _require_enum(
        aggregate.get("final_verdict"),
        "score_envelope.aggregate.final_verdict",
        FINAL_VERDICTS,
    )
    _require_list(aggregate.get("unresolved_layers"), "score_envelope.aggregate.unresolved_layers")
    _require_list(
        aggregate.get("substitution_guard_violations"),
        "score_envelope.aggregate.substitution_guard_violations",
    )
    _require_list(
        aggregate.get("carry_forward_warnings"),
        "score_envelope.aggregate.carry_forward_warnings",
    )
    return data


def clone_score_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(validate_score_envelope(envelope))


def validate_execution_mode(value: Any, path: str = "execution_mode") -> str:
    return _require_enum(value, path, EXECUTION_MODES)


def normalize_batch_eligibility(value: Any, path: str = "batch_eligibility") -> bool:
    return _require_bool(value, path)


def validate_evaluation_lane(value: Any, path: str = "evaluation_lane") -> str:
    if not isinstance(value, str) or not value:
        raise SchemaValidationError(f"{path} must be one of {tuple(EVALUATION_LANE_ALIASES)}")
    normalized = EVALUATION_LANE_ALIASES.get(value)
    if normalized is None:
        raise SchemaValidationError(f"{path} must be one of {tuple(EVALUATION_LANE_ALIASES)}")
    return normalized


def validate_governed_terminal_status(value: Any, path: str = "governed_terminal_status") -> str:
    return _require_enum(value, path, GOVERNED_TERMINAL_STATUSES)


def validate_lane_blocker_codes(value: Any, path: str = "lane_blocker_codes") -> list[str]:
    blockers = _require_list(value, path)
    normalized: list[str] = []
    for index, blocker in enumerate(blockers):
        code = _require_string(blocker, f"{path}[{index}]")
        _require_enum(code, f"{path}[{index}]", LANE_BLOCKER_CODES)
        normalized.append(code)
    return normalized


def validate_eval_run_header_metadata(header: dict[str, Any], path: str = "run_header") -> dict[str, Any]:
    data = _require_mapping(header, path)
    for key in ("batch_id", "eval_id", "variant_id", "route_id", "route_contract_id", "run_fingerprint"):
        _require_string(data.get(key), f"{path}.{key}")
    rerun_index = data.get("rerun_index")
    if not isinstance(rerun_index, int) or rerun_index < 0:
        raise SchemaValidationError(f"{path}.rerun_index must be a non-negative int")
    lane = validate_evaluation_lane(data.get("evaluation_lane"), f"{path}.evaluation_lane")
    promotion_authority = _require_bool(data.get("promotion_authority"), f"{path}.promotion_authority")
    if promotion_authority != (lane == "promotion"):
        raise SchemaValidationError(f"{path}.promotion_authority must match evaluation_lane={lane}")
    routed_module_paths = _require_list(data.get("routed_module_paths"), f"{path}.routed_module_paths")
    if not routed_module_paths:
        raise SchemaValidationError(f"{path}.routed_module_paths must contain at least one entry")
    for index, module_path in enumerate(routed_module_paths):
        _require_string(module_path, f"{path}.routed_module_paths[{index}]")
    return data


def packet03_default_model_tier_ladder(execution_mode: str) -> dict[str, str]:
    mode = validate_execution_mode(execution_mode)
    if mode == "deterministic_no_model":
        return {
            "screening_default": "no_model",
            "screening_fallback": "not_applicable",
            "promotion_tier": "not_applicable",
        }
    return {
        "screening_default": PACKET03_MODEL_TIER_DEFAULT,
        "screening_fallback": PACKET03_MODEL_TIER_FALLBACK,
        "promotion_tier": PACKET03_MODEL_TIER_PROMOTION,
    }


def normalize_model_tier_policy(value: Any, *, execution_mode: str) -> dict[str, str]:
    mode = validate_execution_mode(execution_mode)
    if value is None:
        return packet03_default_model_tier_ladder(mode)
    policy = _require_mapping(value, "model_tier_policy")
    if set(policy) != set(MODEL_TIER_POLICY_KEYS):
        raise SchemaValidationError(
            "model_tier_policy must contain exactly screening_default, screening_fallback, promotion_tier"
        )
    normalized = {
        "screening_default": _require_string(policy.get("screening_default"), "model_tier_policy.screening_default"),
        "screening_fallback": _require_string(
            policy.get("screening_fallback"), "model_tier_policy.screening_fallback"
        ),
        "promotion_tier": _require_string(policy.get("promotion_tier"), "model_tier_policy.promotion_tier"),
    }
    expected = packet03_default_model_tier_ladder(mode)
    if normalized != expected:
        raise SchemaValidationError(
            f"model_tier_policy must match Packet 03 canonical ladder for mode={mode}: {expected}"
        )
    return normalized


def validate_comparability_fields(record: dict[str, Any], path: str = "result_record") -> dict[str, Any]:
    data = _require_mapping(record, path)
    for field in COMPARABILITY_FIELDS:
        if field not in data:
            raise SchemaValidationError(f"{path}.{field} is required")
    _require_string(data["effective_settings_id"], f"{path}.effective_settings_id")
    _require_string(data["invariant_fingerprint"], f"{path}.invariant_fingerprint")
    _require_string(data["grader_version"], f"{path}.grader_version")
    validate_execution_mode(data["execution_mode"], f"{path}.execution_mode")
    _require_mapping(data["budget_used"], f"{path}.budget_used")
    _require_mapping(data["budget_cap"], f"{path}.budget_cap")
    _require_mapping(data["stability_metrics_summary"], f"{path}.stability_metrics_summary")
    route_manifest_fingerprint = data.get("route_manifest_fingerprint")
    if route_manifest_fingerprint is not None:
        _require_string(route_manifest_fingerprint, f"{path}.route_manifest_fingerprint")
    claimed_surface_fingerprints = data.get("claimed_surface_fingerprints")
    if claimed_surface_fingerprints is not None:
        _require_mapping(claimed_surface_fingerprints, f"{path}.claimed_surface_fingerprints")
    unchanged_surface_fingerprints = data.get("unchanged_surface_fingerprints")
    if unchanged_surface_fingerprints is not None:
        _require_mapping(unchanged_surface_fingerprints, f"{path}.unchanged_surface_fingerprints")
    return data


def validate_routed_module_entry(entry: dict[str, Any], path: str = "routed_module") -> dict[str, Any]:
    data = _require_mapping(entry, path)
    _require_string(data.get("variant_id"), f"{path}.variant_id")
    _require_string(data.get("runtime_key"), f"{path}.runtime_key")
    _require_enum(data.get("runtime_key"), f"{path}.runtime_key", ROUTE_RUNTIME_KEYS)
    _require_string(data.get("surface_id"), f"{path}.surface_id")
    _require_enum(data.get("ownership_bucket"), f"{path}.ownership_bucket", OWNERSHIP_BUCKETS)
    _require_string(data.get("declared_card_path"), f"{path}.declared_card_path")
    _require_string(data.get("real_file_path"), f"{path}.real_file_path")
    _require_string(data.get("module_import_path"), f"{path}.module_import_path")
    file_sha = _require_string(data.get("file_sha256"), f"{path}.file_sha256")
    if len(file_sha) != 64:
        raise SchemaValidationError(f"{path}.file_sha256 must be a 64-char sha256 hex digest")
    _require_bool(data.get("claimed_changed_surface"), f"{path}.claimed_changed_surface")
    return data


def validate_route_manifest(route_manifest: dict[str, Any]) -> dict[str, Any]:
    data = _require_mapping(route_manifest, "route_manifest")
    _require_string(data.get("variant_id"), "route_manifest.variant_id")
    _require_enum(
        data.get("route_manifest_version"),
        "route_manifest.route_manifest_version",
        (ROUTE_MANIFEST_VERSION,),
    )
    _require_string(
        data.get("route_manifest_fingerprint"),
        "route_manifest.route_manifest_fingerprint",
    )
    routed_modules = _require_list(data.get("routed_modules"), "route_manifest.routed_modules")
    if not routed_modules:
        raise SchemaValidationError("route_manifest.routed_modules must contain at least one entry")
    for index, entry in enumerate(routed_modules):
        validate_routed_module_entry(entry, f"route_manifest.routed_modules[{index}]")
    return data


def fingerprint_payload(payload: Any) -> str:
    packed = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()
