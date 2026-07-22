from __future__ import annotations

from dataclasses import dataclass

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.completion import CompletionGate
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.proof_contract import record_clause_evidence
from aether_next.runtime_ir import (
    CapabilityDescriptor,
    CompletionPolicy,
    EnvMap,
    RuntimeConfigIR,
)


@dataclass
class ProtocolCandidate:
    request_fields: tuple[str, ...]
    state: dict[str, int]
    live: bool = True

    def health(self) -> bool:
        return self.live

    def set_value(self, request: dict[str, object]) -> None:
        if set(request) != set(self.request_fields):
            missing = sorted(set(self.request_fields) - set(request))
            unexpected = sorted(set(request) - set(self.request_fields))
            raise ValueError(f"request schema mismatch missing={missing} unexpected={unexpected}")
        self.state[str(request["key"])] = int(request["value"])

    def get_value(self, key: str) -> int:
        return self.state[key]


@dataclass(frozen=True)
class ProtocolGrade:
    health_passed: bool
    exact_schema_passed: bool
    round_trip_passed: bool
    detail: str

    @property
    def passed(self) -> bool:
        return self.health_passed and self.exact_schema_passed and self.round_trip_passed


def independent_protocol_client(candidate: ProtocolCandidate) -> ProtocolGrade:
    health = candidate.health()
    expected_fields = {"key", "value"}
    schema = set(candidate.request_fields) == expected_fields
    round_trip = False
    detail = ""
    try:
        candidate.set_value({"key": "alpha", "value": 7})
        round_trip = candidate.get_value("alpha") == 7
    except Exception as exc:
        detail = str(exc)
    if not detail:
        detail = f"health={health} schema={schema} round_trip={round_trip}"
    return ProtocolGrade(health, schema, round_trip, detail)


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Implement a local key/value protocol with request fields key and value.",
        workspace_root="/app",
        capabilities={
            "shell": CapabilityDescriptor("shell", "commands", tool_names=("run_command",)),
            "filesystem": CapabilityDescriptor(
                "filesystem", "files", tool_names=("read_file", "write_file")
            ),
        },
    )


def _policy() -> CompletionPolicy:
    return CompletionPolicy(
        require_authoritative_check=False,
        allow_evidence_fallback=True,
        require_all_obligations=False,
        require_recent_progress=False,
        require_clean_integrity=True,
    )


def _runtime(*, certified: bool) -> RuntimeConfigIR:
    kwargs = {}
    if certified:
        kwargs = {
            "semantic_clause_coverage": ({
                "clause_id": "protocol:set-get",
                "solver_handling": "implement exact key/value request and round trip",
                "verifier_check": "independent client sets key/value and reads the same value",
            },),
            "semantic_verifier_checks": ({
                "clause_id": "protocol:set-get",
                "inspection_route": "overlay_run_command:independent_protocol_client",
                "fallback_route": None,
                "falsification_check": "required key/value request is rejected or round trip differs",
                "required_evidence_class": "exact_contract",
            },),
            "semantic_false_positive_traps": (
                "process live or port open does not prove the request schema",
                "a client generated from the candidate schema is not independent",
            ),
        }
    return RuntimeConfigIR(
        architect_summary="Implement and prove the public key/value protocol.",
        solver_identity_prompt="Use the exact public contract.",
        selected_capabilities=("shell", "filesystem"),
        completion_policy=_policy(),
        inspection_plan=("inspect the public request contract",),
        proof_plan=("run an independently specified client",),
        **kwargs,
    )


def _compile(*, certified: bool):
    env = _env()
    return ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(
        _runtime(certified=certified), env
    )


def test_baseline_proxy_only_runtime_can_false_clean_known_bad_candidate() -> None:
    compiled = _compile(certified=False)
    candidate = ProtocolCandidate(("key", "val"), {})
    assert candidate.health() is True
    assert independent_protocol_client(candidate).passed is False

    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt(
        "health", 1, "service_probe", True, "service is live",
        payload={"service_name": "kv", "live": True},
    ))
    decision = CompletionGate().evaluate(compiled, ledger, [])
    assert decision.ready is True


def test_certified_contract_blocks_known_bad_candidate_despite_live_service() -> None:
    compiled = _compile(certified=True)
    candidate = ProtocolCandidate(("key", "val"), {})
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    record_clause_evidence(
        ledger,
        receipt_id="health",
        step=1,
        clause_id="protocol:set-get",
        route="probe_port:5000",
        evidence_class="metadata_proxy",
        provenance="independent_interface_probe",
        supports_clause=True,
        observation="service is live and accepts TCP connections",
    )
    grade = independent_protocol_client(candidate)
    assert grade.passed is False
    record_clause_evidence(
        ledger,
        receipt_id="independent-fail",
        step=2,
        clause_id="protocol:set-get",
        route="overlay_run_command:independent_protocol_client",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=False,
        observation=grade.detail,
    )
    decision = CompletionGate().evaluate(compiled, ledger, [])
    assert decision.ready is False
    assert any(blocker.code == "clause_disproved" for blocker in decision.blockers)


def test_repair_then_independent_recheck_clears_exact_clause() -> None:
    compiled = _compile(certified=True)
    candidate = ProtocolCandidate(("key", "val"), {})
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    first = independent_protocol_client(candidate)
    record_clause_evidence(
        ledger,
        receipt_id="finding",
        step=1,
        clause_id="protocol:set-get",
        route="overlay_run_command:independent_protocol_client",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=False,
        observation=first.detail,
    )
    assert CompletionGate().evaluate(compiled, ledger, []).ready is False

    candidate.request_fields = ("key", "value")
    second = independent_protocol_client(candidate)
    assert second.passed is True
    record_clause_evidence(
        ledger,
        receipt_id="recheck-pass",
        step=2,
        clause_id="protocol:set-get",
        route="overlay_run_command:independent_protocol_client",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=True,
        observation=second.detail,
    )
    decision = CompletionGate().evaluate(compiled, ledger, [])
    assert decision.ready is True


def test_known_good_candidate_completes_without_false_block() -> None:
    compiled = _compile(certified=True)
    candidate = ProtocolCandidate(("key", "value"), {})
    grade = independent_protocol_client(candidate)
    assert grade.passed is True
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    record_clause_evidence(
        ledger,
        receipt_id="good-client",
        step=1,
        clause_id="protocol:set-get",
        route="overlay_run_command:independent_protocol_client",
        evidence_class="exact_contract",
        provenance="verifier_inspection",
        supports_clause=True,
        observation=grade.detail,
    )
    assert CompletionGate().evaluate(compiled, ledger, []).ready is True
