import json
from pathlib import Path

from aether_next import compile_workbench_config


def test_example_configs_round_trip_to_json(contract, config_factory):
    examples = {
        "interactive": config_factory(),
        "service": config_factory(mode="service", selectors=[
            {"kind": "task_contract", "representation": "full", "required": True},
            {"kind": "service_state", "target": "web", "representation": "structured_summary", "required": True},
        ]),
        "batch": config_factory(mode="batch_job", selectors=[
            {"kind": "task_contract", "representation": "full", "required": True},
            {"kind": "job_state", "target": "trainer", "representation": "structured_summary", "required": True},
        ]),
    }
    for name, raw in examples.items():
        encoded = json.dumps(raw, sort_keys=True)
        compiled = compile_workbench_config(json.loads(encoded), contract)
        assert compiled.process_mode.value == ("batch_job" if name == "batch" else name)
