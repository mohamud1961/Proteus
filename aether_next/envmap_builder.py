"""Build an EnvMap from a task directory for Aether-Next runs."""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
import re
from typing import Any

from .runtime_ir import CapabilityDescriptor, EnvMap
from .task_public_metadata import flatten_task_toml


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}
_MAX_VISIBLE = 2_000
_PROMPT_DELIVERABLE_RE = re.compile(
    r"(?:write|create|produce|save|output|submit)\s+`?(/app/[\w./-]+)`?",
    re.IGNORECASE,
)

_SUBSTRATE_CAPABILITIES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "shell",
        "Execute shell commands via bash",
        ("run_command",),
        "cheap",
    ),
    (
        "filesystem",
        "Read and write files in the workspace",
        ("read_file", "write_file"),
        "cheap",
    ),
    (
        "managed_process",
        "Launch, probe, and stop background processes",
        ("launch_process", "probe_service", "stop_process"),
        "moderate",
    ),
    (
        "service_probe",
        "Probe liveness of running services",
        ("probe_service",),
        "cheap",
    ),
    (
        "artifact_inspection",
        "Inspect artifacts (text, binary, structured data)",
        ("inspect_artifact",),
        "moderate",
    ),
    (
        "output_handle_retrieval",
        "Retrieve full command outputs by handle",
        ("read_output", "grep_output"),
        "cheap",
    ),
    (
        "network_fetch",
        "Fetch resources via curl/wget/git/pip",
        ("bootstrap_acquire",),
        "expensive",
    ),
)


def _scan_visible(
    base_dir: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Scan *base_dir* for visible files and directories, bounded."""
    files: list[str] = []
    dirs: list[str] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(base_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, base_dir)
        if rel_dir != ".":
            dirs.append(rel_dir)
        for fname in sorted(filenames):
            files.append(os.path.relpath(os.path.join(dirpath, fname), base_dir))
            count += 1
            if count >= _MAX_VISIBLE:
                return tuple(files), tuple(dirs)
    return tuple(files), tuple(dirs)


def _build_file_tree(files: tuple[str, ...], dirs: tuple[str, ...], *, limit: int = 250) -> str:
    """Render visible paths as a compact tree for model-facing EnvMap context."""
    paths = sorted(set(dirs) | set(files))[: max(0, limit)]
    if not paths:
        return "/app\n"
    lines = ["/app"]
    for path in paths:
        depth = path.count("/")
        name = path.rsplit("/", 1)[-1]
        suffix = "/" if path in dirs else ""
        lines.append(f"{'  ' * depth}- {name}{suffix}")
    if len(set(dirs) | set(files)) > limit:
        lines.append(f"... truncated after {limit} visible paths")
    return "\n".join(lines)


def _build_file_map_summary(files: tuple[str, ...], dirs: tuple[str, ...]) -> dict[str, object]:
    """Build factual path/extension counts without assigning task roles."""
    top_level = sorted({item.split("/", 1)[0] for item in (*files, *dirs) if item})
    extension_counts: dict[str, int] = {}
    for path in files:
        suffix = Path(path).suffix.lower()
        if suffix:
            extension_counts[suffix] = extension_counts.get(suffix, 0) + 1
    return {
        "top_level": top_level[:50],
        "extension_counts": dict(sorted(extension_counts.items())),
        "visible_file_count": len(files),
        "visible_dir_count": len(dirs),
    }



def _build_visible_validation_surfaces(files: tuple[str, ...], instruction_text: str) -> list[dict[str, Any]]:
    """Return visible-only validation surfaces for architect setup.

    This is the clean replacement for grader-shaped hints: only files, scripts,
    commands, examples, fixtures, and README/package surfaces that are visible in
    task/workspace material are included.
    """
    surfaces: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, evidence: str) -> None:
        value = value.strip()
        key = (kind, value)
        if not value or key in seen or len(surfaces) >= 40:
            return
        seen.add(key)
        surfaces.append({"kind": kind, "value": value, "evidence": evidence})

    for path in files:
        name = path.rsplit("/", 1)[-1].lower()
        lower = path.lower()
        if name in {"readme", "readme.md", "readme.txt"}:
            add("readme", path, "visible workspace file")
        if name in {"makefile", "package.json", "pyproject.toml", "tox.ini", "pytest.ini", "cargo.toml"}:
            add("project_command_surface", path, "visible workspace file")
        if any(part in lower for part in ("test", "tests", "check", "verify", "fixture", "example", "sample")):
            add("visible_test_or_example", path, "visible workspace file name")

    command_patterns = (
        r"`([^`\n]*(?:pytest|make|npm test|python3?\s+[^`\n]+|cargo test|go test|Rscript)[^`\n]*)`",
        r"(?:run|execute|use)\s+`([^`\n]+)`",
    )
    for pattern in command_patterns:
        for match in re.finditer(pattern, instruction_text, flags=re.IGNORECASE):
            add("instruction_command", match.group(1), "visible task instruction")
    return surfaces


_MATERIAL_EXTENSIONS = {
    ".csv", ".tsv", ".json", ".jsonl", ".toml", ".yaml", ".yml", ".txt", ".log",
    ".ttl", ".rdf", ".sparql", ".sql", ".db", ".sqlite", ".sqlite3",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".png", ".jpg", ".jpeg", ".pdf",
    ".gcode", ".gco", ".nc", ".proto", ".html", ".pem", ".crt", ".key",
    ".py", ".js", ".ts", ".sh", ".c", ".cpp", ".rs", ".R", ".r", ".stan",
}
_EXAMPLE_TOKENS = ("example", "sample", "fixture", "demo", "input", "output", "expected")


def _build_visible_materials(files: tuple[str, ...], dirs: tuple[str, ...]) -> dict[str, Any]:
    """Return high-recall visible task material facts without reading hidden data."""
    declared_assets: list[dict[str, Any]] = []
    visible_examples: list[dict[str, Any]] = []
    extension_counts: dict[str, int] = {}
    dir_file_counts: dict[str, int] = {}
    grouped: dict[str, dict[str, Any]] = {}

    for path in files:
        p = Path(path)
        ext = p.suffix.lower()
        if ext:
            extension_counts[ext] = extension_counts.get(ext, 0) + 1
        parent = str(p.parent) if str(p.parent) != "." else "."
        dir_file_counts[parent] = dir_file_counts.get(parent, 0) + 1
        lower = path.lower()
        is_material = ext in _MATERIAL_EXTENSIONS or any(token in lower for token in _EXAMPLE_TOKENS)
        if is_material and len(declared_assets) < 80:
            declared_assets.append({
                "path": path,
                "extension": ext,
                "mime_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                "evidence": "visible workspace file metadata",
            })
        if any(token in lower for token in _EXAMPLE_TOKENS) and len(visible_examples) < 60:
            visible_examples.append({
                "path": path,
                "extension": ext,
                "mime_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                "evidence": "visible filename contains example/sample/fixture/input/output token",
            })
        if ext and parent != ".":
            row = grouped.setdefault(parent, {"dir": parent, "file_count": 0, "extensions": {}})
            row["file_count"] += 1
            row["extensions"][ext] = row["extensions"].get(ext, 0) + 1

    notable_dirs = [
        {"dir": d, "file_count": c}
        for d, c in sorted(dir_file_counts.items(), key=lambda item: (-item[1], item[0]))[:30]
    ]
    directory_materials = []
    for row in sorted(grouped.values(), key=lambda item: (-item["file_count"], item["dir"]))[:30]:
        directory_materials.append({
            "dir": row["dir"],
            "file_count": row["file_count"],
            "extensions": dict(sorted(row["extensions"].items())),
        })
    return {
        "declared_assets": declared_assets,
        "visible_examples": visible_examples,
        "visible_material_summary": {
            "extension_counts": dict(sorted(extension_counts.items())),
            "notable_dirs": notable_dirs,
            "directory_materials": directory_materials,
            "visible_file_count": len(files),
            "visible_dir_count": len(dirs),
        },
    }




def _extract_output_paths(text: str, *, limit: int = 12) -> list[str]:
    raw_paths = re.findall(r"/app/[A-Za-z0-9_./-]+", text)
    paths = [path.rstrip(".,:;)") for path in raw_paths]
    return list(dict.fromkeys(paths))[:limit]


def _static_task_hints(instruction_text: str) -> dict[str, object]:
    """Exact prompt path references only; no inferred tools or task families."""
    return {
        "output_paths": _extract_output_paths(instruction_text),
        "prompt_declared_output_paths": list(dict.fromkeys(
            match.group(1).rstrip(".,:;)")
            for match in _PROMPT_DELIVERABLE_RE.finditer(instruction_text)
        ))[:12],
    }


def _classify_instruction_paths(
    referenced_paths: list[str],
    prompt_declared_output_paths: list[str],
    *,
    workspace_root: str,
    visible_files: tuple[str, ...],
    visible_dirs: tuple[str, ...],
) -> dict[str, list[str]]:
    visible_files_set = {path.strip("./") for path in visible_files}
    visible_dirs_set = {path.strip("./") for path in visible_dirs}
    basename_index: dict[str, list[str]] = {}
    for path in (*visible_files, *visible_dirs):
        clean = path.strip("./")
        basename_index.setdefault(clean.rsplit("/", 1)[-1], []).append(clean)
    referenced_rel = [str(path).strip().replace("\\", "/") for path in referenced_paths]
    declared_output_rel = [str(path).strip().replace("\\", "/") for path in prompt_declared_output_paths]
    normalized_referenced = [
        path[len(workspace_root.rstrip("/") + "/") :]
        if workspace_root and path.startswith(workspace_root.rstrip("/") + "/")
        else path.lstrip("/")
        for path in referenced_rel
    ]
    normalized_declared_outputs = [
        path[len(workspace_root.rstrip("/") + "/") :]
        if workspace_root and path.startswith(workspace_root.rstrip("/") + "/")
        else path.lstrip("/")
        for path in declared_output_rel
    ]

    def is_visible(path: str) -> bool:
        clean = path.strip("./")
        if clean in visible_files_set or clean in visible_dirs_set:
            return True
        parent = clean.rsplit("/", 1)[0] if "/" in clean else ""
        if parent and parent in visible_dirs_set:
            return True
        basename_matches = basename_index.get(clean.rsplit("/", 1)[-1], ())
        return len(basename_matches) == 1

    def basename_matches(path: str) -> list[str]:
        clean = path.strip("./")
        matches = basename_index.get(clean.rsplit("/", 1)[-1], ())
        return matches if len(matches) == 1 else []

    referenced_visible = [path for path in normalized_referenced if is_visible(path)]
    referenced_missing = [path for path in normalized_referenced if not is_visible(path)]
    declared_outputs_visible = [path for path in normalized_declared_outputs if is_visible(path)]
    declared_outputs_missing = [path for path in normalized_declared_outputs if not is_visible(path)]

    return {
        "instruction_referenced_paths": list(dict.fromkeys(normalized_referenced))[:20],
        "instruction_referenced_visible_paths": list(dict.fromkeys(referenced_visible))[:20],
        "instruction_referenced_missing_paths": list(dict.fromkeys(referenced_missing))[:20],
        "instruction_referenced_alias_matches": list(dict.fromkeys(
            match
            for path in normalized_referenced
            for match in basename_matches(path)
        ))[:20],
        "prompt_declared_output_paths": list(dict.fromkeys(normalized_declared_outputs))[:20],
        "prompt_declared_output_visible_paths": list(dict.fromkeys(declared_outputs_visible))[:20],
        "prompt_declared_output_missing_paths": list(dict.fromkeys(declared_outputs_missing))[:20],
        "prompt_declared_output_alias_matches": list(dict.fromkeys(
            match
            for path in normalized_declared_outputs
            for match in basename_matches(path)
        ))[:20],
    }



def _action_affordances(capabilities: dict[str, CapabilityDescriptor]) -> list[dict[str, Any]]:
    affordances: list[dict[str, Any]] = []
    for cap in sorted(capabilities.values(), key=lambda item: item.capability_id):
        for tool in cap.tool_names:
            affordances.append({
                "action": tool,
                "available": bool(cap.available),
                "source_capability": cap.capability_id,
                "scope": "solver_and_reviewer",
            })
    return affordances


def _observed_environment_support(probe: dict[str, Any]) -> dict[str, Any]:
    commands = probe.get("command_names") if isinstance(probe.get("command_names"), dict) else {}
    python = probe.get("python") if isinstance(probe.get("python"), dict) else {}
    network = probe.get("network") if isinstance(probe.get("network"), dict) else {}
    modules = python.get("modules") if isinstance(python.get("modules"), dict) else {}

    def command_status(name: str) -> str:
        info = commands.get(name) if isinstance(commands, dict) else None
        if not isinstance(info, dict):
            return "unknown"
        return "available" if info.get("available") else "missing"

    def module_status(name: str) -> str:
        info = modules.get(name) if isinstance(modules, dict) else None
        if not isinstance(info, dict):
            return "unknown"
        return "available" if info.get("available") else "missing"

    package_contract = python.get("package_contract") if isinstance(python.get("package_contract"), dict) else {}
    return {
        "commands": {name: command_status(name) for name in sorted(commands)},
        "python": {
            "preferred": python.get("preferred", ""),
            "interpreters": python.get("interpreters", []),
            "package_contract": package_contract,
        },
        "packages": {name: module_status(name) for name in sorted(modules)},
        "media": {
            "ffmpeg": command_status("ffmpeg"),
            "ffprobe": command_status("ffprobe"),
            "tesseract": command_status("tesseract"),
            "imagemagick_convert": command_status("convert"),
            "imagemagick_magick": command_status("magick"),
            "python_cv2": module_status("cv2"),
            "python_pil": module_status("PIL"),
            "python_numpy": module_status("numpy"),
        },
        "network": network or {"status": "unknown"},
        "services": {
            "curl": command_status("curl"),
            "wget": command_status("wget"),
            "grpcurl": command_status("grpcurl"),
            "nginx": command_status("nginx"),
        },
    }


def _reviewer_probe_support(probe: dict[str, Any], capabilities: dict[str, CapabilityDescriptor]) -> dict[str, Any]:
    commands = probe.get("command_names") if isinstance(probe.get("command_names"), dict) else {}
    python = probe.get("python") if isinstance(probe.get("python"), dict) else {}
    modules = python.get("modules") if isinstance(python.get("modules"), dict) else {}

    def has_cmd(name: str) -> bool | str:
        info = commands.get(name) if isinstance(commands, dict) else None
        return bool(info.get("available")) if isinstance(info, dict) else "unknown"

    def has_module(name: str) -> bool | str:
        info = modules.get(name) if isinstance(modules, dict) else None
        return bool(info.get("available")) if isinstance(info, dict) else "unknown"

    available_actions = {tool for cap in capabilities.values() if cap.available for tool in cap.tool_names}
    python_available = bool(python.get("preferred")) or has_cmd("python3") is True or has_cmd("python") is True

    can_probe_http: bool | str
    if python_available or has_cmd("curl") is True or has_cmd("wget") is True:
        can_probe_http = True
    elif has_cmd("curl") == "unknown" and has_cmd("wget") == "unknown" and not python:
        can_probe_http = "unknown"
    else:
        can_probe_http = "degraded_missing_python_or_http_tool"

    can_probe_ports: bool | str = True if python_available else "degraded_missing_python"

    can_sample_media: bool | str
    if has_cmd("ffmpeg") is True or has_module("cv2") is True or has_module("PIL") is True:
        can_sample_media = True
    elif has_cmd("ffmpeg") == "unknown" and has_module("cv2") == "unknown" and has_module("PIL") == "unknown":
        can_sample_media = "unknown"
    else:
        can_sample_media = "metadata_only_or_tool_missing"

    return {
        "can_read_files": True,
        "can_stat_artifacts": True,
        "can_read_output_handles": True,
        "can_inspect_artifacts": True,
        "can_probe_ports": can_probe_ports,
        "can_probe_http": can_probe_http,
        "can_list_processes": True if has_cmd("ps") is True or has_cmd("pgrep") is True else has_cmd("ps"),
        "can_sample_media_frames": can_sample_media,
        "python_probe_fallback": python_available,
        "source": "verifier_inspector_schema_and_live_probe_tools",
    }

def _build_capabilities() -> dict[str, CapabilityDescriptor]:
    """Build the generic substrate capability descriptors."""
    result: dict[str, CapabilityDescriptor] = {}
    for cap_id, summary, tool_names, cost_hint in _SUBSTRATE_CAPABILITIES:
        result[cap_id] = CapabilityDescriptor(
            capability_id=cap_id,
            summary=summary,
            available=True,
            tool_names=tool_names,
            cost_hint=cost_hint,
        )
    return result


def build_envmap_from_task(
    task_dir: str,
    instruction_text: str,
    *,
    workspace_root: str = "/app",
    network_scope: str = "unknown",
    task_metadata: dict[str, object] | None = None,
    task_toml: dict[str, object] | None = None,
) -> EnvMap:
    """Build an ``EnvMap`` from a task directory and instruction text.

    Parameters
    ----------
    task_dir:
        Path to the task directory (used to scan for visible files).
    instruction_text:
        The task instruction / prompt text.
    workspace_root:
        The in-container workspace root (default ``/app``).
    network_scope:
        Probed network access policy. Use ``unknown`` unless a live probe proved access or denial.
    task_metadata:
        Optional structured facts from the live task environment, such as
        command/module availability probes.
    task_toml:
        Optional public task metadata/resource budgets from task.toml.

    Returns
    -------
    EnvMap
        Ready for the kernel's ``run`` method.
    """
    task_path = Path(task_dir).resolve()
    if task_path.is_dir():
        visible_files, visible_dirs = _scan_visible(str(task_path))
    else:
        visible_files = ()
        visible_dirs = ()

    capabilities = _build_capabilities()
    static_hints = _static_task_hints(instruction_text)
    summary = _build_file_map_summary(visible_files, visible_dirs)
    visible_validation_surfaces = _build_visible_validation_surfaces(visible_files, instruction_text)
    visible_materials = _build_visible_materials(visible_files, visible_dirs)
    path_classification = _classify_instruction_paths(
        list(static_hints["output_paths"]),
        list(static_hints["prompt_declared_output_paths"]),
        workspace_root=workspace_root,
        visible_files=visible_files,
        visible_dirs=visible_dirs,
    )
    summary["instruction_output_paths"] = list(static_hints["output_paths"])
    summary.update(path_classification)
    metadata = dict(task_metadata or {})
    public_task_metadata = flatten_task_toml(task_toml)
    if public_task_metadata:
        # Keep public benchmark-shaped metadata internal.  Only resource/runtime
        # budgets are model-facing because they constrain the workbench.
        metadata.setdefault("internal_task_metadata", public_task_metadata)
        for key in ("resource_budget", "agent_timeout_sec", "verifier_timeout_sec"):
            if key in public_task_metadata and key not in metadata:
                metadata[key] = public_task_metadata[key]
        budget = public_task_metadata.get("resource_budget") if isinstance(public_task_metadata.get("resource_budget"), dict) else {}
        if isinstance(budget, dict):
            metadata.setdefault("model_facing_resource_budget", {
                k: v for k, v in budget.items()
                if k in {"agent_timeout_sec", "verifier_timeout_sec", "build_timeout_sec", "cpus", "memory", "storage"}
            })
    metadata.setdefault("instruction_path_references", dict(static_hints))
    metadata["env_fact_policy"] = {
        "rule": "EnvMap exposes visible/probed facts only; task semantics and strategy belong to the Architect",
        "semantic_task_classification_present": False,
    }
    metadata["visible_validation_surfaces"] = visible_validation_surfaces
    metadata["declared_assets"] = visible_materials["declared_assets"]
    metadata["visible_examples"] = visible_materials["visible_examples"]
    metadata["visible_material_summary"] = visible_materials["visible_material_summary"]
    metadata["available_action_affordances"] = _action_affordances(capabilities)
    metadata["observed_environment_support"] = _observed_environment_support(metadata.get("environment_probe") if isinstance(metadata.get("environment_probe"), dict) else {})
    metadata["reviewer_probe_support"] = _reviewer_probe_support(metadata.get("environment_probe") if isinstance(metadata.get("environment_probe"), dict) else {}, capabilities)
    summary["visible_validation_surfaces"] = visible_validation_surfaces
    summary["declared_assets"] = visible_materials["declared_assets"]
    summary["visible_examples"] = visible_materials["visible_examples"]
    summary["visible_material_summary"] = visible_materials["visible_material_summary"]
    probe = metadata.get("environment_probe") if isinstance(metadata.get("environment_probe"), dict) else {}
    if network_scope == "unknown" and isinstance(probe, dict):
        network_probe = probe.get("network") if isinstance(probe.get("network"), dict) else {}
        status = str(network_probe.get("status", "")).strip()
        if status in {"probed_true", "probed_false"}:
            network_scope = "unenforced_probe_observation"

    return EnvMap(
        task_prompt=instruction_text,
        workspace_root=workspace_root,
        visible_files=visible_files,
        visible_dirs=visible_dirs,
        capabilities=capabilities,
        grader_hints={},
        resource_limits=dict(metadata.get("model_facing_resource_budget", {}) or {}),
        task_metadata=metadata,
        network_scope=network_scope,
        file_tree=_build_file_tree(visible_files, visible_dirs),
        file_map_summary=summary,
    )
