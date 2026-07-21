"""Native Letta bridge preflight against official letta-evals suite."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from harness.aether2.runtime.azure_openai_env import build_openai_compatible_azure_gpt54_mini_env

REPO_ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_ROOT = REPO_ROOT / "research/sources/codebases/letta-evals"
DEFAULT_SUITE_YAML = UPSTREAM_ROOT / "letta-leaderboard/filesystem-agent/filesystem_code.yaml"
OFFICIAL_JUDGE_MODEL = "gpt-5-mini"
OFFICIAL_NATIVE_AUTHORITY_LABEL = "official_native"
AZURE_EQUIVALENT_AUTHORITY_LABEL = "azure_equivalent"


def native_preflight(
    *,
    upstream_root: Path = UPSTREAM_ROOT,
    suite_yaml: Path = DEFAULT_SUITE_YAML,
    python_executable: str = sys.executable,
) -> dict[str, Any]:
    runtime_blockers, cli_path, probe = _runtime_status(upstream_root, suite_yaml, python_executable)
    azure_judge = build_openai_compatible_azure_gpt54_mini_env()
    official_native = _official_native_status(runtime_blockers, azure_judge)
    azure_equivalent = _azure_equivalent_status(runtime_blockers, azure_judge)
    return {
        "native_runtime_available": official_native["ready"],
        "blocker_codes": official_native["blockers"],
        "upstream_root": str(upstream_root),
        "suite_yaml": str(suite_yaml),
        "letta_evals_cli": cli_path,
        "python_executable": python_executable,
        "present_env": _present_env(azure_judge),
        "python_probe_stdout": (probe.stdout or "").strip(),
        "python_probe_stderr": (probe.stderr or "").strip(),
        "official_native": official_native,
        "azure_equivalent": azure_equivalent,
        "azure_openai_judge_route": azure_judge,
    }


def write_azure_equivalent_suite(
    *, suite_yaml: Path = DEFAULT_SUITE_YAML, output_path: Path, azure_deployment: str
) -> Path:
    suite_dir = suite_yaml.parent
    generated = deepcopy(_load_yaml(suite_yaml))
    generated["name"] = f"{generated.get('name', 'letta-suite')}-azure-equivalent"
    description = str(generated.get("description", "")).strip()
    generated["description"] = f"{description} [azure-equivalent judge route]".strip()
    generated["dataset"] = str((suite_dir / str(generated["dataset"])).resolve())
    target = generated.setdefault("target", {})
    if target.get("working_dir"):
        target["working_dir"] = str((suite_dir / str(target["working_dir"])).resolve())
    rubric = generated.setdefault("graders", {}).setdefault("rubric_check", {})
    if rubric.get("prompt_path"):
        rubric["prompt_path"] = str((suite_dir / str(rubric["prompt_path"])).resolve())
    rubric["provider"] = "openai"
    rubric["model"] = azure_deployment
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _dump_yaml(output_path, generated)
    return output_path


def _runtime_status(upstream_root: Path, suite_yaml: Path, python_executable: str) -> tuple[list[str], str, Any]:
    blockers: list[str] = []
    if not upstream_root.exists() or not suite_yaml.exists():
        blockers.append("missing_letta_eval_assets")
    cli_path = shutil.which("letta-evals")
    if cli_path is None:
        candidate = Path(python_executable).resolve().parent / "letta-evals"
        if candidate.exists():
            cli_path = str(candidate)
    probe = subprocess.run(
        [python_executable, "-c", "import yaml, letta_evals; print('ok')"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if cli_path is None:
        blockers.append("missing_letta_evals_cli")
    if probe.returncode != 0:
        blockers.append("missing_letta_python_runtime_dependencies")
    return blockers, cli_path or "", probe


def _present_env(azure_judge: dict[str, Any]) -> list[str]:
    present = [name for name in ("LETTA_API_KEY", "LETTA_PROJECT_ID", "OPENAI_API_KEY") if os.environ.get(name)]
    for env_name in azure_judge.get("source_env_names", {}).values():
        if isinstance(env_name, str) and os.environ.get(env_name) and env_name not in present:
            present.append(env_name)
    return present


def _official_native_status(runtime_blockers: list[str], azure_judge: dict[str, Any]) -> dict[str, Any]:
    blockers = list(runtime_blockers)
    if not os.environ.get("LETTA_API_KEY"):
        blockers.append("missing_env_letta_api_key")
    if not os.environ.get("LETTA_PROJECT_ID"):
        blockers.append("missing_env_letta_project_id")
    if not _official_openai_judge_available(azure_judge):
        blockers.append("missing_official_openai_judge_credentials")
    return _status_payload(
        authority_label=OFFICIAL_NATIVE_AUTHORITY_LABEL,
        blockers=blockers,
        judge_route=(
            "openai_env_or_matching_azure_deployment"
            if _official_openai_judge_available(azure_judge)
            else "unavailable"
        ),
    )


def _azure_equivalent_status(runtime_blockers: list[str], azure_judge: dict[str, Any]) -> dict[str, Any]:
    blockers = list(runtime_blockers)
    if not os.environ.get("LETTA_API_KEY"):
        blockers.append("missing_env_letta_api_key")
    if not os.environ.get("LETTA_PROJECT_ID"):
        blockers.append("missing_env_letta_project_id")
    if not azure_judge.get("available"):
        blockers.append("missing_azure_openai_judge_route")
    return _status_payload(
        authority_label=AZURE_EQUIVALENT_AUTHORITY_LABEL,
        blockers=blockers,
        judge_route="azure_openai_openai_compatible_v1" if azure_judge.get("available") else "unavailable",
    )


def _status_payload(*, authority_label: str, blockers: list[str], judge_route: str) -> dict[str, Any]:
    return {
        "authority_label": authority_label,
        "ready": not blockers,
        "blockers": blockers,
        "judge_route": judge_route,
    }


def _official_openai_judge_available(azure_judge: dict[str, Any]) -> bool:
    if os.environ.get("OPENAI_API_KEY"):
        return True
    return azure_judge.get("available") and azure_judge.get("deployment_name") == OFFICIAL_JUDGE_MODEL


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        return deepcopy(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ModuleNotFoundError:
        ruby = shutil.which("ruby")
        if not ruby:
            raise RuntimeError("PyYAML and ruby are unavailable; cannot read Letta suite yaml")
        completed = subprocess.run(
            [ruby, "-ryaml", "-rjson", "-e", "print JSON.generate(YAML.load_file(ARGV[0]))", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)


def _dump_yaml(path: Path, payload: dict[str, Any]) -> None:
    try:
        import yaml  # type: ignore

        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    except ModuleNotFoundError:
        ruby = shutil.which("ruby")
        if not ruby:
            raise RuntimeError("PyYAML and ruby are unavailable; cannot write Letta suite yaml")
        completed = subprocess.run(
            [ruby, "-ryaml", "-rjson", "-e", "print YAML.dump(JSON.parse(ARGV[0]))", json.dumps(payload)],
            capture_output=True,
            text=True,
            check=True,
        )
        path.write_text(completed.stdout, encoding="utf-8")
