from dataclasses import asdict
import json
from pathlib import Path

import jsonschema
import pytest

from aether_next import HarnessRuntime, StableEnvMap

ROOT = Path(__file__).resolve().parents[2]


def schema(name: str):
    return json.loads((ROOT / "schemas" / name).read_text())


def test_workbench_schema_accepts_all_three_modes(contract, config_factory):
    validator = jsonschema.Draft202012Validator(schema("workbench_config_v4.schema.json"))
    configs = [
        config_factory(),
        config_factory(mode="service", selectors=[
            {"kind": "task_contract", "representation": "full", "required": True},
            {"kind": "service_state", "target": "web", "representation": "full", "required": True},
        ]),
        config_factory(mode="batch_job", selectors=[
            {"kind": "task_contract", "representation": "full", "required": True},
            {"kind": "job_state", "target": "trainer", "representation": "full", "required": True},
        ]),
    ]
    for raw in configs:
        # The V5 reference schema deliberately rejected Architect tool choice.
        # Canonical parsing retains this legacy field only as ignored advisory
        # input, so remove it when validating the historic schema itself.
        raw.pop("tool_policy", None)
        validator.validate(raw)


def test_workbench_schema_rejects_tool_selection(config_factory):
    raw = config_factory()
    raw["tool_policy"] = {"enabled_tools": ["read_file"]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(raw, schema("workbench_config_v4.schema.json"))


def test_cache_manifest_schema_matches_runtime(contract, world, config_factory):
    runtime = HarnessRuntime(contract=contract, envmap={"workspace": "/app"}, raw_config=config_factory(), world=world)
    _, manifest = runtime.request({"input_tokens": 2000, "input_tokens_details": {"cached_tokens": 1500, "cache_write_tokens": 100}})
    jsonschema.validate(asdict(manifest), schema("context_cache_manifest_v2.schema.json"))


def test_verifier_schema_enforces_owner_axis():
    valid = {
        "task_judgement": "not_judged",
        "verification_status": "failed",
        "owner": "verifier_tooling",
        "summary": "python absent",
        "findings": [],
        "evidence": []
    }
    jsonschema.validate(valid, schema("verifier_outcome_v2.schema.json"))
    invalid = dict(valid, task_judgement="needs_repair")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema("verifier_outcome_v2.schema.json"))


def test_stable_envmap_schema_matches_runtime():
    envmap = StableEnvMap.create({"workspace": "/app", "python": {"version": "3.13"}})
    jsonschema.validate(envmap.to_payload(), schema("stable_envmap_v1.schema.json"))


def test_dynamic_state_delta_schema_accepts_typed_progress_and_rejects_unknown():
    valid = {
        "installed_packages": {"grpcio": "1.73.0"},
        "services": {"server-1": {"state": "listening", "port": 5328}},
        "files": {"/app/server.py": {"status": "modified", "step": 4}},
    }
    jsonschema.validate(valid, schema("dynamic_state_delta_v1.schema.json"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"mystery": 1}, schema("dynamic_state_delta_v1.schema.json"))
