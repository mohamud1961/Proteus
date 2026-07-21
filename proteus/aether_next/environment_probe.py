"""Environment probing helpers for task-local architect context."""
from __future__ import annotations

import json
import shlex
from typing import Any

from .execution import Executor

COMMAND_PROBE_NAMES: tuple[str, ...] = (
    "python", "python3", "pip", "pip3", "uv", "ps", "pgrep",
    "node", "npm", "git",
    "openssl", "curl", "wget",
    "ffmpeg", "ffprobe", "tesseract", "pdftotext", "convert", "magick",
    "qemu-system-i386", "qemu-system-x86_64", "qemu-img",
    "ssh", "sshd", "telnet", "nginx", "grpcurl",
    "pytest", "make", "cmake", "gcc", "g++", "clang", "clang++", "pkg-config",
    "cargo", "rustc", "ocaml", "opam", "dune", "coqtop", "coqc",
    "sqlite3", "R", "Rscript", "java", "julia", "octave",
    "file", "strings", "readelf", "objdump", "gdb", "xxd", "hexdump",
)
_HINT_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "python": ("python", "python3"),
    "python3": ("python3", "python"),
    "pip": ("pip", "pip3", "python3"),
    "r": ("R", "Rscript"),
    "rscript": ("Rscript", "R"),
    "qemu": ("qemu-system-i386", "qemu-system-x86_64", "qemu-img"),
    "ssh": ("ssh", "sshd"),
    "image": ("ffmpeg", "ffprobe", "tesseract", "pdftotext", "convert", "magick"),
    "video": ("ffmpeg", "ffprobe"),
    "ocr": ("tesseract", "pdftotext"),
    "compiler": ("make", "cmake", "gcc", "g++", "clang", "clang++", "pkg-config"),
    "rust": ("cargo", "rustc"),
    "ocaml": ("ocaml", "opam", "dune"),
    "coq": ("coqtop", "coqc"),
    "binary": ("file", "strings", "readelf", "objdump", "gdb", "xxd", "hexdump"),
}


def probe_environment(
    executor: Executor,
    *,
    workspace_root: str = "/app",
    extra_command_names: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Return bounded, task-local environment facts for architect/solver setup.

    The probe intentionally captures command availability and interpreter/module
    basics only. It is evidence for harness configuration, not a hidden grader.
    """
    command_probe_names = _command_probe_names(extra_command_names)
    command_names = _probe_commands(
        executor,
        workspace_root=workspace_root,
        command_probe_names=command_probe_names,
    )
    python = _probe_python(executor, workspace_root=workspace_root, command_names=command_names)
    network = _probe_network(executor, workspace_root=workspace_root, command_names=command_names)
    if isinstance(python, dict):
        python["package_contract"] = _merge_package_contract(
            python.get("interpreter_details", {}) if isinstance(python.get("interpreter_details"), dict) else {},
            command_names,
            network_status=str(network.get("status", "unknown")) if isinstance(network, dict) else "unknown",
        )
    return {
        "schema_version": "environment_probe.v1",
        "workspace_root": workspace_root,
        "command_names": command_names,
        "python": python,
        "network": network,
        "task_hints": {
            "requested_command_names": list(_requested_command_names(extra_command_names)),
            "missing_requested_commands": [
                name
                for name in _requested_command_names(extra_command_names)
                if not _request_satisfied(name, command_names)
            ],
        },
        "validation_guidance": _validation_guidance(command_names, python),
    }


def _command_probe_names(extra_command_names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    names = list(COMMAND_PROBE_NAMES)
    seen = set(names)
    for raw in _expanded_command_names(extra_command_names):
        name = str(raw).strip()
        if not name or "/" in name or len(name) > 64:
            continue
        if not shlex.quote(name) or name in seen:
            continue
        if not all(ch.isalnum() or ch in "._+-" for ch in name):
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= len(COMMAND_PROBE_NAMES) + 12:
            break
    return tuple(names)


def _expanded_command_names(extra_command_names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    expanded: list[str] = []
    seen: set[str] = set()
    for raw in extra_command_names:
        name = str(raw).strip()
        if not name:
            continue
        for candidate in _HINT_EXPANSIONS.get(name.lower(), (name,)):
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return tuple(expanded)


def _requested_command_names(extra_command_names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    requested: list[str] = []
    seen: set[str] = set()
    for raw in extra_command_names:
        name = str(raw).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        requested.append(name)
    return tuple(requested)


def _request_satisfied(name: str, command_names: dict[str, dict[str, Any]]) -> bool:
    candidates = _HINT_EXPANSIONS.get(name.lower(), (name,))
    return any(command_names.get(candidate, {}).get("available") for candidate in candidates)


def _probe_commands(
    executor: Executor,
    *,
    workspace_root: str,
    command_probe_names: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    quoted = " ".join(shlex.quote(name) for name in command_probe_names)
    script = (
        "for c in " + quoted + "; do "
        "p=$(command -v \"$c\" 2>/dev/null || true); "
        "if [ -n \"$p\" ]; then printf '%s\\t%s\\n' \"$c\" \"$p\"; else printf '%s\\t\\n' \"$c\"; fi; "
        "done"
    )
    result = executor.run_command(script, cwd=workspace_root, timeout_s=15)
    commands: dict[str, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        name, path = line.split("\t", 1)
        commands[name] = {"available": bool(path.strip()), "path": path.strip()}
    for name in command_probe_names:
        commands.setdefault(name, {"available": False, "path": ""})
    return commands


def _probe_python(
    executor: Executor,
    *,
    workspace_root: str,
    command_names: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    interpreters = [
        name for name in ("python3", "python")
        if command_names.get(name, {}).get("available")
    ]
    modules = (
        "pytest", "cryptography", "rdflib", "numpy", "scipy", "pandas", "sklearn",
        "PIL", "cv2", "torch", "grpc", "grpc_tools", "fasttext", "pyarrow",
        "toml", "matplotlib",
    )
    results: dict[str, Any] = {"preferred": interpreters[0] if interpreters else "", "interpreters": interpreters}
    module_status: dict[str, dict[str, Any]] = {}
    for interpreter in interpreters[:2]:
        code = (
            "import importlib.util,json,sys; "
            f"mods={json.dumps(list(modules))}; "
            "print(json.dumps({'executable':sys.executable,'version':sys.version.split()[0],"
            "'modules':{m:bool(importlib.util.find_spec(m)) for m in mods}}))"
        )
        result = executor.run_command(
            f"{shlex.quote(interpreter)} -c {shlex.quote(code)}",
            cwd=workspace_root,
            timeout_s=15,
        )
        if result.success:
            try:
                parsed = json.loads(result.stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError):
                continue
            results[interpreter] = parsed
            for mod, available in dict(parsed.get("modules", {})).items():
                current = module_status.setdefault(str(mod), {"available_in": []})
                if available:
                    current["available_in"].append(interpreter)
            details = results.setdefault("interpreter_details", {})
            if isinstance(details, dict):
                details[interpreter] = _python_package_contract(
                    executor,
                    workspace_root=workspace_root,
                    interpreter=interpreter,
                    command_names=command_names,
                )
    for mod in modules:
        item = module_status.setdefault(mod, {"available_in": []})
        item["available"] = bool(item["available_in"])
    results["modules"] = module_status
    results["package_contract"] = _merge_package_contract(
        results.get("interpreter_details", {}) if isinstance(results.get("interpreter_details"), dict) else {},
        command_names,
        network_status="unknown",
    )
    return results


def _python_package_contract(
    executor: Executor,
    *,
    workspace_root: str,
    interpreter: str,
    command_names: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    code = """
import json, os, site, sys, sysconfig
paths = []
try:
    paths.extend(site.getsitepackages())
except Exception:
    pass
try:
    paths.append(site.getusersitepackages())
except Exception:
    pass
stdlib = sysconfig.get_path("stdlib") or ""
externally_managed = os.path.exists(os.path.join(stdlib, "EXTERNALLY-MANAGED"))
print(json.dumps({
    "executable": sys.executable,
    "version": sys.version.split()[0],
    "site_packages": paths,
    "site_packages_writable": any(os.access(path, os.W_OK) for path in paths if path),
    "externally_managed": externally_managed,
}))
""".strip()
    result = executor.run_command(
        f"{shlex.quote(interpreter)} -c {shlex.quote(code)}",
        cwd=workspace_root,
        timeout_s=10,
    )
    parsed: dict[str, Any] = {}
    if result.success:
        try:
            parsed = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            parsed = {}
    parsed["pip_commands_available"] = [
        name for name in ("pip", "pip3") if command_names.get(name, {}).get("available")
    ]
    parsed["pip_module_available"] = _pip_module_available(
        executor,
        workspace_root=workspace_root,
        interpreter=interpreter,
    )
    return parsed


def _pip_module_available(executor: Executor, *, workspace_root: str, interpreter: str) -> bool:
    result = executor.run_command(
        f"{shlex.quote(interpreter)} -m pip --version >/dev/null 2>&1",
        cwd=workspace_root,
        timeout_s=10,
    )
    return result.exit_code == 0


def _merge_package_contract(
    interpreter_details: dict[str, Any],
    command_names: dict[str, dict[str, Any]],
    *,
    network_status: str,
) -> dict[str, Any]:
    preferred = next(iter(interpreter_details), "")
    detail = interpreter_details.get(preferred, {}) if preferred else {}
    pip_commands = [
        name for name in ("pip", "pip3") if command_names.get(name, {}).get("available")
    ]
    pip_available = bool(pip_commands or detail.get("pip_module_available"))
    if network_status == "unknown":
        package_install_status = "unknown"
    elif network_status == "probed_true" and pip_available:
        package_install_status = "probed_possible"
    else:
        package_install_status = "probed_unavailable"
    return {
        "preferred_python": preferred,
        "pip_available": pip_available,
        "pip_commands_available": pip_commands,
        "pip_interpreter": preferred if detail.get("pip_module_available") else "",
        "site_packages_writable": detail.get("site_packages_writable", "unknown"),
        "externally_managed_python": detail.get("externally_managed", "unknown"),
        "package_install_status": package_install_status,
    }


def _probe_network(
    executor: Executor,
    *,
    workspace_root: str,
    command_names: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Probe external network egress without inventing a default.

    Returns status=probed_true, probed_false, or unknown.  A failed probe is
    not a semantic task fact; it is just evidence about this live container.
    """
    if command_names.get("curl", {}).get("available"):
        command = "curl -Is --max-time 5 https://example.com >/dev/null"
    elif command_names.get("wget", {}).get("available"):
        command = "wget -q --spider --timeout=5 https://example.com"
    else:
        return {
            "status": "unknown",
            "value": "unknown",
            "probe_method": "no curl/wget available",
            "command": "",
            "exit_code": None,
            "detail": "network egress not probed because no bounded HTTP probe command was available",
        }
    result = executor.run_command(command, cwd=workspace_root, timeout_s=8)
    return {
        "status": "probed_true" if result.exit_code == 0 else "probed_false",
        "value": "open_external_network" if result.exit_code == 0 else "probed_no_external_network",
        "probe_method": "bounded http HEAD/spider to example.com",
        "command": command,
        "exit_code": result.exit_code,
        "stdout_excerpt": result.stdout[:500],
        "stderr_excerpt": result.stderr[:500],
    }

def _validation_guidance(command_names: dict[str, dict[str, Any]], python: dict[str, Any]) -> dict[str, Any]:
    preferred_python = str(python.get("preferred", ""))
    missing = [
        name for name, info in sorted(command_names.items())
        if not info.get("available")
    ]
    guidance: list[str] = []
    if preferred_python:
        guidance.append(f"Prefer {preferred_python} for Python checks; do not assume bare python exists.")
    else:
        guidance.append("No Python interpreter was detected by the probe; avoid Python-only validation unless bootstrapped.")
    if "python" in missing and "python3" not in missing:
        guidance.append("Bare python is unavailable but python3 is available; use python3 in executable checks.")
    if not python.get("modules", {}).get("cryptography", {}).get("available", False):
        guidance.append("The cryptography module is not available in detected Python interpreters; deliverable scripts should avoid importing it unless the solver installs and verifies it.")
    return {
        "preferred_python": preferred_python,
        "missing_commands": missing,
        "notes": guidance,
    }
