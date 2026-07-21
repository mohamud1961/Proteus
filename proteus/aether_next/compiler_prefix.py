"""Static protocol-card sections of the solver's stable prompt prefix.

Extracted from compiler.py for the 500-LOC cap.  These are byte-stable across
every task and step; dynamic (task/world) sections stay in the compiler.
"""
from __future__ import annotations

from .runtime_ir import stable_json

PROTOCOL_CARD_SECTIONS: tuple[tuple[str, str], ...] = (
            (
                "kernel_contract",
                (
                    "Kernel facts: typed action schema is enforced at dispatch time; receipts are recorded as "
                    "execution evidence; local-only safety and path integrity checks may reject actions; "
                    "submit_outcome asks the runtime to evaluate the current state."
                ),
            ),
            (
                "tool_semantics",
                (
                    "Only action kinds listed in [action_schema] are callable. Disabled tools are rejected before dispatch. "
                    "query_memory, inspect_checks, run_check, query_artifact_history, inspect_diff, and record_observation "
                    "are kernel-owned affordances when present in [action_schema]. Optional action target metadata is used "
                    "for memory matching and does not change tool argument requirements."
                ),
            ),
            (
                "automatic_memory_manual",
                (
                    "Automatic memory compares repeated read_file, write_file, run_command, and run_check actions against prior receipts. "
                    "Stable path, check_id, or command-fingerprint metadata lets the runtime attach matching evidence cheaply. "
                    "automatic_memory_findings, files_already_read, and repeated_actions in context describe prior observations; "
                    "repeat_justification may be required by the active memory policy."
                ),
            ),
            (
                "completion_submit_manual",
                (
                    "Completion facts: inspect_checks and run_check expose harness-visible checks when available; "
                    "submit_outcome is a final completion claim evaluated against current task state; "
                    "active_completion_findings may appear in later context packets when unresolved state or evidence issues remain. "
                    "Local checks and local evidence are useful task evidence, not a substitute for genuine task completion."
                ),
            ),
            (
                "solver_turn_contract",
                stable_json(
                    {
                        "emit_format": "strict_json_only",
                        "turn_kinds": ["act", "submit_outcome"],
                        "required_fields": {
                            "always": ["kind", "summary"],
                            "act": ["actions"],
                            "act_cardinality": "exactly_one_action_frontier",
                            "recommended_turn_fields": ["evidence_gap"],
                            "act_action_fields": ["kind", "arguments"],
                            "recommended_action_fields": [
                                "action_id",
                                "intent",
                                "expected_observation",
                                "if_fail_next",
                            ],
                        },
                        "action_schema_section": "action_schema",
                        "notes": [
                            "Only action kinds listed in action_schema are callable.",
                            "An act turn contains exactly one action frontier.",
                            "Use observe_batch only for bounded independent certified read-only operations; commands, checks, network requests, writes, and process actions must run alone.",
                            "Kernel-owned memory and check affordances are listed in action_schema when available.",
                            "Do not request reconfiguration. Report concrete harness blockers with the report_blocker action.",
                        ],
                    }
                ),
            ),
)
