"""Evidence aware model route escalation decision helper, gated by a flag that defaults to off."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class EscalationDecision:
    escalate: bool
    target_route: str | None
    reason_codes: tuple[str, ...]


def decide_escalation(
    *,
    enabled: bool,
    consecutive_no_progress_steps: int,
    verification_rounds_with_unresolved_relevant_evidence: int,
    current_route: str,
    escalation_route: str,
    no_progress_threshold: int = 3,
    verification_round_threshold: int = 2,
) -> EscalationDecision:
    """Decide whether to escalate the model route based on observable signals.

    Inert when `enabled` is False (the default everywhere this is wired in):
    always returns `escalate=False` with no reason codes, regardless of the
    other inputs. When `enabled` is True, escalates only when either repeated
    semantic no-progress steps or repeated verification rounds with unresolved
    relevant evidence cross their thresholds, and the current route differs
    from the escalation route.
    """

    if not enabled:
        return EscalationDecision(escalate=False, target_route=None, reason_codes=())

    reason_codes: list[str] = []
    if consecutive_no_progress_steps >= no_progress_threshold:
        reason_codes.append("repeated_no_progress")
    if verification_rounds_with_unresolved_relevant_evidence >= verification_round_threshold:
        reason_codes.append("repeated_unresolved_relevant_evidence")

    if not reason_codes:
        return EscalationDecision(escalate=False, target_route=None, reason_codes=())

    if current_route == escalation_route:
        # Already on the escalation route; nothing further to do, but the
        # signals were real so report them.
        return EscalationDecision(escalate=False, target_route=None, reason_codes=tuple(reason_codes))

    return EscalationDecision(
        escalate=True,
        target_route=escalation_route,
        reason_codes=tuple(reason_codes),
    )


def apply_escalation(
    model_route: Mapping[str, Any],
    decision: EscalationDecision,
    *,
    escalation_route_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a (possibly unchanged) model route config reflecting `decision`.

    When `decision.escalate` is False, returns `model_route` unchanged (as a
    plain dict copy). When True and `escalation_route_config` is provided,
    returns that config instead; this function never mutates its inputs.
    """

    if not decision.escalate:
        return dict(model_route)
    if escalation_route_config is None:
        return dict(model_route)
    return dict(escalation_route_config)
