#!/usr/bin/env python3
"""Static generic capability audit for an official task corpus.

This uses task.toml, instruction.md, and environment-visible files as a coverage
corpus.  It intentionally ignores solution/ and tests/ contents and does not
emit task-specific success logic.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aether_next.task_capability import classify_capability_needs, flatten_task_toml, required_tool_hints


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _environment_files(task_dir: Path) -> tuple[str, ...]:
    files: list[str] = []
    for base_name in ("environment",):
        base = task_dir / base_name
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                files.append(str(path.relative_to(task_dir)))
    # Top-level non-hidden artifacts other than task metadata are visible surface too.
    for path in task_dir.iterdir():
        if path.is_file() and path.name not in {"instruction.md", "task.toml"}:
            files.append(path.name)
    return tuple(sorted(set(files)))


# ---------------------------------------------------------------------------
# Harness support matrix (per capability class, evidence-referenced)
#
# status vocabulary:
#   supported                     -- a generic solver path AND a generic
#                                    verifier path exist in the harness today
#   supported_with_environment_gate -- generic paths exist; reachability
#                                    depends on a probed environment fact
#                                    (e.g. external network), which the
#                                    harness reports honestly, never assumes
#
# Every entry names the generic mechanism (module refs), never a task hook.
# ---------------------------------------------------------------------------
_EXECUTION_EVIDENCE = (
    "solver: run_command with task-budget timeout (kernel_dispatch._action_timeout_s, cap 12000s); "
    "runner wall clock honors agent.timeout_sec (docker_runner._effective_run_timeout_s, cap 14400s); "
    "full output spooled beyond 1MB, retrievable by handle (real_executor.StreamSpooler; kernel read_output/grep_output); "
    "verifier: overlay execution with task verifier budget (verifier_overlay.VerifierOverlay; kernel_verifier._verifier_command_budget_s)"
)
_SERVICE_EVIDENCE = (
    "solver: launch_process/probe_service/stop_process (execution.ProcessOrchestratorV2, interactive_detachable policy); "
    "verifier: probe_port/probe_http/probe_process live-state probes (verifier_probes.py)"
)
_ARTIFACT_EVIDENCE = (
    "solver: inspect_artifact perception lane + run_command with image toolchain; "
    "verifier: inspect_artifact probe (type/size/sha256 + ffprobe/pdftotext/identify best-effort, honest tool_missing) "
    "and overlay fixtures (overlay_write_fixture + overlay_run_command)"
)
_INTERACTIVE_EVIDENCE = (
    "solver: scripted interaction via run_command with expect/pexpect authored by the solver "
    "(generic scripting -- no bespoke TTY channel by design; a stronger model scripts better) "
    "plus launch_process for daemons; verifier: probe_port/probe_process + overlay execution"
)

HARNESS_SUPPORT: dict[str, dict[str, str]] = {
    "long_running_command": {"status": "supported", "path": _EXECUTION_EVIDENCE},
    "compiler_build": {"status": "supported", "path": _EXECUTION_EVIDENCE},
    "rust_build": {"status": "supported", "path": _EXECUTION_EVIDENCE},
    "ocaml_coq_build": {"status": "supported", "path": _EXECUTION_EVIDENCE},
    "ml_training_or_inference": {"status": "supported", "path": _EXECUTION_EVIDENCE},
    "scientific_computing": {"status": "supported", "path": _EXECUTION_EVIDENCE},
    "database": {"status": "supported", "path": _EXECUTION_EVIDENCE},
    "crypto_security": {"status": "supported", "path": _EXECUTION_EVIDENCE},
    "binary_reverse_engineering": {"status": "supported", "path": _ARTIFACT_EVIDENCE},
    "image_processing": {"status": "supported", "path": _ARTIFACT_EVIDENCE},
    "video_processing": {"status": "supported", "path": _ARTIFACT_EVIDENCE},
    "ocr_pdf_document": {"status": "supported", "path": _ARTIFACT_EVIDENCE},
    "background_service": {"status": "supported", "path": _SERVICE_EVIDENCE},
    "http_service": {"status": "supported", "path": _SERVICE_EVIDENCE},
    "ssh_or_telnet_service": {"status": "supported", "path": _INTERACTIVE_EVIDENCE},
    "qemu_vm": {"status": "supported", "path": _INTERACTIVE_EVIDENCE},
    "network_download": {
        "status": "supported_with_environment_gate",
        "path": (
            "solver: bootstrap_acquire + run_command; EnvMap network_scope is probed, never assumed "
            "(envmap_builder: unknown until live probe); offline environments are reported as a probed "
            "environment fact, not absorbed as a harness failure"
        ),
    },
}

_STATUS_ORDER = ("unsupported", "partially_supported", "supported_with_environment_gate", "supported")


def _support_rows(needs: list[str]) -> tuple[list[str], str]:
    """Per-class support statuses for one task + the task's worst status."""
    rows: list[str] = []
    worst = "supported"
    for cap in needs:
        entry = HARNESS_SUPPORT.get(cap)
        status = entry["status"] if entry else "unsupported"
        rows.append(f"{cap}={status}")
        if _STATUS_ORDER.index(status) < _STATUS_ORDER.index(worst):
            worst = status
    return rows, worst


def audit_task(task_dir: Path) -> dict[str, str]:
    instruction = _read_text(task_dir / "instruction.md")
    task_toml = _load_toml(task_dir / "task.toml")
    metadata = flatten_task_toml(task_toml)
    visible_files = _environment_files(task_dir)
    needs = classify_capability_needs(instruction, task_metadata=metadata, visible_files=visible_files)
    caps = [need.capability for need in needs]
    tools = required_tool_hints(needs)
    verifier_needs = sorted({tool for need in needs for tool in need.verifier_needs})
    budget = metadata.get("resource_budget", {}) if isinstance(metadata.get("resource_budget"), dict) else {}
    return {
        "task_name": task_dir.name,
        "category": str(metadata.get("category", "")),
        "difficulty": str(metadata.get("difficulty", "")),
        "tags": ";".join(metadata.get("tags", ()) or ()),
        "agent_timeout_sec": str(metadata.get("agent_timeout_sec", "")),
        "verifier_timeout_sec": str(metadata.get("verifier_timeout_sec", "")),
        "build_timeout_sec": str(budget.get("build_timeout_sec", "")),
        "docker_image": str(budget.get("docker_image", "")),
        "visible_environment_files": str(len(visible_files)),
        "capability_classes": ";".join(caps),
        "required_tool_hints": ";".join(tools),
        "verifier_capability_needs": ";".join(verifier_needs),
        "capability_support": ";".join(_support_rows(caps)[0]),
        "readiness": _support_rows(caps)[1],
        "notes": "generic coverage audit; no solution/tests inspected",
    }


def write_outputs(rows: list[dict[str, str]], csv_path: Path, md_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "task_name", "category", "difficulty", "tags", "agent_timeout_sec", "verifier_timeout_sec",
        "build_timeout_sec", "docker_image", "visible_environment_files", "capability_classes",
        "required_tool_hints", "verifier_capability_needs", "capability_support", "readiness", "notes",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    cap_counts: Counter[str] = Counter()
    readiness_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    verifier_counts: Counter[str] = Counter()
    for row in rows:
        readiness_counts[row["readiness"]] += 1
        for cap in filter(None, row["capability_classes"].split(";")):
            cap_counts[cap] += 1
        for tool in filter(None, row["required_tool_hints"].split(";")):
            tool_counts[tool] += 1
        for need in filter(None, row["verifier_capability_needs"].split(";")):
            verifier_counts[need] += 1
    lines = [
        "# Official Task Generic Capability Audit (Local Static)",
        "",
        "This audit uses official tasks as a generic coverage corpus. It does not inspect solution/ or tests/ contents and does not encode task-name-specific behavior.",
        "",
        f"Tasks audited: {len(rows)}",
        "",
        "## Readiness buckets (worst per-class status per task)",
    ]
    for key, count in readiness_counts.most_common():
        lines.append(f"- {key}: {count}")
    lines += ["", "## Harness support matrix (per capability class)", "", "| capability class | status | generic solver+verifier path |", "|---|---|---|"]
    for cap in sorted(HARNESS_SUPPORT):
        entry = HARNESS_SUPPORT[cap]
        lines.append(f"| {cap} | {entry['status']} | {entry['path']} |")
    lines += ["", "## Capability class coverage"]
    for key, count in cap_counts.most_common():
        lines.append(f"- {key}: {count}")
    lines += ["", "## Most common required tool hints"]
    for key, count in tool_counts.most_common(40):
        lines.append(f"- {key}: {count}")
    lines += ["", "## Verifier capability needs"]
    for key, count in verifier_counts.most_common(40):
        lines.append(f"- {key}: {count}")
    lines += ["", "## Per-task table", "", "| task | category | capability classes | readiness |", "|---|---|---|---|"]
    for row in sorted(rows, key=lambda r: r["task_name"]):
        lines.append(f"| {row['task_name']} | {row['category']} | {row['capability_classes']} | {row['readiness']} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tasks_root", type=Path)
    parser.add_argument("--csv", type=Path, default=Path("OFFICIAL_TASK_CAPABILITY_AUDIT_LOCAL.csv"))
    parser.add_argument("--md", type=Path, default=Path("OFFICIAL_TASK_CAPABILITY_AUDIT_LOCAL.md"))
    args = parser.parse_args()
    task_dirs = sorted(path for path in args.tasks_root.iterdir() if (path / "task.toml").exists())
    rows = [audit_task(path) for path in task_dirs]
    write_outputs(rows, args.csv, args.md)
    print(json.dumps({"tasks": len(rows), "csv": str(args.csv), "md": str(args.md)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
