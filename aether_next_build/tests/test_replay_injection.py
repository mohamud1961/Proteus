from __future__ import annotations

from replay_injection import build_ab_packet


def test_replay_injection_builds_old_enriched_and_model_hint_variants() -> None:
    trace = {
        "steps": [
            {
                "step": 0,
                "context_seen": {},
                "turn": {
                    "actions": [
                        {"kind": "read_file", "arguments": {"path": "graph.ttl"}},
                    ],
                },
                "observations": [
                    {"kind": "read_file", "success": True, "path": "graph.ttl"},
                ],
            },
            {
                "step": 1,
                "context_seen": {},
                "turn": {
                    "actions": [
                        {"kind": "read_file", "arguments": {"path": "graph.ttl"}},
                    ],
                },
                "observations": [
                    {"kind": "read_file", "success": True, "path": "graph.ttl"},
                ],
            },
            {
                "step": 2,
                "context_seen": {},
                "turn": {"actions": []},
                "observations": [],
            },
            {
                "step": 3,
                "context_seen": {
                    "pending_checks": [
                        {
                            "label": "schema:summary.csv",
                            "command_short": "python3 -c csv_check",
                            "passed": False,
                        }
                    ],
                },
                "turn": {"actions": []},
                "observations": [],
            },
        ],
    }

    packet = build_ab_packet(trace, 3, model_hint="Try rewriting only the CSV header.")

    variants = packet["variants"]
    enriched = variants["enriched_deterministic_context"]
    model = variants["enriched_plus_model_hint_context"]

    assert variants["old_context"]["pending_checks"][0]["passed"] is False
    assert enriched["pending_checks"][0]["failure_kind"] == "check_failed"
    assert "CSV header" in enriched["pending_checks"][0]["repair_hint"]
    assert enriched["repeated_actions"][0]["action"] == "read_file:graph.ttl"
    assert enriched["files_already_read"][0]["read_count"] == 2
    assert enriched["stuck"]["no_progress"] is True
    assert model["model_written_repair_hint"] == "Try rewriting only the CSV header."
