"""Runtime proof-contract analysis.

Architect-authored evidence requirements are useful only when the harness can
turn at least some of them into concrete evidence gates.  This module keeps the
gates generic: it looks for task/evidence surfaces, not task names.
"""
from __future__ import annotations

import re
from typing import Any

from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.runtime_ir import CompiledRuntime


_RDF_TERM_RE = re.compile(r"\b[a-zA-Z][\w-]*:[A-Za-z_][\w-]*\b")


def analyze_proof_contract(compiled: CompiledRuntime, ledger: ExecutionLedger) -> dict[str, Any]:
    text = " ".join(
        [
            compiled.task_prompt,
            compiled.success_definition,
            " ".join(compiled.evidence_requirements),
            " ".join(compiled.false_positive_risks),
            " ".join(compiled.minimum_completion_evidence),
        ]
    ).lower()
    findings: list[dict[str, Any]] = []
    findings.extend(_semantic_query_findings(text, ledger))
    findings.extend(_filter_security_findings(text, ledger))
    findings.extend(_openssl_cert_findings(text, ledger))
    if any(f.get("severity") == "blocking" for f in findings):
        status = "failed"
    elif not _has_contract(compiled):
        # An architect contract that never populated success_definition /
        # evidence_requirements / false_positive_risks / minimum_completion_evidence
        # has nothing for these analyzers to check. "passed" would claim "checked
        # and clean" when nothing was actually checked -- label it honestly instead.
        # This does not block completion (see proof_contract_receipt): a missing
        # contract is not itself evidence of a bad artifact, just of an unassessed one.
        status = "contract_missing"
    else:
        status = "passed"
    return {
        "status": status,
        "findings": findings,
        "finding_count": len(findings),
    }


def _has_contract(compiled: CompiledRuntime) -> bool:
    return bool(
        compiled.success_definition
        or compiled.evidence_requirements
        or compiled.false_positive_risks
        or compiled.minimum_completion_evidence
    )


def proof_contract_receipt(compiled: CompiledRuntime, ledger: ExecutionLedger, *, step: int) -> Receipt | None:
    analysis = analyze_proof_contract(compiled, ledger)
    if not analysis["findings"] and analysis["status"] == "contract_missing":
        return None
    return Receipt(
        receipt_id=f"step-{step}:proof_contract",
        step=step,
        kind="proof_contract",
        # Only a genuine "failed" (a blocking finding fired) withholds completion.
        # "contract_missing" is honestly labeled in the payload but does not itself
        # block -- an absent contract is not evidence the artifact is wrong.
        success=analysis["status"] != "failed",
        summary=(
            "proof contract failed: " + ", ".join(f["code"] for f in analysis["findings"] if f.get("severity") == "blocking")
            if analysis["status"] == "failed"
            else f"proof contract {analysis['status']}"
        ),
        failure_class="" if analysis["status"] != "failed" else "proof_contract_unmet",
        payload=analysis,
    )


def _semantic_query_findings(surface_text: str, ledger: ExecutionLedger) -> list[dict[str, Any]]:
    if not ("sparql" in surface_text and ("turtle" in surface_text or ".ttl" in surface_text or "rdf" in surface_text)):
        return []

    query_text = _latest_text_for_suffix(ledger, ".sparql")
    graph_text = _latest_text_for_suffix(ledger, ".ttl")
    findings: list[dict[str, Any]] = []

    if query_text and graph_text:
        query_terms = _rdf_terms(query_text)
        graph_terms = _rdf_terms(graph_text)
        # Prefix declarations are allowed even if the prefixed local names are
        # not graph terms; compare only concrete terms likely used in patterns.
        ignored = {term for term in query_terms if term.lower().endswith(("integer", "date", "string"))}
        absent = sorted(term for term in query_terms - graph_terms - ignored if not term.startswith("xsd:"))
        if absent:
            findings.append({
                "code": "declared_query_terms_absent_from_graph",
                "severity": "blocking",
                "summary": "The saved SPARQL/RDF artifact uses prefixed terms not observed in the graph evidence.",
                "evidence": absent[:16],
                "next_action": "Repair the query using predicates/classes observed in the Turtle graph, then execute it.",
            })

    if query_text and graph_text and not _has_semantic_query_execution(ledger):
        findings.append({
            "code": "missing_semantic_query_execution",
            "severity": "blocking",
            "summary": "A SPARQL/Turtle task has graph and query evidence but no semantic query execution receipt.",
            "evidence": ["query artifact present", "Turtle graph read", "no command evidence of executing the query"],
            "next_action": "Run the query against the Turtle graph or declare the missing query engine as a blocker.",
        })

    return findings


_ATTACK_CLASS_TERMS: dict[str, tuple[str, ...]] = {
    "script_tag": ("<script",),
    "event_handler": ("onclick", "onerror", "onload", "onmouseover"),
    "javascript_uri": ("javascript:",),
}
_PRESERVATION_TERMS = ("unchanged", "preserved", "identical", "same_bytes", "byte-preserv", "byte_preserv")


def _filter_security_findings(surface_text: str, ledger: ExecutionLedger) -> list[dict[str, Any]]:
    """Security/HTML-sanitizer task family: structural trigger, not phrase-gated.

    The trigger (html+javascript co-occurring with an xss/security/sanitize/clean
    notion) is a shape signal already present in the task prompt or architect
    contract -- it does not depend on the architect using any specific wording for
    the risk, so a well-written but differently-phrased false_positive_risks entry
    still engages this obligation. (This replaces a prior version that additionally
    required one of a few hardcoded risk phrases like "one trivial input" -- that
    second gate was the actual bug: a legitimately better-written risk description
    that didn't use those exact words silently disabled the whole check.)
    """
    securityish = all(term in surface_text for term in ("html", "javascript")) and (
        "xss" in surface_text or "security" in surface_text or "sanitize" in surface_text or "clean" in surface_text
    )
    if not securityish:
        return []
    findings: list[dict[str, Any]] = []
    sample_commands = [
        receipt
        for receipt in ledger.all_receipts()
        if receipt.kind == "run_command" and receipt.success and _command_mentions_sample_test(receipt)
    ]
    attack_classes_covered = {
        label
        for receipt in sample_commands
        for label, terms in _ATTACK_CLASS_TERMS.items()
        if _receipt_mentions_any(receipt, terms)
    }
    if len(attack_classes_covered) < 2:
        findings.append({
            "code": "insufficient_adversarial_filter_evidence",
            "severity": "blocking",
            "summary": (
                "A security/HTML-sanitizer task requires evidence across multiple attack "
                f"classes, but only {len(attack_classes_covered)} class(es) "
                f"({', '.join(sorted(attack_classes_covered)) or 'none'}) are evidenced."
            ),
            "evidence": [receipt.receipt_id for receipt in sample_commands],
            "next_action": (
                "Run adversarial fixtures covering multiple XSS vectors "
                "(e.g. <script> tags, inline event handlers, javascript: URIs) and show the full output."
            ),
        })
    preservation_commands = [
        receipt for receipt in ledger.all_receipts()
        if receipt.kind == "run_command" and receipt.success and _receipt_mentions_any(receipt, _PRESERVATION_TERMS)
    ]
    if not preservation_commands:
        findings.append({
            "code": "missing_clean_preservation_evidence",
            "severity": "blocking",
            "summary": (
                "A security/HTML-sanitizer task requires evidence that benign/clean HTML is "
                "preserved unchanged, distinct from evidence that dangerous content is removed, "
                "but no such comparison evidence is present."
            ),
            "evidence": [],
            "next_action": (
                "Run a before/after comparison on clean, benign HTML and show the output is "
                "byte-identical (or explicitly unchanged) outside the removed script content."
            ),
        })
    return findings


def _openssl_cert_findings(surface_text: str, ledger: ExecutionLedger) -> list[dict[str, Any]]:
    """Certificate/key-generation task family: structural trigger on openssl+key/cert shape."""
    certish = "openssl" in surface_text and ("certificate" in surface_text or "cert" in surface_text) and "key" in surface_text
    if not certish:
        return []
    findings: list[dict[str, Any]] = []
    perm_commands = [
        receipt for receipt in ledger.all_receipts()
        if receipt.kind == "run_command" and receipt.success and _receipt_mentions_any(
            receipt, ("stat -c", "stat --format", "chmod", "%a %n"),
        )
    ]
    if not perm_commands:
        findings.append({
            "code": "missing_key_permission_evidence",
            "severity": "blocking",
            "summary": "A certificate/key task has no evidence the private key's file permissions were checked.",
            "evidence": [],
            "next_action": "Run a permission check (e.g. stat -c '%a %n') on the private key file and show the mode.",
        })
    inspect_commands = [
        receipt for receipt in ledger.all_receipts()
        if receipt.kind == "run_command" and receipt.success and _receipt_mentions_any(
            receipt, ("openssl x509", "openssl req", "openssl rsa", "openssl verify"),
        )
    ]
    if not inspect_commands:
        findings.append({
            "code": "missing_openssl_inspection_evidence",
            "severity": "blocking",
            "summary": "A certificate/key task has no evidence the generated certificate/key was inspected with openssl.",
            "evidence": [],
            "next_action": "Run an openssl inspection command (e.g. openssl x509 -noout -subject -dates) and show the output.",
        })
    return findings


def _receipt_mentions_any(receipt: Receipt, terms: tuple[str, ...]) -> bool:
    payload = receipt.payload or {}
    text = " ".join(str(payload.get(key, "")) for key in ("command", "stdout", "stderr")).lower()
    return any(term.lower() in text for term in terms)


def _latest_text_for_suffix(ledger: ExecutionLedger, suffix: str) -> str:
    chunks: list[str] = []
    for receipt in ledger.all_receipts():
        payload = receipt.payload or {}
        path = str(payload.get("path", "")).strip().lower()
        if path.endswith(suffix):
            for key in ("excerpt", "stdout", "detail"):
                value = str(payload.get(key, "")).strip()
                if value:
                    chunks.append(value)
        if receipt.kind == "run_command":
            stdout = str(payload.get("stdout", ""))
            if suffix in stdout.lower():
                chunks.append(stdout)
    return "\n".join(chunks[-4:])


def _rdf_terms(text: str) -> set[str]:
    return {term for term in _RDF_TERM_RE.findall(text or "") if not term.startswith("@prefix")}


def _has_semantic_query_execution(ledger: ExecutionLedger) -> bool:
    for receipt in ledger.all_receipts():
        if receipt.kind != "run_command":
            continue
        payload = receipt.payload or {}
        command = str(payload.get("command", "")).lower()
        if any(term in command for term in ("rdflib", "sparql", "rapper", "riot")) and (
            ".sparql" in command or ".ttl" in command
        ):
            return True
    return False


def _command_mentions_sample_test(receipt: Receipt) -> bool:
    payload = receipt.payload or {}
    text = " ".join(str(payload.get(key, "")) for key in ("command", "stdout", "stderr")).lower()
    return any(term in text for term in ("xss", "javascript:", "onclick", "<script", "clean_html", "in_place_ok"))
