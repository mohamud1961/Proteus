from __future__ import annotations

import json

import pytest

from aether_next.compiler import CapabilityRegistry, ConfigCompiler
from aether_next.model_hooks import ModelOutputError
from aether_next.workbench_compile import config_realization_audit, harness_config_to_runtime_ir
from aether_next.workbench_config import parse_harness_config_ir
from tests.test_vnext_workbench_ir import _env, _raw_config


def _semantic_config() -> str:
    raw = json.loads(_raw_config())
    raw["clause_coverage"] = [
        {
            "clause_id": "c_file",
            "solver_handling": "write the required artifact",
            "verifier_check": "read the current artifact",
        },
    ]
    raw["verifier_strategy"] = {
        "clause_checks": [{
            "clause_id": "c_file",
            "inspection_route": "read_file:/app/out.txt",
            "fallback_route": "inspect_artifact:/app/out.txt",
            "falsification_check": "a changed byte must be detected",
            "required_evidence_class": "exact_contract",
        }],
        "false_positive_traps": ["presence without exact content"],
        "return_all_findings": True,
    }
    return json.dumps(raw)


def test_structured_semantic_contract_reaches_compiler_realization() -> None:
    env = _env()
    config = parse_harness_config_ir(_semantic_config())
    ir = harness_config_to_runtime_ir(config, env)
    compiled = ConfigCompiler(CapabilityRegistry.from_envmap(env)).compile(ir, env)
    realization = dict(compiled.config_realization)
    assert realization["semantic_evidence_status"] == "compiled"
    assert realization["compiled_evidence_requirements"] == [{
        "clause_id": "c_file",
        "minimum_class": "exact_contract",
        "inspection_route": "read_file:/app/out.txt",
        "fallback_route": "inspect_artifact:/app/out.txt",
        "falsification_check": "a changed byte must be detected",
    }]
    assert realization["inspection_evidence_ceilings"] == {
        "read_file:/app/out.txt": "exact_contract",
        "inspect_artifact:/app/out.txt": "exact_contract",
    }
    assert config_realization_audit(config, env)["dispositions"]["verifier_strategy"]["count"] == 1


def test_structured_semantic_contract_rejects_unknown_evidence_class() -> None:
    raw = json.loads(_semantic_config())
    raw["verifier_strategy"]["clause_checks"][0]["required_evidence_class"] = "guessing"
    with pytest.raises(ModelOutputError, match="unknown evidence class"):
        parse_harness_config_ir(json.dumps(raw))
