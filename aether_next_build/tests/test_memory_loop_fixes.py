from __future__ import annotations

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.context_compiler import ContextCompiler
from aether_next.execution import MemoryExecutor
from aether_next.kernel import AetherNextKernel
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.model_hooks import parse_solver_turn
from aether_next.runtime_ir import (
    ActionRequest,
    AutomaticMemoryPolicy,
    BootstrapPolicy,
    CapabilityDescriptor,
    CompletionPolicy,
    ContextPolicy,
    EnvMap,
    HelperToolPolicy,
    RuntimeConfigIR,
    SolverTurn,
)
from aether_next.tracing import RunTrace
from aether_next.verifier_packets import build_verifier_packet


def _env() -> EnvMap:
    return EnvMap(
        task_prompt="Inspect data.ttl and write solution.sparql.",
        workspace_root="/app",
        capabilities={
            "filesystem": CapabilityDescriptor("filesystem", "Files", tool_names=("read_file", "write_file")),
        },
    )


def _runtime(*, automatic_memory_policy: AutomaticMemoryPolicy | None = None) -> RuntimeConfigIR:
    return RuntimeConfigIR(
        architect_summary="memory loop test",
        solver_identity_prompt="Inspect primary files directly first; automatic memory will surface repeat evidence.",
        selected_capabilities=("filesystem",),
        completion_policy=CompletionPolicy(require_all_obligations=False, require_recent_progress=False),
        context_policy=ContextPolicy(mode="retrieval_augmented"),
        automatic_memory_policy=automatic_memory_policy or AutomaticMemoryPolicy(),
        bootstrap_policy=BootstrapPolicy(allow_acquisition=False),
        helper_tool_policy=HelperToolPolicy(allow_creation=False),
    )


class _RepeatedMemoryHooks:
    def __init__(self) -> None:
        self.calls = 0

    def architect(self, request):
        return _runtime()

    def solve(self, messages, compiled):
        self.calls += 1
        return SolverTurn(
            kind="act",
            summary="repeat same memory query",
            actions=(
                ActionRequest("a1", "query_memory", "kernel", {"query": "data.ttl predicates"}, "memory", "prior evidence", "act"),
            ),
        )


def test_reused_action_ids_produce_unique_query_memory_receipts_and_feedback() -> None:
    trace = RunTrace()
    result = AetherNextKernel(max_steps=3).run(_env(), MemoryExecutor(files={}), _RepeatedMemoryHooks(), trace=trace)

    queries = [receipt for receipt in result.receipts if receipt.kind == "query_memory"]
    assert len(queries) == 3
    assert [receipt.receipt_id for receipt in queries] == ["step-0:a1:query", "step-1:a1:query", "step-2:a1:query"]
    assert all(receipt.payload["no_new_evidence"] is True for receipt in queries)
    assert "do not repeat the same memory query" in queries[-1].payload["guidance"]

    assert trace.steps[2]["context_seen"]["memory_loop_feedback"]["no_progress" if False else "guidance"]
    assert "Repeated query_memory calls produced no new evidence" in trace.steps[2]["context_seen"]["memory_loop_feedback"]["guidance"]


def test_latest_file_read_excerpt_is_preserved_after_direct_inspection() -> None:
    env = _env()
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(_runtime(), env)
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt(
        "step-0:r1:read",
        0,
        "read_file",
        True,
        "read data.ttl (50 bytes)",
        payload={
            "path": "data.ttl",
            "content_hash": "abc123",
            "bytes": 50,
            "excerpt": "@prefix ex: <http://e/> . ex:a ex:teaches ex:b .",
        },
    ))

    packet = ContextCompiler().compile(compiled, ledger, alerts=[])

    assert packet["latest_file_reads"][0]["path"] == "data.ttl"
    assert "ex:teaches" in packet["latest_file_reads"][0]["excerpt"]
    assert packet["automatic_memory_available"] is True
    assert packet["automatic_memory_guidance"].startswith("Memory repeat interception is automatic")


def test_query_memory_can_retrieve_read_excerpt_by_path_after_receipt_id_fix() -> None:
    ledger = ExecutionLedger()
    ledger.record(Receipt(
        "step-0:r1:read",
        0,
        "read_file",
        True,
        "read university_graph.ttl (100 bytes)",
        payload={
            "path": "university_graph.ttl",
            "content_hash": "ttlhash",
            "bytes": 100,
            "excerpt": "Professor ex:Alice ex:teaches ex:CS101 .",
        },
    ))

    hits = ledger.query_memory("what did I learn from university_graph.ttl predicates")

    assert hits
    assert hits[0]["receipt_id"] == "step-0:r1:read"
    assert hits[0]["path"] == "university_graph.ttl"
    assert "ex:teaches" in hits[0]["excerpt"]


class _RepeatedReadHooks:
    def __init__(self, *, runtime: RuntimeConfigIR | None = None, repeat_justification: str = "") -> None:
        self.calls = 0
        self.messages = []
        self.runtime = runtime or _runtime()
        self.repeat_justification = repeat_justification

    def architect(self, request):
        return self.runtime

    def solve(self, messages, compiled):
        self.calls += 1
        self.messages.append(messages)
        return SolverTurn(
            kind="act",
            summary="repeat same file read",
            actions=(
                ActionRequest(
                    "read1",
                    "read_file",
                    "filesystem",
                    {"path": "data.ttl"} | ({"repeat_justification": self.repeat_justification} if self.repeat_justification else {}),
                    "inspect data",
                    "file contents",
                    "try another file",
                    target={"type": "file", "path": "data.ttl"},
                ),
            ),
        )


def test_automatic_memory_surfaces_repeated_file_read_in_context() -> None:
    trace = RunTrace()
    hooks = _RepeatedReadHooks()
    result = AetherNextKernel(max_steps=3).run(
        _env(),
        MemoryExecutor(files={"data.ttl": "@prefix ex: <http://e/> ."}, workspace_root="/app"),
        hooks,
        trace=trace,
    )

    automatic = [receipt for receipt in result.receipts if receipt.kind == "automatic_memory"]
    assert automatic
    assert automatic[0].payload["target"]["key"] == "data.ttl"
    assert automatic[0].payload["recent_evidence"][0]["kind"] == "read_file"
    assert automatic[0].payload["repeat_justified"] is False
    assert "automatic_memory_findings" in trace.steps[2]["context_seen"]
    assert trace.steps[2]["context_seen"]["automatic_memory_findings"][0]["target"]["key"] == "data.ttl"


def test_solver_turn_parser_preserves_target_metadata() -> None:
    turn = parse_solver_turn(
        '{"kind":"act","summary":"read","actions":[{"action_id":"a1","kind":"read_file","capability_id":"filesystem",'
        '"arguments":{"path":"data.ttl"},"target":{"type":"file","path":"data.ttl","purpose":"inspect"},'
        '"intent":"inspect","expected_observation":"contents","if_fail_next":"stop"}]}'
    )

    assert turn.actions[0].target["path"] == "data.ttl"
    assert turn.actions[0].target["purpose"] == "inspect"


def test_verifier_packet_includes_automatic_memory_findings() -> None:
    env = _env()
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(_runtime(), env)
    ledger = ExecutionLedger()
    ledger.ensure_objective(compiled.objective_graph)
    ledger.record(Receipt(
        "step-0:r1:read",
        0,
        "read_file",
        True,
        "read data.ttl",
        payload={"path": "data.ttl", "content_hash": "same", "excerpt": "alpha"},
    ))
    ledger.record(Receipt(
        "step-1:r2:automatic_memory",
        1,
        "automatic_memory",
        True,
        "automatic memory surfaced 1 prior event(s) for read_file:data.ttl",
        payload={
            "action_kind": "read_file",
            "target": {"action_kind": "read_file", "target_type": "file", "key": "data.ttl", "label": "read_file:data.ttl", "explicit": True},
            "match_count": 1,
            "latest_receipt_id": "step-0:r1:read",
            "same_content_hash": True,
            "repeat_justified": False,
            "guidance": "Automatic memory found prior evidence.",
            "recent_evidence": [{"receipt_id": "step-0:r1:read", "kind": "read_file", "path": "data.ttl"}],
        },
    ))

    packet = build_verifier_packet(compiled, ledger, step=2, reason="no_progress", envmap=_env())
    assert "automatic_memory_findings" not in packet

    solver_context = ContextCompiler().compile(compiled, ledger, [])
    assert solver_context["automatic_memory_findings"]
    assert solver_context["automatic_memory_findings"][0]["target"]["key"] == "data.ttl"


def test_automatic_memory_soft_block_exact_repeat_is_advisory_only() -> None:
    trace = RunTrace()
    hooks = _RepeatedReadHooks(
        runtime=_runtime(automatic_memory_policy=AutomaticMemoryPolicy(mode="soft_block_exact_repeat")),
    )
    result = AetherNextKernel(max_steps=3).run(
        _env(),
        MemoryExecutor(files={"data.ttl": "same"}, workspace_root="/app"),
        hooks,
        trace=trace,
    )

    reads = [receipt for receipt in result.receipts if receipt.kind == "read_file"]
    blocks = [receipt for receipt in result.receipts if receipt.kind == "automatic_memory_advisory"]
    assert len(reads) == 3
    assert blocks
    assert "soft-blocked" in blocks[0].summary
    assert blocks[0].payload["authority"] == "advisory_only"
    assert trace.steps[2]["context_seen"]["automatic_memory_findings"]


def test_automatic_memory_repeat_justification_allows_dispatch() -> None:
    hooks = _RepeatedReadHooks(
        runtime=_runtime(automatic_memory_policy=AutomaticMemoryPolicy(mode="soft_block_exact_repeat")),
        repeat_justification="file may have changed after previous command",
    )
    result = AetherNextKernel(max_steps=2).run(
        _env(),
        MemoryExecutor(files={"data.ttl": "same"}, workspace_root="/app"),
        hooks,
    )

    reads = [receipt for receipt in result.receipts if receipt.kind == "read_file"]
    blocks = [receipt for receipt in result.receipts if receipt.kind == "automatic_memory_block"]
    assert len(reads) == 2
    assert not blocks
