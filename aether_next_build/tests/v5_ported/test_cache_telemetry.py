from aether_next import build_prompt_cache_key, parse_provider_cache_telemetry


def test_responses_usage_cache_telemetry():
    telemetry = parse_provider_cache_telemetry({
        "input_tokens": 2000,
        "input_tokens_details": {"cached_tokens": 1500, "cache_write_tokens": 200},
    })
    assert telemetry.cached_tokens == 1500
    assert telemetry.cache_write_tokens == 200
    assert telemetry.cache_read_share == 0.75
    assert telemetry.cache_write_share == 0.1


def test_chat_completions_usage_cache_telemetry():
    telemetry = parse_provider_cache_telemetry({
        "prompt_tokens": 2400,
        "prompt_tokens_details": {"cached_tokens": 1920, "cache_write_tokens": 0},
    })
    assert telemetry.input_tokens == 2400
    assert telemetry.cache_read_share == 0.8
    assert telemetry.cache_write_share == 0.0


def test_absent_provider_usage_stays_unknown():
    telemetry = parse_provider_cache_telemetry(None)
    assert telemetry.cache_read_share is None
    assert telemetry.cache_write_share is None


def test_prompt_cache_key_is_stable_task_independent_and_60_chars():
    one = build_prompt_cache_key(deployment="gpt-5.4-mini", role="solver", namespace="aether-next")
    two = build_prompt_cache_key(deployment="gpt-5.4-mini", role="solver", namespace="aether-next")
    verifier = build_prompt_cache_key(deployment="gpt-5.4-mini", role="verifier", namespace="aether-next")
    assert one == two
    assert one != verifier
    assert len(one) == 60
    assert "task" not in one


def test_malformed_or_impossible_cache_telemetry_never_crashes_or_claims_share():
    from aether_next import parse_provider_cache_telemetry

    malformed = parse_provider_cache_telemetry({"input_tokens": "not-a-number", "input_tokens_details": {"cached_tokens": -2}})
    assert malformed.input_tokens is None
    assert malformed.cached_tokens is None
    assert malformed.cache_read_share is None

    impossible = parse_provider_cache_telemetry({"input_tokens": 10, "input_tokens_details": {"cached_tokens": 11}})
    assert impossible.cache_read_share is None
