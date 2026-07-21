"""Harbor-style task mounting and production runtime wiring for Aether-2."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
import ast
import json
import os
import shutil
import subprocess
import sys
import types
import uuid

from harness.aether2.runtime.executor import ContainerBackend, ContainerExecutor
from harness.aether2.runtime.model_client import Aether2ModelClient
from harness.aether2.runtime.model_routes import (
    make_azure_gpt53_codex_route_from_env,
    make_azure_gpt54_mini_route_from_env,
    make_azure_gpt54_pro_route_from_env,
)
from harness.aether2.runtime.task_spec import TaskSpec


def _parse_toml_scalar(raw: str) -> object:
    value = raw.strip()
    if value.startswith(("'", '"')):
        return ast.literal_eval(value)
    if value in {"true", "false"}:
        return value == "true"
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def _load_tomllib() -> types.ModuleType:
    """Return a tomllib-compatible module, falling back to tomli or a minimal loader."""
    try:
        import tomllib as tomllib_module  # type: ignore[import-not-found]

        return tomllib_module
    except ModuleNotFoundError:
        pass

    try:
        import tomli as tomllib_module  # type: ignore[import-not-found]

        return tomllib_module
    except ModuleNotFoundError:
        pass

    tomllib_module = types.ModuleType("tomllib")

    def loads(text: str) -> dict[str, object]:
        data: dict[str, object] = {}
        current: dict[str, object] = data
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                path = [part.strip() for part in line[1:-1].split(".") if part.strip()]
                current = data
                for part in path:
                    next_value = current.get(part)
                    if not isinstance(next_value, dict):
                        next_value = {}
                        current[part] = next_value
                    current = next_value
                continue
            if "=" not in line:
                continue
            key, value = [part.strip() for part in line.split("=", 1)]
            current[key] = _parse_toml_scalar(value)
        return data

    def load(fp) -> dict[str, object]:
        return loads(fp.read())

    tomllib_module.loads = loads  # type: ignore[attr-defined]
    tomllib_module.load = load  # type: ignore[attr-defined]
    return tomllib_module


tomllib = sys.modules.get("tomllib") or _load_tomllib()
sys.modules.setdefault("tomllib", tomllib)


@dataclass(frozen=True)
class HarborRuntime:
    """The live model client and executor mounted for one Harbor-style task run."""

    model_client: Any
    executor: ContainerExecutor


class HarborRuntimeHandle(AbstractContextManager["HarborRuntime"]):
    """Context manager that owns the container lifecycle for one Aether-2 task run."""

    def __init__(self, runtime: HarborRuntime, *, container_id: str | None = None) -> None:
        self.runtime = runtime
        self.container_id = container_id

    def __enter__(self) -> HarborRuntime:
        return self.runtime

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Intentionally a no-op: Harbor grades the task AFTER the agent
        # process exits, and declared services/jobs/sessions must remain
        # running inside the container for that grading pass. Do NOT stop
        # the container or kill in-container process trees here. Container
        # lifecycle (if any cleanup is ever needed) is owned by the
        # orchestration layer, after grading completes, not by this harness.
        return False


class _MissingModelClient:
    def call(self, messages, tools, *, cache_prefix_len):
        raise RuntimeError("Aether-2 Harbor runtime could not construct a production model client from the environment.")


def run_task_via_harbor(task_dir: Path, loop_fn: Callable[..., Any], *, deadline_ts: float) -> Any:
    resolved_task_dir = task_dir.resolve()
    instruction_path = _find_first_existing(
        resolved_task_dir / "instruction.txt",
        resolved_task_dir / "instruction.md",
        resolved_task_dir / "task.md",
        resolved_task_dir / "prompt.txt",
    )
    if instruction_path is None:
        raise FileNotFoundError(f"no instruction file found in {resolved_task_dir}")

    workspace_root = resolved_task_dir / "workspace"
    artifacts_dir = resolved_task_dir / "artifacts"
    _prepare_workspace_dir(workspace_root)
    _prepare_artifacts_dir(artifacts_dir)

    task = TaskSpec(
        task_id=resolved_task_dir.name,
        instruction=instruction_path.read_text(encoding="utf-8"),
        task_dir=resolved_task_dir,
        workspace_root=workspace_root,
        artifacts_dir=artifacts_dir,
    )
    with _build_runtime(task) as runtime:
        result = loop_fn(task, runtime.model_client, runtime.executor, deadline_ts=deadline_ts)
        result = _attach_grader_reward(result, runtime.executor)
    _sync_workspace_artifacts(workspace_root, artifacts_dir)
    _write_result_artifact(artifacts_dir, result)
    _write_run_manifest(
        artifacts_dir,
        build_harbor_run_manifest(
            task,
            runtime_mode=getattr(getattr(runtime, "executor", None), "execution_boundary", "unknown"),
            result_summary=_result_summary(result),
        ),
    )
    _assert_artifacts_synced(artifacts_dir)
    return result


def _attach_grader_reward(result: Any, executor: ContainerExecutor) -> Any:
    """Populate `grader_reward` on a `RunResult`-like object from Harbor's reward file, if present.

    Harbor (when present) writes its grader's reward to `/logs/verifier/reward.txt` inside the
    task container/workspace, AFTER the agent's own run. This is advisory-verifier-independent
    grader authority (see C6): verifier readiness never masquerades as this value, and this value
    is `None` when no reward file exists (e.g. local homolog runs with no grader).
    """
    if not hasattr(result, "grader_reward"):
        return result
    reward_value: float | None = None
    try:
        reward_path = executor.resolve_workspace_path("logs/verifier/reward.txt")
    except ValueError:
        reward_path = None
    if reward_path is not None and reward_path.exists():
        try:
            reward_value = float(reward_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            reward_value = None
    if reward_value is None:
        return result
    try:
        from dataclasses import replace as _dc_replace

        return _dc_replace(result, grader_reward=reward_value)
    except TypeError:
        return result


def build_harbor_run_manifest(
    task: TaskSpec,
    *,
    runtime_mode: str,
    cleanup_scope: str = "attributable_resources_only",
    result_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_sync = {
        "source_root": str(task.workspace_root),
        "destination_root": str(task.artifacts_dir),
        "visible_files_only": True,
        "result_artifact": "result.json",
    }
    manifest: dict[str, Any] = {
        "manifest_type": "aether2_harbor_run_manifest",
        "manifest_version": 1,
        "execution_state": "complete" if result_summary is not None else "prepared",
        "task_id": task.task_id,
        "task_dir": str(task.task_dir),
        "workspace_root": str(task.workspace_root),
        "artifacts_dir": str(task.artifacts_dir),
        "runtime_mode": runtime_mode,
        "cleanup_scope": cleanup_scope,
        "artifact_sync": artifact_sync,
        "boundary": {
            "runtime_mode": runtime_mode,
            "task_dir": str(task.task_dir),
            "workspace_root": str(task.workspace_root),
            "artifacts_dir": str(task.artifacts_dir),
            "cleanup_scope": cleanup_scope,
            "artifact_sync": artifact_sync,
        },
    }
    if result_summary is not None:
        safe_result_summary = _json_safe(dict(result_summary))
        manifest["result_summary"] = safe_result_summary
        manifest["result_artifact_ref"] = "result.json"
        result_attribution: dict[str, Any] = {
            "execution_boundary": runtime_mode,
            "cleanup_scope": cleanup_scope,
            "result_artifact_ref": "result.json",
            "task_dir": str(task.task_dir),
            "workspace_root": str(task.workspace_root),
            "artifacts_dir": str(task.artifacts_dir),
            "visible_files_only": True,
            "official_grader_phase": "post_agent",
            "official_grader_agent_visible": False,
            "official_grader_authority": "external_measurement",
        }
        for field in (
            "verifier_readiness",
            "verifier_clean",
            "finalize_reason",
            "grader_reward",
            "reasoning_trace_ref",
            "job_survival",
            "session_survival",
            "no_delta_streaks",
            "verification_rounds",
            "recoveries",
            "compaction_count",
            "transcript_repairs",
            "pass_",
            "status",
        ):
            value = safe_result_summary.get(field)
            if value is not None:
                result_attribution[field] = value
        manifest["result_attribution"] = result_attribution
    return manifest


def _result_field(result: Any, field: str) -> Any:
    if isinstance(result, Mapping) and field in result:
        return result[field]
    return getattr(result, field, None)


def _result_summary(result: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for field in (
        "verifier_readiness",
        "verifier_clean",
        "finalize_reason",
        "summary",
        "steps",
        "model_calls",
        "grader_reward",
        "reasoning_trace_ref",
        "job_survival",
        "session_survival",
        "no_delta_streaks",
        "verification_rounds",
        "recoveries",
        "compaction_count",
        "transcript_repairs",
    ):
        value = _result_field(result, field)
        if value is not None:
            summary[field] = value
    return summary


def _write_run_manifest(artifacts_dir: Path, manifest: Mapping[str, Any]) -> None:
    manifest_path = artifacts_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(_json_safe(dict(manifest)), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_runtime(task: TaskSpec) -> HarborRuntimeHandle:
    container_image = _task_container_image(task.task_dir)
    if container_image:
        return _build_container_runtime(task, container_image=container_image)
    return _build_local_runtime(task)


def _build_container_runtime(task: TaskSpec, *, container_image: str) -> HarborRuntimeHandle:
    container_name = f"aether2-{task.task_id}-{uuid.uuid4().hex[:10]}"
    run_command = _docker_run_command(task, container_name=container_name, container_image=container_image)
    completed = subprocess.run(run_command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 and (task.task_dir / "Dockerfile").exists():
        build = subprocess.run(
            ["docker", "build", "-t", container_image, str(task.task_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if build.returncode != 0:
            raise RuntimeError(f"failed to build task container: {build.stderr.strip()}")
        completed = subprocess.run(run_command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"failed to start task container: {completed.stderr.strip()}")
    container_id = completed.stdout.strip()
    if not container_id:
        raise RuntimeError("failed to start task container: empty container id")

    executor = ContainerExecutor(
        workspace_root=task.workspace_root,
        backend=ContainerBackend(
            kind="docker",
            container_id=container_id,
            container_workspace_root="/app",
        ),
    )
    return HarborRuntimeHandle(
        HarborRuntime(
            model_client=_build_model_client(),
            executor=executor,
        ),
        container_id=container_id,
    )


def _docker_run_command(task: TaskSpec, *, container_name: str, container_image: str) -> list[str]:
    run_command = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "-w",
        "/app",
        "-v",
        f"{task.workspace_root}:{'/app'}",
        container_image,
        "sleep",
        "infinity",
    ]
    return run_command


def _build_local_runtime(task: TaskSpec) -> HarborRuntimeHandle:
    executor = ContainerExecutor(workspace_root=task.workspace_root)
    return HarborRuntimeHandle(
        HarborRuntime(
            model_client=_build_model_client(),
            executor=executor,
        )
    )


def _build_model_client() -> Any:
    try:
        deployment_hint = str(
            os.environ.get("AETHER2_MODEL_TIER")
            or os.environ.get("AETHER2_MODEL")
            or os.environ.get("AZURE_OPENAI_GPT54_PRO_DEPLOYMENT")
            or os.environ.get("AZURE_OPENAI_GPT54_MINI_DEPLOYMENT")
            or os.environ.get("AZURE_OPENAI_GPT53_CODEX_DEPLOYMENT")
            or ""
        ).lower()
        if "5.3" in deployment_hint or "codex" in deployment_hint:
            route = make_azure_gpt53_codex_route_from_env()
        elif "pro" in deployment_hint and ("5.4" in deployment_hint or "gpt54" in deployment_hint):
            route = make_azure_gpt54_pro_route_from_env()
        else:
            route = make_azure_gpt54_mini_route_from_env()
        return Aether2ModelClient(route)
    except Exception:
        return _MissingModelClient()


def _task_container_image(task_dir: Path) -> str | None:
    task_toml = task_dir / "task.toml"
    if not task_toml.exists():
        return None
    data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    environment = data.get("environment")
    if not isinstance(environment, dict):
        return None
    image = environment.get("docker_image")
    if not isinstance(image, str) or not image.strip():
        return None
    return image.strip()


def _find_first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists() and path.is_file():
            return path
    return None


def _write_result_artifact(artifacts_dir: Path, result: Any) -> None:
    result_path = artifacts_dir / "result.json"
    payload = _json_safe(result)
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _prepare_workspace_dir(workspace_root: Path) -> None:
    # Never delete pre-existing files: Harbor task fixtures may already be
    # staged in the workspace directory. Create-if-missing only.
    workspace_root.mkdir(parents=True, exist_ok=True)


def _prepare_artifacts_dir(artifacts_dir: Path) -> None:
    # Never delete pre-existing files: create-if-missing only (see
    # _prepare_workspace_dir).
    artifacts_dir.mkdir(parents=True, exist_ok=True)


def _sync_workspace_artifacts(workspace_root: Path, artifacts_dir: Path) -> None:
    for path in sorted(workspace_root.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(workspace_root)
        destination = artifacts_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def _assert_artifacts_synced(artifacts_dir: Path) -> None:
    files = [path for path in artifacts_dir.rglob("*") if path.is_file()]
    if not files:
        raise RuntimeError(f"incomplete artifact sync-back: no files found under {artifacts_dir}")
    if not any(path.name == "result.json" for path in files):
        raise RuntimeError(f"incomplete artifact sync-back: missing result.json under {artifacts_dir}")
    if not any(_is_visible_synced_artifact(path, artifacts_dir) for path in files):
        raise RuntimeError(f"incomplete artifact sync-back: no visible synced task artifacts under {artifacts_dir}")
    for path in files:
        if path.stat().st_size <= 0:
            raise RuntimeError(f"incomplete artifact sync-back: empty artifact {path}")


_HARNESS_GENERATED_ARTIFACT_NAMES = {"result.json", "run_manifest.json"}


def _is_visible_synced_artifact(path: Path, artifacts_dir: Path) -> bool:
    if path.name in _HARNESS_GENERATED_ARTIFACT_NAMES:
        return False
    relative_parts = path.relative_to(artifacts_dir).parts
    return all(not part.startswith(".") for part in relative_parts)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))
    if isinstance(value, Path):
        return os.fspath(value)
    return str(value)


__all__ = ["HarborRuntime", "TaskSpec", "build_harbor_run_manifest", "run_task_via_harbor"]
