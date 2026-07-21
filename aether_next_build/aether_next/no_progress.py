"""Runtime no-progress and repeat-display enforcement."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .ledger import ExecutionLedger, Receipt
from .runtime_ir import ActionRequest


_DISPLAY_RE = re.compile(
    r"^\s*(?:bash\s+-lc\s+)?['\"]?\s*(?:cat|sed|nl|tail|head|grep|awk|wc|ls)\b",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?P<path>(?:/app/)?[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8})")

# Once this many blocks have already fired for a target, a write/state-change
# alone no longer re-arms the guard: only a non-display command that actually
# exercises the target file (executes/validates it, not just reads it) does.
# A single block already grants one write-based reset grace period; if the
# display loop resumes right after that reset without real progress, further
# writes alone must stop working or the loop never actually ends.
_ESCALATION_THRESHOLD = 2


@dataclass(frozen=True)
class NoProgressDecision:
    consequence: str
    reason_code: str
    target: str
    action_family: str
    repeat_count: int
    prior_receipt_ids: tuple[str, ...]
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class NoProgressController:
    """Detect evidence-display loops and emit advice, never a dispatch block.

    In the certified harness this controller is an information-availability
    assistant.  It may warn that the solver is rereading the same target, but
    it must not prevent execution; repeated inspection can be rational when
    context was missing or a handle was not yet obvious.  Post-run audit may
    use these receipts, but runtime completion/dispatch must not treat them as
    authority.
    """

    max_evidence_display_repeats = 2

    def evaluate(self, action: ActionRequest, ledger: ExecutionLedger) -> NoProgressDecision | None:
        if action.kind == "inspect_artifact":
            return self._evaluate_repeated_artifact_inspection(action, ledger)
        if action.kind == "probe_service":
            return self._evaluate_repeated_service_probe(action, ledger)
        if action.kind != "run_command":
            return None
        command = str(action.arguments.get("command", "")).strip()
        target = _evidence_display_target(command)
        if not target:
            return None
        prior = _matching_display_receipts(ledger, target)
        if len(prior) < self.max_evidence_display_repeats:
            return None
        # Compare against the most recent prior display, not the earliest one.
        # Using the earliest match lets a single write anywhere in the run
        # permanently satisfy "state changed since the guard first triggered",
        # silently disabling the guard for the rest of the run.
        reference_step = prior[-1].step
        prior_blocks = _prior_block_count(ledger, target)
        escalated = prior_blocks >= _ESCALATION_THRESHOLD
        if escalated:
            recovered = _semantic_progress_after(ledger, reference_step, target)
        else:
            recovered = _state_changed_after(ledger, reference_step)
        if recovered:
            return None
        if escalated:
            message = (
                "Repeated evidence-display commands targeted the same artifact after repeated warnings "
                "and repair attempts. A write alone no longer clears this: run a command that actually "
                "exercises the artifact (executes it, validates it, or tests it against its real target), "
                "inspect a different artifact, or declare a concrete blocker."
            )
        else:
            message = (
                "Repeated evidence-display commands targeted the same unchanged artifact. "
                "Next action must repair the artifact, run semantic validation, inspect a new target, "
                "or declare a concrete blocker."
            )
        return NoProgressDecision(
            consequence="advisory",
            reason_code="repeated_evidence_display_no_state_change",
            target=target,
            action_family="evidence_display_command",
            repeat_count=len(prior) + 1,
            prior_receipt_ids=tuple(receipt.receipt_id for receipt in prior[-4:]),
            message=message,
        )

    def _evaluate_repeated_artifact_inspection(
        self,
        action: ActionRequest,
        ledger: ExecutionLedger,
    ) -> NoProgressDecision | None:
        path = str(action.arguments.get("path", "")).strip().removeprefix("/app/")
        mode = str(action.arguments.get("mode", "")).strip() or "default"
        if not path:
            return None
        prior = [
            receipt for receipt in ledger.all_receipts()
            if receipt.kind == "artifact_inspection"
            and str((receipt.payload or {}).get("path", "")).strip().removeprefix("/app/") == path
            and str((receipt.payload or {}).get("mode", "")).strip() in {mode, ""}
        ]
        if len(prior) < self.max_evidence_display_repeats:
            return None
        reference_step = prior[-1].step
        if _state_changed_after(ledger, reference_step):
            return None
        return NoProgressDecision(
            consequence="advisory",
            reason_code="repeated_artifact_inspection_no_state_change",
            target=f"{path}:{mode}",
            action_family="artifact_inspection",
            repeat_count=len(prior) + 1,
            prior_receipt_ids=tuple(receipt.receipt_id for receipt in prior[-4:]),
            message=(
                "Repeated artifact inspection targeted the same unchanged artifact. "
                "Use the surfaced metadata/text, inspect a different region/artifact, "
                "switch capability, produce a new semantic extraction, or declare a concrete blocker."
            ),
        )

    def _evaluate_repeated_service_probe(
        self,
        action: ActionRequest,
        ledger: ExecutionLedger,
    ) -> NoProgressDecision | None:
        target = str(action.arguments.get("target", "")).strip()
        if not target:
            return None
        prior = [
            receipt for receipt in ledger.all_receipts()
            if receipt.kind == "service_probe"
            and str((receipt.payload or {}).get("target", "")).strip() == target
        ]
        failed_prior = [receipt for receipt in prior if not receipt.success]
        if len(failed_prior) < self.max_evidence_display_repeats:
            return None
        reference_step = failed_prior[-1].step
        if _state_changed_after(ledger, reference_step):
            return None
        return NoProgressDecision(
            consequence="advisory",
            reason_code="repeated_service_probe_no_state_change",
            target=target,
            action_family="service_probe",
            repeat_count=len(failed_prior) + 1,
            prior_receipt_ids=tuple(receipt.receipt_id for receipt in failed_prior[-4:]),
            message=(
                "Repeated service probes targeted the same endpoint without a state change. "
                "Inspect process/log evidence, relaunch or repair the service, run a semantic client check, "
                "or declare a concrete blocker instead of probing again."
            ),
        )

    @staticmethod
    def receipt(decision: NoProgressDecision, *, step: int, action_id: str) -> Receipt:
        return Receipt(
            receipt_id=f"step-{step}:{action_id}:no_progress_control",
            step=step,
            kind="no_progress_control",
            success=False,
            summary=decision.message,
            failure_class=decision.reason_code,
            payload=decision.as_dict(),
        )


def _evidence_display_target(command: str) -> str:
    command = command.strip()
    if not _DISPLAY_RE.search(command):
        return ""
    paths = [match.group("path") for match in _PATH_RE.finditer(command)]
    if not paths:
        return ""
    # Use the last concrete file target; commands often include echoed labels
    # before the actual inspected file.
    target = paths[-1]
    return target.removeprefix("/app/")


def _matching_display_receipts(ledger: ExecutionLedger, target: str) -> list[Receipt]:
    rows: list[Receipt] = []
    for receipt in ledger.all_receipts():
        if receipt.kind != "run_command":
            continue
        command = str((receipt.payload or {}).get("command", ""))
        if _evidence_display_target(command) == target:
            rows.append(receipt)
    return rows


def _state_changed_after(ledger: ExecutionLedger, step: int) -> bool:
    return any(receipt.step > step and receipt.state_change for receipt in ledger.all_receipts())


def _prior_block_count(ledger: ExecutionLedger, target: str) -> int:
    count = 0
    for receipt in ledger.all_receipts():
        if receipt.kind != "no_progress_control":
            continue
        if (receipt.payload or {}).get("target") == target:
            count += 1
    return count


def _semantic_progress_after(ledger: ExecutionLedger, step: int, target: str) -> bool:
    """True if a non-display command actually exercised ``target`` after ``step``.

    A write or generic state change is not enough once escalated: the target
    must have been referenced by a command that is not itself an evidence-
    display command (e.g. running/validating it, not just reading it back).
    """
    for receipt in ledger.all_receipts():
        if receipt.step <= step or receipt.kind != "run_command":
            continue
        payload = receipt.payload or {}
        command = str(payload.get("command", "")).strip()
        if not command or target not in command:
            continue
        if _DISPLAY_RE.search(command):
            continue
        if receipt.success:
            return True
    return False
