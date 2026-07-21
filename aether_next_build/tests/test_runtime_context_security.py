import pytest

from aether_next import HarnessRuntime, TaskClause, TaskContract, WorldState


def _runtime(raw_config=None):
    contract = TaskContract.create("Create out.txt.", (TaskClause("c", "out exists"),))
    world = WorldState(contract)
    world.receipts.append(
        "internal",
        {
            "architect_verifier_prompt": "SECRET_PROMPT",
            "solver_journey": "SECRET_JOURNEY",
            "verifier_strategy": "SECRET_STRATEGY",
            "summary": "safe receipt summary",
        },
    )
    config = {
        "context_policy": {
            "selectors": [{"kind": "receipt", "target": "receipt:000001", "representation": "full", "required": True}],
        },
    }
    if raw_config:
        config.update(raw_config)
    return HarnessRuntime(contract=contract, envmap={}, raw_config=config, world=world)


def test_receipt_selector_is_solver_safe():
    runtime = _runtime()
    request, _ = runtime.request()
    text = request.decode()
    assert "SECRET_PROMPT" not in text
    assert "SECRET_JOURNEY" not in text
    assert "SECRET_STRATEGY" not in text
    assert "safe receipt summary" in text


@pytest.mark.parametrize("value", [None, "bad", 7])
def test_malformed_context_policy_fails_closed(value):
    with pytest.raises(ValueError, match="context_policy must be a mapping"):
        _runtime({"context_policy": value})


@pytest.mark.parametrize("key", ["max_events_before_compaction", "max_dynamic_bytes"])
def test_boolean_compaction_limit_fails_closed(key):
    with pytest.raises(ValueError, match="positive integer"):
        _runtime({"context_policy": {key: True}})
