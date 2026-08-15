"""Gate resolution that can never hang an unattended run.

A DDD gate posts a ``ReviewRequest`` to canopy-web and waits for a human to click
something.  There are exactly two: ``concept_change`` and ``external_release``.
``external_release`` was already made hang-proof (``upload._default_gate``: a
non-TTY caller HOLDS instead of polling for a click nobody will make).
``concept_change`` was not — the orchestrator was told to poll
``review.await_resolution``, which in an unattended run stalls for 30 minutes and
then raises.  So any finding routed to CONCEPT dead-ended.

Autonomous is the primary mode, so blocking forever on a click nobody will make
is not a safe default.  This module states, in one place, what each gate does
when there is no human:

======================  ====================  ============================================
gate                    unattended default    why
======================  ====================  ============================================
``concept_change``      ``defer``             Never block. The review stays posted and
                                              resolvable; the run terminates and reports
                                              honestly rather than stalling. Nothing is
                                              published, so deferring costs nothing.
``external_release``    ``hold``              Never publish to external humans without an
                                              approval. Holding is the safe direction here
                                              — the asymmetry is deliberate.
======================  ====================  ============================================

Both defaults are *terminal and honest*: the run ends, says what it could not
decide, and leaves a resolvable review behind.  Neither is a silent drop and
neither is a wait.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

CONCEPT_CHANGE = "concept_change"
EXTERNAL_RELEASE = "external_release"

#: The one place the no-human answer for each gate is written down.
UNATTENDED_DEFAULT: dict[str, str] = {
    CONCEPT_CHANGE: "defer",
    EXTERNAL_RELEASE: "hold",
}

#: Bounded wait for an ATTENDED caller. Never a silent day.
ATTENDED_TIMEOUT_S: float = 1800.0


@dataclass(frozen=True)
class GateOutcome:
    """What a gate decided, and whether a human actually decided it."""

    gate: str
    decision: str
    resolved_by: str  # "human" | "unattended_default" | "timeout_default" | "preapproved"
    review_id: str | None = None
    review_url: str | None = None
    reason: str = ""

    @property
    def blocked(self) -> bool:
        """True only when a human's answer is still genuinely outstanding."""
        return self.resolved_by in ("unattended_default", "timeout_default")

    def as_dict(self) -> dict:
        return {
            "gate": self.gate,
            "decision": self.decision,
            "resolved_by": self.resolved_by,
            "review_id": self.review_id,
            "review_url": self.review_url,
            "reason": self.reason,
            "blocked": self.blocked,
        }


def is_unattended(stdin=None) -> bool:
    """No human in the loop -> stdin is not a TTY.

    Same detection ``upload._default_gate`` already uses for
    ``external_release``, lifted here so both gates answer the question the same
    way instead of one of them not asking it at all.
    """
    stream = stdin if stdin is not None else sys.stdin
    try:
        return not stream.isatty()
    except Exception:  # pragma: no cover - exotic stdin replacements
        return True


def default_for(gate: str) -> str:
    """The declared no-human decision for ``gate``."""
    try:
        return UNATTENDED_DEFAULT[gate]
    except KeyError:  # pragma: no cover - guarded by test_gates
        raise ValueError(
            f"unknown gate {gate!r} — every gate must declare an unattended default "
            f"(known: {sorted(UNATTENDED_DEFAULT)})"
        ) from None


def resolve(
    gate: str,
    *,
    review_id: str | None = None,
    review_url: str | None = None,
    preapproved: str | None = None,
    unattended: bool | None = None,
    await_fn=None,
    timeout: float = ATTENDED_TIMEOUT_S,
    on_wait=None,
    echo=None,
) -> GateOutcome:
    """Resolve one gate without ever hanging.

    Parameters
    ----------
    gate:
        ``"concept_change"`` or ``"external_release"``.
    preapproved:
        A decision the operator already gave in-session (e.g.
        ``--release-approved``).  Returned immediately, attributed as
        ``"preapproved"`` — recorded, not bypassed.
    unattended:
        Override the TTY detection.  ``None`` (default) auto-detects.
    await_fn:
        ``callable(review_id, timeout=..., on_wait=...) -> decision | None``.
        Only ever called for an ATTENDED caller with a ``review_id``.  Raising
        ``TimeoutError`` is expected and handled — it becomes the gate's declared
        default with ``resolved_by="timeout_default"``, never an exception out of
        this function.
    echo:
        ``callable(str)`` for operator-visible messages (default: stderr).

    Returns
    -------
    GateOutcome
        Always. This function does not raise for an unresolved gate — an
        unresolved gate is a *result*, not an error.
    """
    say = echo if echo is not None else (lambda m: print(m, file=sys.stderr, flush=True))
    fallback = default_for(gate)

    if preapproved:
        return GateOutcome(
            gate=gate,
            decision=preapproved,
            resolved_by="preapproved",
            review_id=review_id,
            review_url=review_url,
            reason="operator approved in-session; the review is still recorded and attributed",
        )

    if unattended is None:
        unattended = is_unattended()

    if unattended or not review_id or await_fn is None:
        say(
            f"[{gate}] no interactive human — not waiting on a UI click. "
            f"Decision defaults to {fallback!r}."
            + (f" Review stays open: {review_url}" if review_url else "")
        )
        return GateOutcome(
            gate=gate,
            decision=fallback,
            resolved_by="unattended_default",
            review_id=review_id,
            review_url=review_url,
            reason=(
                f"unattended run — {gate} defaults to {fallback!r} rather than blocking on a "
                "click nobody will make; the review remains resolvable"
            ),
        )

    say(f"[{gate}] waiting up to {int(timeout)}s for a decision — {review_url or review_id}")
    try:
        decision = await_fn(review_id, timeout=timeout, on_wait=on_wait)
    except TimeoutError:
        decision = None
    if not decision:
        say(f"[{gate}] no decision within {int(timeout)}s — defaulting to {fallback!r}.")
        return GateOutcome(
            gate=gate,
            decision=fallback,
            resolved_by="timeout_default",
            review_id=review_id,
            review_url=review_url,
            reason=f"attended wait timed out after {int(timeout)}s; defaulted to {fallback!r}",
        )
    return GateOutcome(
        gate=gate,
        decision=str(decision),
        resolved_by="human",
        review_id=review_id,
        review_url=review_url,
        reason="resolved on the canopy-web review surface",
    )


__all__ = [
    "ATTENDED_TIMEOUT_S",
    "CONCEPT_CHANGE",
    "EXTERNAL_RELEASE",
    "GateOutcome",
    "UNATTENDED_DEFAULT",
    "default_for",
    "is_unattended",
    "resolve",
]
