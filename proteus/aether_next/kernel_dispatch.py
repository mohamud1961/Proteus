"""Solver action dispatch: executes one action via kernel-owned engines.

Extracted from kernel.py to honor the 500-LOC module cap.  ``kernel`` is the
AetherNextKernel instance supplying the engines (failure parser, integrity
guards, bootstrap/process/perception/experiment lanes).
"""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping

from .execution import Executor
from .ledger import ExecutionLedger, Receipt
from .perception_vision import needs_vision, vision_transcribe_receipt
from .runtime_ir import ActionRequest, CompiledRuntime, EnvMap, normalize_relpath


def _head_tail(text: str, cap: int) -> str:
    """Return a marked head+tail excerpt without destroying full payloads."""
    if len(text) <= cap:
        return text
    half = max(1, cap // 2)
    omitted = len(text) - (half * 2)
    return text[:half] + f"\n... [omitted {omitted} chars; full output available by handle]\n" + text[-half:]



def _action_timeout_s(action: ActionRequest, envmap: EnvMap) -> tuple[int, str]:
    """Bound solver-requested command timeout by generic task budget metadata.

    The solver may request a longer timeout for builds/training/service setup,
    but the harness remains in charge of the ceiling.  Task metadata comes from
    task.toml when available; this is generic budget information, not hidden
    grader logic.
    """
    requested_raw = action.arguments.get("timeout_s", None)
    default_timeout = 30
    metadata = envmap.task_metadata if isinstance(envmap.task_metadata, Mapping) else {}
    budget = metadata.get("resource_budget") if isinstance(metadata.get("resource_budget"), Mapping) else {}
    timeout_candidates = [300]
    for key in ("agent_timeout_sec", "timeout_sec", "verifier_timeout_sec"):
        value = budget.get(key) or metadata.get(key)
        try:
            if value is not None:
                timeout_candidates.append(max(30, int(float(value))))
        except (TypeError, ValueError):
            pass
    max_timeout = min(max(timeout_candidates), 12_000)
    if requested_raw is None:
        return default_timeout, f"default={default_timeout}; max_available={max_timeout}"
    try:
        requested = int(float(requested_raw))
    except (TypeError, ValueError):
        return default_timeout, f"invalid_requested={requested_raw!r}; default={default_timeout}; max_available={max_timeout}"
    if requested <= 0:
        return default_timeout, f"nonpositive_requested={requested}; default={default_timeout}; max_available={max_timeout}"
    effective = min(requested, max_timeout)
    return effective, f"requested={requested}; effective={effective}; max_available={max_timeout}"


def dispatch_action(kernel: Any, action: ActionRequest, step: int, compiled: CompiledRuntime,
                executor: Executor, envmap: EnvMap, ledger: ExecutionLedger) -> list[Receipt]:
    """Execute one solver action through the kernel-owned engines and return receipts."""
    kind = action.kind
    if kind == "read_file":
        path = normalize_relpath(str(action.arguments.get("path", "")), envmap.workspace_root)
        base = {"path": path, "candidate_id": action.candidate_id}
        try:
            content = executor.read_file(path)
            payload = dict(base)
            payload.update({
                "content_hash": sha256(content.encode("utf-8", "replace")).hexdigest()[:16],
                "bytes": len(content),
                "content": content,
                "excerpt": _head_tail(content, 4000),
                "file_handle": f"file:{path}",
            })
            return [Receipt(
                receipt_id=f"step-{step}:{action.action_id}:read", step=step,
                kind="read_file", success=True,
                summary=f"read {path} ({len(content)} bytes)", payload=payload,
            )]
        except FileNotFoundError:
            return [Receipt(
                receipt_id=f"step-{step}:{action.action_id}:read", step=step,
                kind="read_file", success=False, summary=f"file not found: {path}",
                failure_class="missing_artifact", payload=base,
            )]
    if kind == "read_file_page":
        path = normalize_relpath(str(action.arguments.get("path", "")), envmap.workspace_root)
        try:
            content = executor.read_file(path)
            offset = max(0, int(action.arguments.get("offset", 0) or 0))
            span = max(1, min(20000, int(action.arguments.get("span", 8000) or 8000)))
            chunk = content[offset: offset + span]
            return [Receipt(
                receipt_id=f"step-{step}:{action.action_id}:read_page",
                step=step, kind="read_file_page", success=True,
                summary=f"read {path} bytes {offset}:{offset + len(chunk)}",
                payload={"path": path, "offset": offset, "span": span, "bytes": len(content), "chunk": chunk, "file_handle": f"file:{path}"},
            )]
        except FileNotFoundError:
            return [Receipt(
                receipt_id=f"step-{step}:{action.action_id}:read_page", step=step,
                kind="read_file_page", success=False, summary=f"file not found: {path}",
                failure_class="missing_artifact", payload={"path": path},
            )]
    if kind in {"read_output", "grep_output"}:
        handle = str(action.arguments.get("handle", "")).strip()
        pattern = str(action.arguments.get("pattern", ""))
        full = ""
        source_receipt = ""
        stream = ""
        overflow = ""
        for receipt in ledger.all_receipts():
            payload = receipt.payload or {}
            if payload.get("stdout_handle") == handle:
                full = str(payload.get("stdout_full", "")); source_receipt = receipt.receipt_id; stream = "stdout"
                overflow = str(payload.get("stdout_overflow_path", ""))
                break
            if payload.get("stderr_handle") == handle:
                full = str(payload.get("stderr_full", "")); source_receipt = receipt.receipt_id; stream = "stderr"
                overflow = str(payload.get("stderr_overflow_path", ""))
                break
        if source_receipt and overflow:
            # The complete stream was spooled to disk; serve from the spool
            # so paging/grep operate on the full untruncated output.
            try:
                with open(overflow, encoding="utf-8", errors="replace") as fh:
                    full = fh.read()
            except OSError as exc:
                return [Receipt(
                    receipt_id=f"step-{step}:{action.action_id}:output", step=step,
                    kind=kind, success=False,
                    summary=f"output spool unreadable for handle {handle}: {exc}",
                    failure_class="missing_context_handle",
                    payload={"handle": handle, "overflow_path": overflow},
                )]
        if not source_receipt:
            return [Receipt(
                receipt_id=f"step-{step}:{action.action_id}:output", step=step,
                kind=kind, success=False, summary=f"output handle not found: {handle}",
                failure_class="missing_context_handle", payload={"handle": handle},
            )]
        if kind == "grep_output":
            lines = [line for line in full.splitlines() if pattern in line]
            chunk = "\n".join(lines[:200])
            summary = f"grep_output {handle!r} pattern={pattern!r}: {len(lines)} matching lines"
            payload = {"handle": handle, "pattern": pattern, "matches": len(lines), "chunk": chunk, "source_receipt_id": source_receipt, "stream": stream}
        else:
            offset = max(0, int(action.arguments.get("offset", 0) or 0))
            span = max(1, min(20000, int(action.arguments.get("span", 8000) or 8000)))
            chunk = full[offset: offset + span]
            summary = f"read_output {handle!r} bytes {offset}:{offset + len(chunk)}"
            payload = {"handle": handle, "offset": offset, "span": span, "bytes": len(full), "chunk": chunk, "source_receipt_id": source_receipt, "stream": stream}
        return [Receipt(
            receipt_id=f"step-{step}:{action.action_id}:output", step=step, kind=kind, success=True, summary=summary, payload=payload,
        )]
    if kind == "report_blocker":
        payload = {
            "blocked_component": str(action.arguments.get("blocked_component", "")),
            "observed_evidence": str(action.arguments.get("observed_evidence", "")),
            "attempted_actions": str(action.arguments.get("attempted_actions", "")),
            "why_current_tools_or_config_prevent_progress": str(action.arguments.get("why_current_tools_or_config_prevent_progress", "")),
            "requested_harness_change": str(action.arguments.get("requested_harness_change", "")),
            "candidate_id": action.candidate_id,
        }
        return [Receipt(
            receipt_id=f"step-{step}:{action.action_id}:blocker", step=step,
            kind="report_blocker", success=False,
            summary=f"solver reported blocker: {payload['blocked_component']}",
            failure_class="solver_reported_blocker", payload=payload,
        )]
    if kind == "write_file":
        path = normalize_relpath(str(action.arguments.get("path", "")), envmap.workspace_root)
        before_hash = ""
        before_bytes = None
        try:
            before_content = executor.read_file(path)
            before_hash = sha256(before_content.encode("utf-8", "replace")).hexdigest()[:16]
            before_bytes = len(before_content)
        except FileNotFoundError:
            before_content = ""
        content = str(action.arguments.get("content", ""))
        executor.write_file(path, content)
        after_hash = sha256(content.encode("utf-8", "replace")).hexdigest()[:16]
        payload = {
            "path": path,
            "modified_paths": (path,),
            "artifact_paths": (path,),
            "candidate_id": action.candidate_id,
            "before_content_hash": before_hash,
            "after_content_hash": after_hash,
            "before_bytes": before_bytes,
            "bytes": len(content),
            "content": content,
            "excerpt": _head_tail(content, 4000),
            "file_handle": f"file:{path}",
        }
        return [Receipt(
            receipt_id=f"step-{step}:{action.action_id}:write", step=step,
            kind="write_file", success=True, summary=f"wrote {path}", state_change=True,
            payload={k: v for k, v in payload.items() if v is not None and v != ""},
        )]
    if kind == "run_command":
        command = str(action.arguments.get("command", ""))
        timeout_s, timeout_note = _action_timeout_s(action, envmap)
        result = executor.run_command(command, cwd=envmap.workspace_root, timeout_s=timeout_s)
        failure_class = kernel.failure_parser.classify(
            result.stdout + "\n" + result.stderr,
            exit_code=result.exit_code,
        ) if not result.success else ""
        changed_paths = tuple(result.modified_paths) + tuple(result.removed_paths)
        integrity_violation = kernel.integrity_guards.validate_modified_paths(
            compiled.objective_graph, changed_paths,
        )
        payload: dict[str, Any] = {
            "command": command,
            "timeout_s": timeout_s,
            "timeout_policy": timeout_note,
            "exit_code": result.exit_code,
            "stdout": _head_tail(result.stdout, 8000),
            "stderr": _head_tail(result.stderr, 8000),
            "stdout_full": result.stdout,
            "stderr_full": result.stderr,
            "stdout_handle": f"{step}:{action.action_id}:stdout",
            "stderr_handle": f"{step}:{action.action_id}:stderr",
            "stdout_bytes": result.stdout_bytes_total,
            "stderr_bytes": result.stderr_bytes_total,
            "stdout_overflow_path": result.stdout_overflow_path,
            "stderr_overflow_path": result.stderr_overflow_path,
            "timed_out": result.timed_out,
            "modified_paths": tuple(
                normalize_relpath(p, envmap.workspace_root) for p in result.modified_paths
            ),
            "artifact_paths": tuple(
                normalize_relpath(p, envmap.workspace_root) for p in result.produced_artifacts
            ),
            "removed_paths": tuple(
                normalize_relpath(p, envmap.workspace_root) for p in result.removed_paths
            ),
            "state_delta": dict(result.state_delta),
            "candidate_id": action.candidate_id,
        }
        if integrity_violation:
            payload["integrity_violation"] = integrity_violation
        if result.metrics:
            first_key = sorted(result.metrics)[0]
            payload["metric_name"] = first_key
            payload["metric_value"] = result.metrics[first_key]
        return [Receipt(
            receipt_id=f"step-{step}:{action.action_id}:cmd",
            step=step,
            kind="run_command",
            success=result.success,
            summary=f"command exit={result.exit_code}: {command}",
            state_change=bool(
                result.modified_paths or result.produced_artifacts or result.removed_paths
            ),
            failure_class=failure_class,
            payload=payload,
        )]
    if kind == "bootstrap_acquire":
        return [kernel.bootstrap_engine.execute(action, step, executor, envmap)[0]]
    if kind == "launch_process":
        interactive = compiled.process_policy.mode == "interactive_detachable"
        return [kernel.process_orchestrator.launch(
            action, step, executor,
            workspace_root=envmap.workspace_root, interactive=interactive,
        )]
    if kind == "probe_service":
        return [kernel.process_orchestrator.probe(action, step, executor)]
    if kind == "stop_process":
        return [kernel.process_orchestrator.stop(action, step, executor)]
    if kind == "inspect_artifact":
        base = kernel.perception_lane.inspect(
            action, step, executor, workspace_root=envmap.workspace_root,
        )
        if needs_vision(base):
            vision = vision_transcribe_receipt(kernel, action, step, executor, base)
            if vision is not None:
                return [vision]
        return [base]
    if kind == "register_candidate":
        cid = str(action.arguments.get("candidate_id", "")).strip()
        return [Receipt(
            receipt_id=f"step-{step}:{action.action_id}:register", step=step,
            kind="register_candidate", success=True,
            summary=f"registered candidate {cid}", state_change=True,
            payload={"candidate_id": cid,
                     "candidate_summary": str(action.arguments.get("summary", "")).strip(),
                     "candidate_status": "active"},
        )]
    if kind == "run_experiment":
        return [kernel.experiment_engine.run(
            action, step, executor, workspace_root=envmap.workspace_root,
        )]
    return [Receipt(
        receipt_id=f"step-{step}:{action.action_id}:unknown", step=step,
        kind="unknown_action", success=False,
        summary=f"unknown action kind: {kind}", failure_class="action_validation",
    )]