#!/usr/bin/env python3
"""Run bounded replay A/B solver-continuation experiments.

This executes real next-turn model calls from captured trace checkpoints for:

1. old trace context
2. enriched deterministic context
3. enriched deterministic context plus a model-written repair hint

It is intentionally a *turn-quality* replay, not a full container resume. The
source traces do not include a resumable workspace snapshot, so this script
scores the proposed next solver turn rather than claiming artifact mutation or
grader improvement.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

_BUILD_DIR = str(Path(__file__).resolve().parent)
if _BUILD_DIR not in sys.path:
    sys.path.insert(0, _BUILD_DIR)

from aether_next.model_hooks import SOLVER_SYSTEM_PROMPT, parse_solver_turn  # noqa: E402
from aether_next.providers.azure_model import make_azure_callable  # noqa: E402
from replay_injection import build_ab_packet, load_trace  # noqa: E402


@dataclass(frozen=True)
class ReplayCase:
    case_id: str
    trace: str
    step: int
    target_artifact: str
    repair_terms: tuple[str, ...]
    bad_terms: tuple[str, ...]
    deployment_env: str
    key_env: str


CASES: tuple[ReplayCase, ...] = (
    ReplayCase(
        case_id="filter_js_step3",
        trace="aether_next_build/phase2_traces/codex/filter-js-from-html.trace.json",
        step=3,
        target_artifact="filter.py",
        repair_terms=("filter.py", "script", "javascript", "sanitize", "xss"),
        bad_terms=("<html_file>",),
        deployment_env="AZURE_OPENAI_GPT53_CODEX_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT53_CODEX_KEY",
    ),
    ReplayCase(
        case_id="sparql_step15",
        trace="aether_next_build/phase2_traces/codex/sparql-university.trace.json",
        step=15,
        target_artifact="solution.sparql",
        repair_terms=("solution.sparql", "select", "where", "sparql"),
        bad_terms=("university_graph.ttl", "sed -n", "cat "),
        deployment_env="AZURE_OPENAI_GPT53_CODEX_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT53_CODEX_KEY",
    ),
    ReplayCase(
        case_id="raman_step10",
        trace="aether_next_build/phase2_traces/codex/raman-fitting.trace.json",
        step=10,
        target_artifact="results.json",
        repair_terms=("results.json", "fit", "graphene", "2d", "g"),
        bad_terms=("test -e results.json", "json.load", "schema"),
        deployment_env="AZURE_OPENAI_GPT53_CODEX_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT53_CODEX_KEY",
    ),
    ReplayCase(
        case_id="mini_log_summary_step2",
        trace="aether_next_build/phase2_traces/mini/log-summary-date-ranges.trace.json",
        step=2,
        target_artifact="summary.csv",
        repair_terms=("summary.csv", "csv", "header", "date"),
        bad_terms=("json.load", "json", "schema"),
        deployment_env="AZURE_OPENAI_GPT54_MINI_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT54_MINI_KEY",
    ),
    ReplayCase(
        case_id="extract_elf_step10",
        trace="aether_next_build/phase2_traces/codex/extract-elf.trace.json",
        step=10,
        target_artifact="extract.js",
        repair_terms=("extract.js", "node", "elf", "write_file"),
        bad_terms=("objdump", "readelf", "file /app/a.out"),
        deployment_env="AZURE_OPENAI_GPT53_CODEX_DEPLOYMENT",
        key_env="AZURE_OPENAI_GPT53_CODEX_KEY",
    ),
)

VARIANT_ORDER = (
    "old_context",
    "enriched_deterministic_context",
    "enriched_plus_model_hint_context",
)


def _json_message(section: str, payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "role": "system",
        "content": f"[{section}]\n"
        + json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    }


def _make_solver_messages(trace: Mapping[str, Any], context: Mapping[str, Any]) -> list[dict[str, str]]:
    prefix = trace.get("prefix_messages", [])
    messages = [dict(item) for item in prefix if isinstance(item, dict)]
    messages.append(_json_message("context_packet", context))
    messages.append({"role": "system", "content": SOLVER_SYSTEM_PROMPT})
    return messages


def _make_hint_messages(trace: Mapping[str, Any], context: Mapping[str, Any], case: ReplayCase) -> list[dict[str, str]]:
    prefix = trace.get("prefix_messages", [])
    task_prompt = ""
    for item in prefix:
        if isinstance(item, dict) and str(item.get("content", "")).startswith("[task_prompt]"):
            task_prompt = str(item.get("content", ""))
            break
    payload = {
        "task_prompt": task_prompt[:4000],
        "target_artifact": case.target_artifact,
        "context_packet": context,
        "instruction": (
            "Write one concise deterministic repair hint for the next solver turn. "
            "Do not solve from hidden knowledge. Focus on avoiding repeats and targeting the blocker."
        ),
    }
    return [
        {
            "role": "system",
            "content": "You write compact repair hints for a solver. Return plain text, one sentence.",
        },
        {"role": "user", "content": json.dumps(payload, sort_keys=True, ensure_ascii=True)},
    ]


def _turn_to_dict(turn: Any) -> dict[str, Any]:
    return {
        "kind": getattr(turn, "kind", ""),
        "summary": getattr(turn, "summary", ""),
        "reconfigure_reason": getattr(turn, "reconfigure_reason", ""),
        "requested_check_ids": list(getattr(turn, "requested_check_ids", ()) or ()),
        "claimed_artifacts": list(getattr(turn, "claimed_artifacts", ()) or ()),
        "actions": [asdict(action) for action in getattr(turn, "actions", ()) or ()],
    }


def _action_text(turn_dict: Mapping[str, Any]) -> str:
    return json.dumps(turn_dict, sort_keys=True, ensure_ascii=True).lower()


def score_turn(case: ReplayCase, context: Mapping[str, Any], turn_dict: Mapping[str, Any]) -> dict[str, Any]:
    text = _action_text(turn_dict)
    actions = turn_dict.get("actions", []) or []
    action_kinds = [
        str(action.get("kind", ""))
        for action in actions
        if isinstance(action, dict)
    ]
    repeated_actions = {
        str(item.get("action", "")).lower()
        for item in context.get("repeated_actions", []) or []
        if isinstance(item, dict)
    }

    repeats_old_action = False
    for action in actions:
        if not isinstance(action, dict):
            continue
        args = action.get("arguments", {})
        if not isinstance(args, dict):
            args = {}
        key = ""
        if action.get("kind") == "run_command":
            key = str(args.get("command", "")).lower()
        elif action.get("kind") == "read_file":
            path = str(args.get("path", "")).lower()
            key = "read_file:" + path.removeprefix("/app/")
        if key and key in repeated_actions:
            repeats_old_action = True

    targets_artifact = case.target_artifact.lower() in text
    uses_repair_terms = sum(1 for term in case.repair_terms if term.lower() in text)
    uses_bad_terms = sum(1 for term in case.bad_terms if term.lower() in text)
    writes_or_repairs = any(kind in {"write_file", "run_command"} for kind in action_kinds)
    just_submits = str(turn_dict.get("kind", "")) == "submit_outcome"
    requests_reconfigure = str(turn_dict.get("kind", "")) == "request_reconfigure"

    score = 0
    if targets_artifact:
        score += 2
    if writes_or_repairs:
        score += 1
    if uses_repair_terms:
        score += min(2, uses_repair_terms)
    if repeats_old_action:
        score -= 3
    if uses_bad_terms:
        score -= min(3, uses_bad_terms)
    if just_submits:
        score -= 2
    if requests_reconfigure:
        score -= 1

    return {
        "score": score,
        "targets_artifact": targets_artifact,
        "writes_or_repairs": writes_or_repairs,
        "repeats_old_action": repeats_old_action,
        "repair_term_hits": uses_repair_terms,
        "bad_term_hits": uses_bad_terms,
        "action_kinds": action_kinds,
        "just_submits": just_submits,
        "requests_reconfigure": requests_reconfigure,
    }


def run_case(case: ReplayCase, *, endpoint_env: str, effort: str, max_output_tokens: int) -> dict[str, Any]:
    trace = load_trace(Path(case.trace))
    solver = make_azure_callable(
        deployment_env=case.deployment_env,
        key_env=case.key_env,
        endpoint_env=endpoint_env,
        effort=effort,
        poll_interval_s=1.0,
        poll_timeout_s=240.0,
    )
    packet = build_ab_packet(trace, case.step)
    enriched = packet["variants"]["enriched_deterministic_context"]

    hint_raw = solver(_make_hint_messages(trace, enriched, case), max_output_tokens=800)
    packet = build_ab_packet(trace, case.step, model_hint=hint_raw.strip())

    variants: dict[str, Any] = {}
    for variant_name in VARIANT_ORDER:
        context = packet["variants"][variant_name]
        raw = solver(_make_solver_messages(trace, context), max_output_tokens=max_output_tokens)
        parse_error = ""
        try:
            turn = parse_solver_turn(raw)
            turn_dict = _turn_to_dict(turn)
        except Exception as exc:
            parse_error = str(exc)
            turn_dict = {
                "kind": "parse_error",
                "summary": parse_error,
                "actions": [],
            }
        variants[variant_name] = {
            "context": context,
            "raw_response": raw,
            "turn": turn_dict,
            "parse_error": parse_error,
            "score": score_turn(case, context, turn_dict),
        }

    return {
        "case": asdict(case),
        "schema_version": "aether_next.replay_ab_experiment.v1",
        "resume_limitation": (
            "Executed real next-turn solver model calls from trace context. "
            "Did not mutate a resumed container/workspace because traces do not "
            "include resumable filesystem snapshots."
        ),
        "model_hint": hint_raw.strip(),
        "variants": variants,
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        name: {"score": 0, "repeats": 0, "targets": 0, "writes": 0, "parse_errors": 0}
        for name in VARIANT_ORDER
    }
    for record in records:
        for name in VARIANT_ORDER:
            variant = record["variants"][name]
            score = variant["score"]
            totals[name]["score"] += int(score["score"])
            totals[name]["repeats"] += int(bool(score["repeats_old_action"]))
            totals[name]["targets"] += int(bool(score["targets_artifact"]))
            totals[name]["writes"] += int(bool(score["writes_or_repairs"]))
            totals[name]["parse_errors"] += int(bool(variant["parse_error"]))
    deterministic_delta = totals["enriched_deterministic_context"]["score"] - totals["old_context"]["score"]
    hint_delta = totals["enriched_plus_model_hint_context"]["score"] - totals["enriched_deterministic_context"]["score"]
    if hint_delta >= 5:
        decision = "model_hints_worth_testing_live"
    elif deterministic_delta > 0 and hint_delta <= 1:
        decision = "deterministic_context_enough_for_now"
    else:
        decision = "inconclusive_needs_more_replay"
    return {
        "totals": totals,
        "deterministic_delta_vs_old": deterministic_delta,
        "model_hint_delta_vs_deterministic": hint_delta,
        "decision": decision,
    }


def write_markdown(records: list[dict[str, Any]], summary: Mapping[str, Any], path: Path) -> None:
    lines = [
        "# Replay A/B Recovery Experiment",
        "",
        "This report scores real next-turn solver model calls from trace checkpoints. It does not claim full container replay.",
        "",
        "## Summary",
        "",
        f"- Decision: `{summary['decision']}`",
        f"- Deterministic delta vs old: `{summary['deterministic_delta_vs_old']}`",
        f"- Model-hint delta vs deterministic: `{summary['model_hint_delta_vs_deterministic']}`",
        "",
        "| Variant | Score | Repeats | Targets Artifact | Writes/Repairs | Parse Errors |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, totals in summary["totals"].items():
        lines.append(
            f"| `{name}` | {totals['score']} | {totals['repeats']} | "
            f"{totals['targets']} | {totals['writes']} | {totals['parse_errors']} |"
        )
    lines.extend(["", "## Cases", ""])
    for record in records:
        case_id = record["case"]["case_id"]
        lines.extend([f"### {case_id}", "", f"Model hint: {record['model_hint']}", ""])
        lines.append("| Variant | Score | Repeats Old Action | Targets Artifact | Action Kinds | Summary |")
        lines.append("|---|---:|---|---|---|---|")
        for name in VARIANT_ORDER:
            variant = record["variants"][name]
            score = variant["score"]
            turn = variant["turn"]
            summary_text = str(turn.get("summary", "")).replace("|", "\\|")[:220]
            lines.append(
                f"| `{name}` | {score['score']} | {score['repeats_old_action']} | "
                f"{score['targets_artifact']} | `{','.join(score['action_kinds'])}` | {summary_text} |"
            )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("tracking/collab/aether_next_replay_ab"))
    parser.add_argument("--endpoint-env", default="AZURE_OPENAI_ENDPOINT")
    parser.add_argument("--effort", default="low", choices=["none", "low", "medium", "high", "xhigh"])
    parser.add_argument("--max-output-tokens", type=int, default=6000)
    parser.add_argument("--cases", default=",".join(case.case_id for case in CASES))
    args = parser.parse_args()

    wanted = {item.strip() for item in args.cases.split(",") if item.strip()}
    selected = [case for case in CASES if case.case_id in wanted]
    if not selected:
        raise SystemExit("no matching cases selected")

    for case in selected:
        for env_name in (args.endpoint_env, case.deployment_env, case.key_env):
            if not os.environ.get(env_name):
                raise SystemExit(f"missing required env var: {env_name}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for case in selected:
        print(f"RUN {case.case_id} step={case.step}", flush=True)
        record = run_case(
            case,
            endpoint_env=args.endpoint_env,
            effort=args.effort,
            max_output_tokens=args.max_output_tokens,
        )
        records.append(record)
        (args.out_dir / f"{case.case_id}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        print(
            "DONE "
            + case.case_id
            + " scores="
            + json.dumps({name: record["variants"][name]["score"]["score"] for name in VARIANT_ORDER}, sort_keys=True),
            flush=True,
        )

    summary = summarize(records)
    (args.out_dir / "summary.json").write_text(
        json.dumps({"summary": summary, "records": records}, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    write_markdown(records, summary, args.out_dir / "REPORT.md")
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
