"""Compile safe typed visible smoke-test specs into harness check specs.

The architect may propose only typed specs.  This module turns a narrow subset
into compiler-generated commands.  Unsupported or under-specified specs are
reported as rejected items and never become authority.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Iterable

from .runtime_ir import CheckSpec, EnvMap, normalize_relpath
from .workbench_config import HarnessConfigIR


@dataclass(frozen=True)
class SmokeCompileResult:
    checks: tuple[CheckSpec, ...]
    rejected: tuple[dict[str, Any], ...]


def compile_visible_smoke_tests(config: HarnessConfigIR, envmap: EnvMap) -> SmokeCompileResult:
    checks: list[CheckSpec] = []
    rejected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, spec in enumerate(config.verification_policy.visible_smoke_tests):
        check, reason = _compile_one(spec, idx, envmap)
        if check is None:
            rejected.append({
                "status": "not_compiled",
                "path": f"verification_policy.visible_smoke_tests[{idx}]",
                "reason_code": reason[0],
                "message": reason[1],
                "original_item": dict(spec),
            })
            continue
        check_id = check.check_id
        if check_id in seen_ids:
            rejected.append({
                "status": "not_compiled",
                "path": f"verification_policy.visible_smoke_tests[{idx}]",
                "reason_code": "duplicate_visible_smoke_check_id",
                "message": f"Duplicate compiled smoke check id {check_id}.",
                "original_item": dict(spec),
            })
            continue
        seen_ids.add(check_id)
        checks.append(check)
    return SmokeCompileResult(checks=tuple(checks), rejected=tuple(rejected))


def _compile_one(spec: dict[str, Any], idx: int, envmap: EnvMap) -> tuple[CheckSpec | None, tuple[str, str]]:
    smoke_type = str(spec.get("type", "")).strip()
    if smoke_type == "syntax_check":
        return _compile_syntax_check(spec, idx, envmap)
    if smoke_type == "content_assertion":
        return _compile_content_assertion(spec, idx, envmap)
    if smoke_type == "file_exists":
        return _compile_file_exists(spec, idx, envmap)
    if smoke_type == "file_size":
        return _compile_file_size(spec, idx, envmap)
    if smoke_type == "run_deliverable_on_fixture":
        return _compile_fixture_run(spec, idx, envmap)
    return None, ("unsupported_visible_smoke_test_type", f"Unsupported visible smoke-test type {smoke_type!r}.")


def _compile_syntax_check(spec: dict[str, Any], idx: int, envmap: EnvMap) -> tuple[CheckSpec | None, tuple[str, str]]:
    path = _path(spec, envmap)
    if not path:
        return None, ("visible_smoke_missing_path", "syntax_check requires path.")
    language = str(spec.get("language") or _language_from_path(path)).lower().strip()
    quoted = shlex.quote(path)
    if language in {"python", "py"}:
        python_cmd = _python_command(envmap)
        if not python_cmd:
            return None, ("visible_smoke_python_unavailable", "syntax_check for Python requires a probed Python interpreter.")
        command = f"{shlex.quote(python_cmd)} -m py_compile {quoted}"
    elif language in {"javascript", "js", "node"}:
        command = f"node --check {quoted}"
    elif language == "json":
        python_cmd = _python_command(envmap)
        if not python_cmd:
            return None, ("visible_smoke_python_unavailable", "json syntax_check requires a probed Python interpreter.")
        command = _python_json_load_command(path, python_cmd=python_cmd)
    else:
        return None, ("visible_smoke_unsupported_syntax_language", f"Unsupported syntax_check language {language!r}.")
    return _check(idx, "syntax_check", path, command), ("", "")


def _compile_content_assertion(spec: dict[str, Any], idx: int, envmap: EnvMap) -> tuple[CheckSpec | None, tuple[str, str]]:
    path = _path(spec, envmap)
    if not path:
        return None, ("visible_smoke_missing_path", "content_assertion requires path.")
    contains = spec.get("contains")
    not_contains = spec.get("not_contains")
    if contains is None and not_contains is None:
        return None, ("visible_smoke_missing_assertion", "content_assertion requires contains or not_contains.")
    python_cmd = _python_command(envmap)
    if not python_cmd:
        return None, ("visible_smoke_python_unavailable", "content_assertion requires a probed Python interpreter.")
    command = _python_content_assertion_command(path, contains=contains, not_contains=not_contains, python_cmd=python_cmd)
    return _check(idx, "content_assertion", path, command), ("", "")


def _compile_file_exists(spec: dict[str, Any], idx: int, envmap: EnvMap) -> tuple[CheckSpec | None, tuple[str, str]]:
    path = _path(spec, envmap)
    if not path:
        return None, ("visible_smoke_missing_path", "file_exists requires path.")
    python_cmd = _python_command(envmap)
    if not python_cmd:
        return None, ("visible_smoke_python_unavailable", "file_exists requires a probed Python interpreter.")
    command = _python_file_probe_command(path, min_bytes=0, python_cmd=python_cmd)
    return _check(idx, "file_exists", path, command), ("", "")


def _compile_file_size(spec: dict[str, Any], idx: int, envmap: EnvMap) -> tuple[CheckSpec | None, tuple[str, str]]:
    path = _path(spec, envmap)
    if not path:
        return None, ("visible_smoke_missing_path", "file_size requires path.")
    try:
        min_bytes = int(spec.get("min_bytes", 1))
    except (TypeError, ValueError):
        return None, ("visible_smoke_invalid_min_bytes", "file_size min_bytes must be an integer.")
    python_cmd = _python_command(envmap)
    if not python_cmd:
        return None, ("visible_smoke_python_unavailable", "file_size requires a probed Python interpreter.")
    command = _python_file_probe_command(path, min_bytes=max(0, min_bytes), python_cmd=python_cmd)
    return _check(idx, "file_size", path, command), ("", "")


def _compile_fixture_run(spec: dict[str, Any], idx: int, envmap: EnvMap) -> tuple[CheckSpec | None, tuple[str, str]]:
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item.strip() for item in argv):
        return None, ("visible_smoke_missing_argv", "run_deliverable_on_fixture requires argv: list[str].")
    if any(_looks_shellish(item) for item in argv):
        return None, ("visible_smoke_shellish_argv", "run_deliverable_on_fixture argv must not contain shell metacharacters.")
    python_cmd = _python_command(envmap)
    if not python_cmd:
        return None, ("visible_smoke_python_unavailable", "run_deliverable_on_fixture requires a probed Python interpreter.")
    command = _python_subprocess_argv_command(tuple(argv), stdin_file=_optional_path(spec.get("stdin_file"), envmap), python_cmd=python_cmd)
    label_path = normalize_relpath(str(argv[0]), envmap.workspace_root)
    return _check(idx, "run_deliverable_on_fixture", label_path, command), ("", "")


def _check(idx: int, smoke_type: str, path: str, command: str) -> CheckSpec:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", path).strip("-")[:40] or str(idx)
    # Shape-only: existence/size/syntax/content-literal checks prove that a
    # deliverable has the right surface, never that it is semantically
    # correct.  Presenting them as authoritative biases the solver toward
    # "green enough" (observed live: log-summary-date-ranges shipped wrong
    # counts behind passing shape checks).
    return CheckSpec(
        check_id=f"visible-smoke:{idx}:{smoke_type}:{safe}",
        label=f"visible smoke (shape-only, not semantic proof) {smoke_type}: {path}",
        command=command,
        origin="visible_smoke",
        authoritative=False,
    )


def _path(spec: dict[str, Any], envmap: EnvMap) -> str:
    return _optional_path(spec.get("path") or spec.get("artifact_path"), envmap)


def _optional_path(value: Any, envmap: EnvMap) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return normalize_relpath(value, envmap.workspace_root)


def _language_from_path(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".py"):
        return "python"
    if lower.endswith(".js") or lower.endswith(".mjs") or lower.endswith(".cjs"):
        return "javascript"
    if lower.endswith(".json"):
        return "json"
    return ""


def _python_command(envmap: EnvMap) -> str:
    probe = envmap.task_metadata.get("environment_probe", {}) if isinstance(envmap.task_metadata, dict) else {}
    if isinstance(probe, dict):
        guidance = probe.get("validation_guidance", {})
        if isinstance(guidance, dict):
            preferred = str(guidance.get("preferred_python", "")).strip()
            if preferred:
                return preferred
        python = probe.get("python", {})
        if isinstance(python, dict):
            preferred = str(python.get("preferred", "")).strip()
            if preferred:
                return preferred
            interpreters = python.get("interpreters", ())
            if isinstance(interpreters, list) and interpreters:
                return str(interpreters[0])
        commands = probe.get("command_names", {})
        if isinstance(commands, dict):
            for name in ("python3", "python"):
                item = commands.get(name, {})
                if isinstance(item, dict) and item.get("available"):
                    return name
    if not probe:
        return "python3"
    return ""


def _python_json_load_command(path: str, *, python_cmd: str) -> str:
    code = "import json,pathlib,sys; json.loads(pathlib.Path(sys.argv[1]).read_text())"
    return f"{shlex.quote(python_cmd)} -c {shlex.quote(code)} {shlex.quote(path)}"


def _python_file_probe_command(path: str, *, min_bytes: int, python_cmd: str) -> str:
    code = (
        "import pathlib,sys; "
        "p=pathlib.Path(sys.argv[1]); min_bytes=int(sys.argv[2]); "
        "assert p.exists(), f'missing {p}'; "
        "assert p.stat().st_size >= min_bytes, f'{p} has {p.stat().st_size} bytes < {min_bytes}'"
    )
    return f"{shlex.quote(python_cmd)} -c {shlex.quote(code)} {shlex.quote(path)} {min_bytes}"


def _python_content_assertion_command(path: str, *, contains: Any, not_contains: Any, python_cmd: str) -> str:
    code = (
        "import pathlib,sys,json; "
        "p=pathlib.Path(sys.argv[1]); s=p.read_text(errors='replace'); "
        "cfg=json.loads(sys.argv[2]); "
        "missing=[x for x in cfg.get('contains',[]) if x not in s]; "
        "present=[x for x in cfg.get('not_contains',[]) if x in s]; "
        "assert not missing and not present, f'missing={missing} present={present}'"
    )
    cfg = {
        "contains": _string_list(contains),
        "not_contains": _string_list(not_contains),
    }
    return f"{shlex.quote(python_cmd)} -c {shlex.quote(code)} {shlex.quote(path)} {shlex.quote(json.dumps(cfg))}"


def _python_subprocess_argv_command(argv: tuple[str, ...], *, stdin_file: str = "", python_cmd: str = "python3") -> str:
    code = (
        "import json,subprocess,sys,pathlib; "
        "argv=json.loads(sys.argv[1]); stdin=None; "
        "p=sys.argv[2] if len(sys.argv)>2 else ''; "
        "stdin=pathlib.Path(p).read_bytes() if p else None; "
        "raise SystemExit(subprocess.run(argv,input=stdin).returncode)"
    )
    parts = [shlex.quote(python_cmd), "-c", shlex.quote(code), shlex.quote(json.dumps(list(argv)))]
    if stdin_file:
        parts.append(shlex.quote(stdin_file))
    return " ".join(parts)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _looks_shellish(value: str) -> bool:
    return any(token in value for token in (";", "&&", "||", "|", "`", "$(`", ">", "<"))
