"""Env-analysis: objective graph construction, eval indexing, and helpers.

Extracted from compiler.py to stay under the 500-LOC module cap.
"""
from __future__ import annotations

from hashlib import sha256
import os
import re
from typing import Iterable

from .runtime_ir import (
    CheckSpec,
    DeliverableSpec,
    EnvMap,
    EvalIndex,
    MetricThreshold,
    ObjectiveGraph,
    ProofObligation,
    ServiceRequirement,
    normalize_relpath,
)

_VERIFY_COMMAND_RE = re.compile(
    r"(?:you can run|run)\s+`?([^`\n]+?)`?\s+(?:to verify|to test)",
    re.IGNORECASE,
)
_PROMPT_DELIVERABLE_RE = re.compile(
    r"(?:write|create|produce|save|output|submit)\s+`?(/?[\w./-]+\.[A-Za-z0-9]+)`?",
    re.IGNORECASE,
)
_CHECK_FILENAME_RE = re.compile(
    r"(^|/)(tests?/.*|.*test.*\.py|test\.sh|verify\.sh|eval\.py|benchmark\.py|check\.py)$",
    re.IGNORECASE,
)


def _dedupe_preserve(values: Iterable[str]) -> tuple[str, ...]:
    """Deduplicate strings preserving first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = value.strip()
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return tuple(ordered)


def _check_id(origin: str, command: str) -> str:
    digest = sha256(f"{origin}|{command}".encode("utf-8")).hexdigest()[:10]
    return f"check-{digest}"


class EvalIndexer:
    """Builds an ``EvalIndex`` from an ``EnvMap``."""

    def build(self, envmap: EnvMap) -> EvalIndex:
        checks: list[CheckSpec] = []
        by_command: set[str] = set()

        def add(command: str, origin: str, label: str | None = None) -> None:
            cmd = command.strip()
            if not cmd or cmd in by_command:
                return
            by_command.add(cmd)
            checks.append(
                CheckSpec(
                    check_id=_check_id(origin, cmd),
                    label=label or cmd,
                    command=cmd,
                    origin=origin,
                    authoritative=True,
                )
            )

        for raw in envmap.grader_hints.get("verify_commands", ()) or ():
            add(str(raw), "grader_hint")

        for match in _VERIFY_COMMAND_RE.finditer(envmap.task_prompt):
            add(match.group(1), "task_prompt")

        for raw_path in sorted(envmap.visible_files):
            path = normalize_relpath(raw_path, envmap.workspace_root)
            if not _CHECK_FILENAME_RE.search(path):
                continue
            add(self._command_for_path(path), "visible_file", label=path)

        for target in envmap.task_metadata.get("make_targets", ()) or ():
            value = str(target).strip()
            if value:
                add(f"make {value}", "make_target", label=f"make:{value}")

        return EvalIndex(checks=tuple(checks))

    @staticmethod
    def _command_for_path(path: str) -> str:
        _, ext = os.path.splitext(path)
        lowered = path.lower()
        if lowered.endswith(".sh"):
            return f"bash {path}"
        if lowered.endswith(".py"):
            return f"python {path}"
        return path


class ObjectiveGraphBuilder:
    """Builds an ``ObjectiveGraph`` from an ``EnvMap`` and ``EvalIndex``."""

    def build(self, envmap: EnvMap, eval_index: EvalIndex) -> ObjectiveGraph:
        deliverables = self._deliverables(envmap)
        protected_paths = self._paths_from(
            envmap.workspace_root,
            envmap.grader_hints.get("immutable_paths", ()),
            envmap.task_metadata.get("immutable_paths", ()),
        )
        allowed_edit_roots = self._allowed_roots(envmap)
        services = self._services(envmap)
        packages = _dedupe_preserve(
            str(item)
            for source in (
                envmap.grader_hints.get("required_packages", ()),
                envmap.task_metadata.get("required_packages", ()),
            )
            for item in (source or ())
        )
        thresholds = self._thresholds(envmap)
        output_schema = dict(envmap.grader_hints.get("output_schema", {}) or {})
        output_schema_target = normalize_relpath(
            str(envmap.grader_hints.get("output_schema_target", "") or ""),
            envmap.workspace_root,
        )

        obligations: list[ProofObligation] = []
        for deliverable in deliverables:
            if deliverable.required:
                obligations.append(
                    ProofObligation(
                        obligation_id=f"artifact:{deliverable.path}",
                        kind="artifact",
                        description=f"required artifact {deliverable.path}",
                        target=deliverable.path,
                    )
                )
        for service in services:
            obligations.append(
                ProofObligation(
                    obligation_id=f"service:{service.name}",
                    kind="service",
                    description=f"service proof for {service.name}",
                    target=service.name,
                )
            )
        obligations.append(
            ProofObligation(
                obligation_id="integrity:clean",
                kind="integrity",
                description="no protected or disallowed edits",
                target="clean_workspace",
            )
        )

        return ObjectiveGraph(
            deliverables=tuple(deliverables),
            protected_paths=protected_paths,
            allowed_edit_roots=allowed_edit_roots,
            service_requirements=tuple(services),
            package_requirements=packages,
            thresholds=tuple(thresholds),
            output_schema=output_schema,
            output_schema_target=output_schema_target,
            obligations=tuple(obligations),
        )

    def _deliverables(self, envmap: EnvMap) -> list[DeliverableSpec]:
        seen: set[str] = set()
        deliverables: list[DeliverableSpec] = []

        def add(path: str, description: str = "") -> None:
            normalized = normalize_relpath(path, envmap.workspace_root)
            if not normalized or normalized.startswith("tests/"):
                return
            if normalized not in seen:
                seen.add(normalized)
                deliverables.append(DeliverableSpec(path=normalized, description=description))

        for source in (
            envmap.grader_hints.get("required_artifacts", ()),
            envmap.task_metadata.get("required_artifacts", ()),
        ):
            for raw in source or ():
                add(str(raw), "explicit artifact hint")

        for match in _PROMPT_DELIVERABLE_RE.finditer(envmap.task_prompt):
            add(match.group(1), "prompt-inferred deliverable")

        return deliverables

    @staticmethod
    def _paths_from(workspace_root: str, *sources: object) -> tuple[str, ...]:
        values: list[str] = []
        for source in sources:
            for raw in source or ():
                path = normalize_relpath(str(raw), workspace_root)
                if path:
                    values.append(path)
        return _dedupe_preserve(values)

    @staticmethod
    def _allowed_roots(envmap: EnvMap) -> tuple[str, ...]:
        explicit = ObjectiveGraphBuilder._paths_from(
            envmap.workspace_root,
            envmap.grader_hints.get("allowed_edit_roots", ()),
            envmap.task_metadata.get("allowed_edit_roots", ()),
        )
        return explicit or (".",)

    @staticmethod
    def _services(envmap: EnvMap) -> list[ServiceRequirement]:
        services: list[ServiceRequirement] = []
        for raw in envmap.grader_hints.get("service_requirements", ()) or ():
            if isinstance(raw, dict):
                name = str(raw.get("name", "")).strip()
                if not name:
                    continue
                port = raw.get("port")
                services.append(
                    ServiceRequirement(
                        name=name,
                        port=int(port) if port is not None else None,
                        must_be_live=bool(raw.get("must_be_live", True)),
                        proof_kind=str(raw.get("proof_kind", "probe")),
                    )
                )
        return services

    @staticmethod
    def _thresholds(envmap: EnvMap) -> list[MetricThreshold]:
        thresholds: list[MetricThreshold] = []
        raw_thresholds = envmap.grader_hints.get("thresholds", {}) or {}
        if isinstance(raw_thresholds, dict):
            for name, raw in sorted(raw_thresholds.items()):
                if isinstance(raw, dict):
                    comparator = str(raw.get("comparator", ">="))
                    target = raw.get("target", 0)
                else:
                    comparator = ">="
                    target = raw
                thresholds.append(
                    MetricThreshold(
                        name=str(name),
                        comparator=comparator,
                        target=target,
                    )
                )
        return thresholds
