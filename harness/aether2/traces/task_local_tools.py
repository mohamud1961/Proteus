"""Task-local helper tracking for model-authored tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
import json
import re

_DEFAULT_TOOL_ROOT = ".aether/tools"
_EXEC_RE = re.compile(r"(?P<path>(?:\./)?\.aether/tools/[\w./-]+)")


@dataclass
class TaskLocalToolRecord:
    path: str
    name: str
    created_step: int | None = None
    created_by_tool: str | None = None
    inspected: bool = False
    entrypoints: list[str] = field(default_factory=list)
    last_used_step: int | None = None
    validated: bool = False
    smoke_tested: bool = False
    last_exit_code: int | None = None
    evidence_ids: list[str] = field(default_factory=list)
    trusted_for_completion: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "created_step": self.created_step,
            "created_by_tool": self.created_by_tool,
            "inspected": self.inspected,
            "entrypoints": list(self.entrypoints),
            "last_used_step": self.last_used_step,
            "used": self.last_used_step is not None,
            "validated": self.validated,
            "smoke_tested": self.smoke_tested,
            "last_exit_code": self.last_exit_code,
            "evidence_ids": list(self.evidence_ids),
            "trusted_for_completion": self.trusted_for_completion,
            "trusted_for_current_run": self.trusted_for_completion and self.validated and self.last_exit_code == 0,
            "notes": list(self.notes),
        }


class TaskLocalToolRegistry:
    """Observes existing tools; it does not add any new solver tool schema."""

    def __init__(self, *, root: Path, tool_root: str = _DEFAULT_TOOL_ROOT) -> None:
        self.root = root
        self.tool_root = tool_root.strip("/") or _DEFAULT_TOOL_ROOT
        self._records: dict[str, TaskLocalToolRecord] = {}
        self.registry_path = root / ".aether2" / "local_tools.json"

    def observe_tool_invocation(
        self,
        *,
        step: int,
        tool_name: str,
        arguments: Mapping[str, Any],
        exit_code: int | None,
        evidence_id: str | None,
        files_changed: list[str] | None = None,
    ) -> None:
        files_changed = files_changed or []
        if tool_name == "write_file":
            path = str(arguments.get("path", ""))
            if self._is_local_tool_path(path):
                record = self._ensure(path)
                record.created_step = record.created_step if record.created_step is not None else step
                record.created_by_tool = "write_file"
                record.last_exit_code = exit_code
                record.trusted_for_completion = False
                self._add_evidence(record, evidence_id)
                record.notes.append("created_or_updated_by_write_file")

        if tool_name in {"read_file", "inspect_artifact"}:
            path = str(arguments.get("path", ""))
            if self._is_local_tool_path(path):
                record = self._ensure(path)
                record.inspected = True
                self._add_evidence(record, evidence_id)
                record.notes.append("inspected")

        if tool_name in {"run_command", "start_job", "session_start"}:
            cmd = str(
                arguments.get("cmd", "")
                or arguments.get("command", "")
            )
            for path in self._extract_tool_paths(cmd):
                record = self._ensure(path)
                if cmd not in record.entrypoints:
                    record.entrypoints.append(cmd)
                record.last_used_step = step
                record.last_exit_code = exit_code
                self._add_evidence(record, evidence_id)
                if self._looks_like_smoke_test(cmd) and exit_code == 0:
                    record.smoke_tested = True
                    record.validated = True
                    record.trusted_for_completion = True
                    record.notes.append("smoke_test_passed_trusted")
                elif exit_code == 0:
                    record.trusted_for_completion = bool(record.validated)
                    record.notes.append(
                        "successful_execution_trusted"
                        if record.trusted_for_completion
                        else "successful_execution_untrusted"
                    )
                else:
                    record.trusted_for_completion = False

        for path in files_changed:
            if self._is_local_tool_path(path):
                record = self._ensure(path)
                record.created_step = record.created_step if record.created_step is not None else step
                self._add_evidence(record, evidence_id)

        self.persist()

    def summary(self, *, limit: int = 8) -> dict[str, Any]:
        records = sorted(self._records.values(), key=lambda record: record.path)[: max(0, limit)]
        return {
            "tool_root": self.tool_root,
            "count": len(self._records),
            "tools": [record.as_dict() for record in records],
        }

    def completion_risks(self) -> dict[str, Any]:
        trusted: list[dict[str, Any]] = []
        untrusted: list[dict[str, Any]] = []
        for record in sorted(self._records.values(), key=lambda item: item.path):
            payload = {
                "path": record.path,
                "name": record.name,
                "inspected": record.inspected,
                "validated": record.validated,
                "smoke_tested": record.smoke_tested,
                "trusted_for_completion": record.trusted_for_completion,
                "trusted_for_current_run": record.trusted_for_completion and record.validated and record.last_exit_code == 0,
                "last_exit_code": record.last_exit_code,
                "evidence_ids": list(record.evidence_ids),
            }
            if record.trusted_for_completion and record.last_exit_code == 0:
                trusted.append(payload)
            elif record.last_used_step is not None or record.created_step is not None:
                untrusted.append(payload)
        return {
            "trusted_tools": trusted,
            "untrusted_tools": untrusted,
        }

    def persist(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(
            json.dumps(self.summary(limit=10_000), sort_keys=True, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

    def _ensure(self, path: str) -> TaskLocalToolRecord:
        norm = self._normalize_path(path)
        record = self._records.get(norm)
        if record is None:
            record = TaskLocalToolRecord(path=norm, name=Path(norm).name)
            self._records[norm] = record
        return record

    def _is_local_tool_path(self, path: str) -> bool:
        norm = self._normalize_path(path)
        return norm == self.tool_root or norm.startswith(self.tool_root + "/")

    def _normalize_path(self, path: str) -> str:
        path = str(path).strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        return path.lstrip("/")

    def _extract_tool_paths(self, command: str) -> list[str]:
        paths: list[str] = []
        for match in _EXEC_RE.finditer(command):
            path = self._normalize_path(match.group("path"))
            if self._is_local_tool_path(path):
                paths.append(path)
        return paths

    def _looks_like_smoke_test(self, command: str) -> bool:
        lowered = command.lower()
        return any(marker in lowered for marker in ("--self-test", "selftest", "smoke", "pytest", "unittest", "--check"))

    def _add_evidence(self, record: TaskLocalToolRecord, evidence_id: str | None) -> None:
        if evidence_id and evidence_id not in record.evidence_ids:
            record.evidence_ids.append(evidence_id)


__all__ = ["TaskLocalToolRecord", "TaskLocalToolRegistry"]
