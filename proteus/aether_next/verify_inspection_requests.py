"""Verifier inspection-request construction and ref bookkeeping.

Extracted from model_hooks.py for the 500-LOC cap. These helpers turn
verifier-side signals (missing-evidence prose, an empty inspection history, a
packet's known artifact/output handles) into concrete read-only
``VerifierInspectionRequest`` objects, and classify which inspections
actually happened (and which of those are independent-derivation kinds) so
the completion-evidence protocol in verify_completion_protocol.py can gate on
them without re-deriving that bookkeeping itself.

The completion_evidence refusal builders (``_refuse_completion_record`` /
``_refuse_completion_independence``) were further extracted to
verify_completion_gates.py, alongside the record/independence problem
detectors they pair with, for the same 500-LOC cap.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from .verifier_inspector import VerifierInspectionRequest, parse_verifier_inspection_requests


def _model_output_error(message: str) -> Exception:
    # Deferred import: model_hooks.py imports this module at module load
    # time, so importing ModelOutputError back at module level would cycle.
    # Mirrors the same pattern already used in model_parse.py.
    from .model_hooks import ModelOutputError

    return ModelOutputError(message)


def _verifier_identity_prompt_for(compiled: Any) -> str:
    prompt = str(getattr(compiled, "verifier_identity_prompt", "") or "").strip()
    if prompt:
        operational = (
            "You have read-only verifier tools this round. To use them, emit exactly "
            "one JSON object of the form {\"kind\":\"inspect\",\"requests\":[...]} before judging "
            "when evidence is missing. Available inspection kinds include read_file, "
            "read_output, rerun_check, overlay_run_command, probe_port, probe_http, "
            "probe_process, inspect_artifact, and perceive_artifact. Do not claim "
            "blocked_by_tooling or that you cannot inspect until a concrete inspection "
            "request has failed.\n\n"
        )
        return operational + prompt
    raise _model_output_error("architect-authored verifier prompt is required")


def _verifier_max_output_tokens() -> int:
    return int(os.environ.get("AETHER_VERIFIER_MAX_OUTPUT_TOKENS", "6000"))


def _structured_missing_evidence_requests(raw: str) -> tuple[VerifierInspectionRequest, ...]:
    text = str(raw or "").strip()
    decoder = json.JSONDecoder()
    data: Any = None
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _end = decoder.raw_decode(text[idx:])
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(data, Mapping):
        return ()
    raw_requests = data.get("missing_evidence_requests")
    if not isinstance(raw_requests, list) or not any(isinstance(item, Mapping) for item in raw_requests):
        return ()
    request_items = [dict(item) for item in raw_requests if isinstance(item, Mapping)]
    return parse_verifier_inspection_requests({"kind": "inspect", "requests": request_items})


_PATH_IN_REQUEST_RE = re.compile(r"(?:/app/|\b)([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*\.[A-Za-z0-9]{1,8})")

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")
_TRANSCRIPT_REQUEST_RE = re.compile(
    r"\b(stdout|stderr|transcript|frame-evidence|frame evidence|receipt text|command output|printed by)\b",
    re.IGNORECASE,
)


def _inspections_from_missing_evidence(
    result: Any,
    *,
    packet: Mapping[str, Any] | None = None,
) -> tuple[VerifierInspectionRequest, ...]:
    """Realize prose missing-evidence requests that name concrete files.

    Observed live: verifiers returned uncertain_missing_evidence asking the
    SOLVER to "provide the contents of /app/output.txt" -- evidence only
    verifier-side inspection can produce (solver claims never enter the
    state-only packet).  When a request names a workspace file, inspect it
    directly instead of stalling the run on an unsatisfiable ask.
    """
    seen: list[str] = []
    wants_transcript = False
    for request in getattr(result, "missing_evidence_requests", ()) or ():
        text = str(request)
        if _TRANSCRIPT_REQUEST_RE.search(text):
            wants_transcript = True
        for match in _PATH_IN_REQUEST_RE.finditer(str(request)):
            path = match.group(1)
            if path not in seen:
                seen.append(path)
    requests: list[VerifierInspectionRequest] = []
    for idx, path in enumerate(seen[:4]):
        kind = "perceive_artifact" if path.lower().endswith(_IMAGE_EXTENSIONS) else "read_file"
        requests.append(VerifierInspectionRequest(
            request_id=f"auto-missing-evidence-{idx}",
            kind=kind,
            path=path,
        ))
    if wants_transcript:
        requests.extend(_read_output_requests_from_packet(packet, start_idx=len(requests)))
    return tuple(requests)


def _read_output_requests_from_packet(
    packet: Mapping[str, Any] | None,
    *,
    start_idx: int = 0,
) -> tuple[VerifierInspectionRequest, ...]:
    if not isinstance(packet, Mapping):
        return ()
    handles = packet.get("state_inspection_handles")
    if not isinstance(handles, (list, tuple)):
        return ()
    output_handles: list[dict[str, Any]] = []
    for item in handles:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind", "")).strip() != "output":
            continue
        handle = str(item.get("handle", "")).strip()
        stream = str(item.get("stream", "")).strip()
        if not handle or stream not in {"stdout", "stderr"}:
            continue
        output_handles.append({
            "handle": handle,
            "stream": stream,
            "bytes": int(item.get("bytes", 0) or 0),
        })
    if not output_handles:
        return ()
    def _handle_key(item: Mapping[str, Any]) -> tuple[int, str]:
        handle = str(item.get("handle", ""))
        try:
            step_part = handle.split(":", 1)[0]
            return (int(step_part), handle)
        except Exception:
            return (-1, handle)

    def _handle_base(handle: str) -> str:
        parts = handle.split(":")
        return ":".join(parts[:-1]) if len(parts) >= 2 else handle

    output_handles.sort(key=_handle_key)
    latest_stdout = next((item for item in reversed(output_handles) if item["stream"] == "stdout"), None)
    chosen: list[dict[str, Any]] = []
    if latest_stdout is not None:
        chosen.append(latest_stdout)
        sibling_base = _handle_base(latest_stdout["handle"])
        sibling_stderr = next(
            (
                item
                for item in reversed(output_handles)
                if item["stream"] == "stderr" and _handle_base(item["handle"]) == sibling_base
            ),
            None,
        )
        if sibling_stderr is not None:
            chosen.append(sibling_stderr)
    if not chosen:
        chosen = output_handles[-2:]
    requests: list[VerifierInspectionRequest] = []
    for idx, item in enumerate(chosen, start=start_idx):
        requests.append(VerifierInspectionRequest(
            request_id=f"auto-missing-output-{idx}",
            kind="read_output",
            handle=item["handle"],
            span=4000,
        ))
    return tuple(requests)


def _inspection_result_errored(row: Mapping[str, Any]) -> bool:
    """True when an inspection RESULT row reports failure, never content.

    Every failure path in verifier_inspector.py (``_error_result`` and each
    probe's own failure dict) funnels through a non-empty top-level
    ``error`` field. A negative-but-successful observation -- a closed
    port, an unreachable URL, a non-existent artifact, a non-zero rerun
    exit code -- is NOT an error by this check: it is a real, content-
    bearing inspection outcome, and judging whether that content supports a
    claim stays the model's job. This is a structural/provenance check
    only.
    """
    return bool(str(row.get("error", "")).strip())


def _paired_non_errored_inspections(
    requests: Any, results: Any,
) -> list[tuple[Any, Mapping[str, Any]]]:
    """Pair each inspection request with its result row, keeping non-errored pairs.

    Pairing prefers a same-request_id result row (the real inspector always
    echoes request_id back); positional pairing is a defensive fallback for
    a results list that omits it. This is the shared basis for every
    content-blind provenance check on completion_evidence: a ref may only
    resolve to an inspection that both HAPPENED and DID NOT ERROR this
    round -- citing a failed read/probe must never satisfy a gate.
    """
    results_list = [row for row in (results or ()) if isinstance(row, Mapping)]
    by_request_id: dict[str, Mapping[str, Any]] = {}
    for row in results_list:
        rid = str(row.get("request_id", "")).strip()
        if rid and rid not in by_request_id:
            by_request_id[rid] = row
    pairs: list[tuple[Any, Mapping[str, Any]]] = []
    for idx, request in enumerate(requests or ()):
        request_id = str(getattr(request, "request_id", "")).strip()
        row = by_request_id.get(request_id)
        if row is None and idx < len(results_list):
            row = results_list[idx]
        if row is None or _inspection_result_errored(row):
            continue
        pairs.append((request, row))
    return pairs


def _refs_from_inspections(requests: Any, results: Any) -> set[str]:
    """Canonical IDs of successful registered inspections from this round.

    Paths, handles, request IDs, and targets are useful display aliases but are
    not evidence authority.  Completion records must cite ``inspection_id``
    values created by the kernel registry before the result reached the model.
    """
    refs: set[str] = set()
    for _request, row in _paired_non_errored_inspections(requests, results):
        inspection_id = str(row.get("inspection_id", "")).strip()
        if inspection_id and bool(row.get("eligible_for_proof", False)):
            refs.add(inspection_id)
    return refs


# Inspection kinds that constitute independent derivation: the verifier
# itself executes/observes something (overlay execution, a live probe, or
# its own perception) rather than reading a solver-produced artifact's
# content and trusting it. read_file, read_output, inspect_recent_receipts,
# and inspect_artifact_history are deliberately excluded -- they only ever
# surface what the solver already produced, which is exactly the self-
# confirmation the false-clean failure mode exploits (see
# FABLE5_BATCH_AUDIT_20260709T101515Z.md secs 4/6). rerun_check groups with
# overlay_run_command: both execute independently in the disposable verifier
# overlay via VerifierOverlay.run_command, differing only in whether the
# command comes from an architect-declared check or an ad hoc request.
_INDEPENDENT_DERIVATION_KINDS = frozenset({
    "overlay_run_command",
    "rerun_check",
    "probe_port",
    "probe_http",
    "probe_process",
    "perceive_artifact",
})


def _independent_derivation_refs(requests: Any, results: Any) -> set[str]:
    """Canonical registered IDs for independent-derivation inspections."""
    refs: set[str] = set()
    for request, row in _paired_non_errored_inspections(requests, results):
        if str(getattr(request, "kind", "")).strip() not in _INDEPENDENT_DERIVATION_KINDS:
            continue
        inspection_id = str(row.get("inspection_id", "")).strip()
        if inspection_id and bool(row.get("eligible_for_proof", False)):
            refs.add(inspection_id)
    return refs


def _default_completion_inspection_requests(packet: Mapping[str, Any]) -> tuple[VerifierInspectionRequest, ...]:
    """Minimal generic read-only evidence when a verifier completes uninspected.

    This does not decide task state. It gives the verifier current-state
    observations it failed to ask for itself, then asks the verifier to judge.
    """
    requests: list[VerifierInspectionRequest] = []
    artifact_paths: list[str] = []
    raw_state_paths: list[str] = []
    for key in ("artifacts_present",):
        raw = packet.get(key, ())
        if isinstance(raw, (list, tuple)):
            artifact_paths.extend(str(item).strip() for item in raw if str(item).strip())
    artifact_evidence = packet.get("artifact_evidence", ())
    if isinstance(artifact_evidence, (list, tuple)):
        for item in artifact_evidence:
            if isinstance(item, Mapping):
                path = str(item.get("path", "") or item.get("artifact_path", "")).strip()
                if path:
                    artifact_paths.append(path)

    latest_file_reads = packet.get("latest_file_reads", ())
    if isinstance(latest_file_reads, (list, tuple)):
        for item in latest_file_reads:
            if isinstance(item, Mapping):
                path = str(item.get("path", "")).strip()
                if path:
                    raw_state_paths.append(path)
    raw_state_candidates = packet.get("raw_state_candidates", ())
    if isinstance(raw_state_candidates, (list, tuple)):
        for item in raw_state_candidates:
            if isinstance(item, Mapping):
                path = str(item.get("path", "")).strip()
                if path:
                    raw_state_paths.append(path)
    # Neutral verifier v2 packets carry current artifacts in compact dynamic
    # state and exact state handles rather than the legacy artifact_evidence /
    # latest_file_reads fields.  Prefer those paths for the mandatory first
    # read so automatic inspection produces a resolvable evidence reference.
    dynamic_state = packet.get("dynamic_state")
    if isinstance(dynamic_state, Mapping):
        dynamic_files = dynamic_state.get("files", {})
        if isinstance(dynamic_files, Mapping):
            for path in dynamic_files:
                text = str(path).strip()
                if text:
                    artifact_paths.append(text)
    state_handles = packet.get("state_inspection_handles", ())
    if isinstance(state_handles, (list, tuple)):
        for item in state_handles:
            if isinstance(item, Mapping):
                path = str(item.get("path", "") or item.get("handle", "")).strip()
                if path and str(item.get("kind", "file")).strip() == "file":
                    raw_state_paths.append(path)

    def _dedupe(paths: list[str], *, seen: set[str] | None = None) -> list[str]:
        local_seen = seen if seen is not None else set()
        deduped: list[str] = []
        for path in paths:
            if path in local_seen:
                continue
            local_seen.add(path)
            deduped.append(path)
        return deduped

    seen_paths: set[str] = set()
    deduped_artifacts = _dedupe(artifact_paths, seen=seen_paths)
    deduped_raw_state = _dedupe(raw_state_paths, seen=seen_paths)

    for idx, path in enumerate(deduped_artifacts[:1]):
        requests.append(VerifierInspectionRequest(
            request_id=f"auto-read-artifact-{idx}",
            kind="read_file",
            path=path,
            limit=1,
        ))
    for idx, path in enumerate(deduped_raw_state[:1]):
        requests.append(VerifierInspectionRequest(
            request_id=f"auto-read-raw-state-{idx}",
            kind="read_file",
            path=path,
            limit=1,
        ))
    if len(requests) < 3:
        requests.append(VerifierInspectionRequest(
            request_id="auto-recent-receipts",
            kind="inspect_recent_receipts",
            limit=8,
        ))
    if len(requests) < 3 and deduped_artifacts:
        requests.append(VerifierInspectionRequest(
            request_id="auto-artifact-history",
            kind="inspect_artifact_history",
            path=deduped_artifacts[0],
            limit=8,
        ))
    command_receipts = packet.get("recent_command_receipts", ())
    if len(requests) < 3 and isinstance(command_receipts, (list, tuple)):
        latest_stdout = ""
        latest_stderr = ""
        for item in reversed(command_receipts):
            if not isinstance(item, Mapping):
                continue
            if not latest_stdout:
                latest_stdout = str(item.get("stdout_handle", "")).strip()
            if not latest_stderr:
                latest_stderr = str(item.get("stderr_handle", "")).strip()
            if latest_stdout and latest_stderr:
                break
        if latest_stdout:
            requests.append(VerifierInspectionRequest(
                request_id="auto-latest-command-stdout",
                kind="read_output",
                handle=latest_stdout,
                span=4000,
            ))
        if len(requests) < 3 and latest_stderr:
            requests.append(VerifierInspectionRequest(
                request_id="auto-latest-command-stderr",
                kind="read_output",
                handle=latest_stderr,
                span=4000,
            ))
    return tuple(requests[:3])


def _completed_inspection_is_semantically_grounded(
    packet: Mapping[str, Any],
    inspection_results: list[Mapping[str, Any]],
) -> bool:
    if not inspection_results:
        return False
    if not packet.get("local_verification_limits") and not packet.get("false_positive_risks"):
        return True
    saw_output = False
    saw_substantive_file = False
    for row in inspection_results:
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind", "")).strip()
        if kind == "read_output" and str(row.get("excerpt", "")).strip():
            saw_output = True
            continue
        if kind == "read_file":
            excerpt = str(row.get("excerpt", "")).strip()
            path = str(row.get("path", "")).strip()
            if excerpt and path and not path.endswith((".py", ".sh", ".js", ".ts", ".java", ".c", ".cpp", ".rs", ".go")):
                saw_substantive_file = True
    return saw_output or saw_substantive_file
