#!/usr/bin/env python3
"""Build replay-injection context packets from an Aether-Next trace.

This is a cheap A/B harness for solver-context experiments. It does not claim
to resume containers or execute a model. It isolates the prompt/context delta:
old trace context versus deterministic enriched context, with an optional
model-written repair hint supplied from a file.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Mapping


def load_trace(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    trace = data.get("trace", data)
    if not isinstance(trace, dict):
        raise ValueError(f"{path} does not contain a trace object")
    return trace


def _steps_before(trace: Mapping[str, Any], step: int) -> list[dict[str, Any]]:
    steps = trace.get("steps", [])
    if not isinstance(steps, list):
        return []
    return [
        item for item in steps
        if isinstance(item, dict) and int(item.get("step", -1)) < step
    ]


def context_at_step(trace: Mapping[str, Any], step: int) -> dict[str, Any]:
    steps = trace.get("steps", [])
    if not isinstance(steps, list):
        return {}
    for item in steps:
        if isinstance(item, dict) and int(item.get("step", -1)) == step:
            ctx = item.get("context_seen", {})
            return dict(ctx) if isinstance(ctx, dict) else {}
    return {}


def _action_key(action: Mapping[str, Any]) -> str:
    kind = str(action.get("kind", "")).strip()
    args = action.get("arguments", {})
    if not isinstance(args, dict):
        args = {}
    if kind == "run_command":
        command = str(args.get("command", "")).strip()
        return command
    if kind == "read_file":
        path = _normalize_replay_path(str(args.get("path", "")).strip())
        return f"read_file:{path}" if path else ""
    return ""


def _normalize_replay_path(path: str) -> str:
    if path.startswith("/app/"):
        return path[len("/app/"):]
    return path


def repeated_actions(trace: Mapping[str, Any], step: int, *, limit: int = 8) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    last_step: dict[str, int] = {}
    for item in _steps_before(trace, step):
        turn = item.get("turn", {})
        if not isinstance(turn, dict):
            continue
        for action in turn.get("actions", []) or []:
            if not isinstance(action, dict):
                continue
            key = _action_key(action)
            if not key:
                continue
            counts[key] += 1
            last_step[key] = int(item.get("step", -1))
    return [
        {"action": action, "count": count, "last_step": last_step[action]}
        for action, count in counts.most_common()
        if count > 1
    ][:limit]


def files_already_read(trace: Mapping[str, Any], step: int, *, limit: int = 12) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    last_step: dict[str, int] = {}
    for item in _steps_before(trace, step):
        for obs in item.get("observations", []) or []:
            if not isinstance(obs, dict):
                continue
            if obs.get("kind") != "read_file" or not bool(obs.get("success")):
                continue
            path = str(obs.get("path", "")).strip()
            if not path:
                continue
            counts[path] += 1
            last_step[path] = int(item.get("step", -1))
    return [
        {"path": path, "read_count": count, "last_step": last_step[path]}
        for path, count in counts.most_common(limit)
    ]


def no_progress_signal(trace: Mapping[str, Any], step: int) -> dict[str, Any]:
    streak = 0
    for item in reversed(_steps_before(trace, step)):
        observations = item.get("observations", []) or []
        progressed = False
        for obs in observations:
            if not isinstance(obs, dict) or not bool(obs.get("success")):
                continue
            if obs.get("kind") in {"write_file", "check_result", "schema_validation", "register_candidate"}:
                progressed = True
                break
        if progressed:
            break
        streak += 1
    return {"no_progress": streak >= 3, "no_progress_streak": streak}


def _repair_hint(label: str, failure_kind: str) -> str:
    if not failure_kind:
        return ""
    if failure_kind == "check_broken":
        return "Do not retry this check command; it appears invalid. Continue by fixing the task artifact."
    if label.startswith("exists:"):
        return f"Create or write the required artifact at {label.split(':', 1)[1]}."
    if label.startswith("schema:"):
        target = label.split(":", 1)[1]
        if target.lower().endswith(".csv"):
            return f"Update {target} so its CSV header contains the required columns."
        return f"Update {target} so it contains the required structured keys."
    return "Change the artifact or strategy before rechecking."


def enrich_context(trace: Mapping[str, Any], step: int, old_context: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(old_context)
    pending = []
    for item in old_context.get("pending_checks", []) or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        failure_kind = str(row.get("failure_kind") or "")
        if row.get("passed") is False and not failure_kind:
            command = str(row.get("command_short", ""))
            if "<" in command and ">" in command:
                failure_kind = "check_broken"
            else:
                failure_kind = "check_failed"
        row["failure_kind"] = failure_kind
        row["repair_hint"] = str(row.get("repair_hint") or _repair_hint(str(row.get("label", "")), failure_kind))
        pending.append(row)
    if pending:
        enriched["pending_checks"] = pending

    repeats = repeated_actions(trace, step)
    if repeats:
        enriched["repeated_actions"] = repeats
    reads = files_already_read(trace, step)
    if reads:
        enriched["files_already_read"] = reads
    stuck = no_progress_signal(trace, step)
    if stuck["no_progress_streak"]:
        enriched["stuck"] = stuck
    return enriched


def build_ab_packet(
    trace: Mapping[str, Any],
    step: int,
    *,
    model_hint: str = "",
) -> dict[str, Any]:
    old_context = context_at_step(trace, step)
    enriched = enrich_context(trace, step, old_context)
    model_context = dict(enriched)
    if model_hint:
        model_context["model_written_repair_hint"] = model_hint
    return {
        "schema_version": "aether_next.replay_injection.v1",
        "step": step,
        "variants": {
            "old_context": old_context,
            "enriched_deterministic_context": enriched,
            "enriched_plus_model_hint_context": model_context,
        },
        "ab_axes": [
            "old_context vs enriched_deterministic_context",
            "enriched_deterministic_context vs enriched_plus_model_hint_context",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--step", required=True, type=int)
    parser.add_argument("--model-hint-file", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    hint = ""
    if args.model_hint_file:
        hint = args.model_hint_file.read_text(encoding="utf-8").strip()
    packet = build_ab_packet(load_trace(args.trace), args.step, model_hint=hint)
    rendered = json.dumps(packet, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if args.out:
        args.out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
