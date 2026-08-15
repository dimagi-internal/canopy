"""No DDD gate may hang an unattended run.

``external_release`` was fixed for this in 0.2.x (``upload._default_gate``:
non-TTY -> hold). ``concept_change`` was not — the orchestrator was told to poll
``review.await_resolution``, which stalls 30 minutes and then raises. Autonomous
is the primary mode, so a click nobody will make must never be a blocking wait.
"""
from __future__ import annotations

import types

import pytest

from scripts.ddd import gates


def _tty(is_tty: bool):
    return types.SimpleNamespace(isatty=lambda: is_tty)


class TestPolicyTable:
    def test_every_gate_declares_an_unattended_default(self) -> None:
        assert set(gates.UNATTENDED_DEFAULT) == {gates.CONCEPT_CHANGE, gates.EXTERNAL_RELEASE}

    def test_concept_change_defers_never_blocks(self) -> None:
        assert gates.default_for(gates.CONCEPT_CHANGE) == "defer"

    def test_external_release_holds_never_publishes(self) -> None:
        """The asymmetry is deliberate: never publish externally without approval."""
        assert gates.default_for(gates.EXTERNAL_RELEASE) == "hold"

    def test_unknown_gate_is_loud(self) -> None:
        with pytest.raises(ValueError, match="unattended default"):
            gates.default_for("made_up_gate")


class TestUnattended:
    def test_non_tty_is_unattended(self) -> None:
        assert gates.is_unattended(_tty(False)) is True

    def test_tty_is_attended(self) -> None:
        assert gates.is_unattended(_tty(True)) is False

    def test_concept_gate_never_polls_unattended(self) -> None:
        def _must_not_run(*_a, **_k):
            raise AssertionError("await_fn must not be called in an unattended run")

        out = gates.resolve(
            gates.CONCEPT_CHANGE,
            review_id="rev1",
            review_url="http://canopy/review/rev1/",
            unattended=True,
            await_fn=_must_not_run,
            echo=lambda _m: None,
        )
        assert out.decision == "defer"
        assert out.resolved_by == "unattended_default"
        assert out.blocked is True
        assert out.review_url == "http://canopy/review/rev1/"

    def test_release_gate_holds_unattended(self) -> None:
        out = gates.resolve(gates.EXTERNAL_RELEASE, review_id="rev1", unattended=True, echo=lambda _m: None)
        assert out.decision == "hold"
        assert out.blocked is True


class TestAttended:
    def test_a_human_decision_is_returned_and_attributed(self) -> None:
        out = gates.resolve(
            gates.CONCEPT_CHANGE,
            review_id="rev1",
            unattended=False,
            await_fn=lambda _rid, **_k: "approve",
            echo=lambda _m: None,
        )
        assert out.decision == "approve"
        assert out.resolved_by == "human"
        assert out.blocked is False

    def test_a_timeout_becomes_the_default_not_an_exception(self) -> None:
        def _timeout(*_a, **_k):
            raise TimeoutError("no click")

        out = gates.resolve(
            gates.CONCEPT_CHANGE,
            review_id="rev1",
            unattended=False,
            await_fn=_timeout,
            echo=lambda _m: None,
        )
        assert out.decision == "defer"
        assert out.resolved_by == "timeout_default"
        assert out.blocked is True

    def test_attended_wait_is_bounded_in_minutes(self) -> None:
        assert gates.ATTENDED_TIMEOUT_S <= 3600

    def test_the_bound_is_passed_through(self) -> None:
        seen: dict = {}

        def _await(rid, *, timeout=None, on_wait=None):
            seen["timeout"] = timeout
            return "approve"

        gates.resolve(
            gates.CONCEPT_CHANGE,
            review_id="rev1",
            unattended=False,
            await_fn=_await,
            echo=lambda _m: None,
        )
        assert seen["timeout"] == gates.ATTENDED_TIMEOUT_S


class TestPreapproval:
    def test_an_in_session_approval_short_circuits_without_bypassing_the_record(self) -> None:
        out = gates.resolve(
            gates.EXTERNAL_RELEASE,
            review_id="rev1",
            preapproved="publish",
            echo=lambda _m: None,
        )
        assert out.decision == "publish"
        assert out.resolved_by == "preapproved"
        assert out.blocked is False
        assert out.review_id == "rev1"


def test_resolve_never_raises_for_an_unresolved_gate() -> None:
    """An unresolved gate is a RESULT, not an error — that is the whole contract."""
    for gate in gates.UNATTENDED_DEFAULT:
        out = gates.resolve(gate, unattended=True, echo=lambda _m: None)
        assert isinstance(out, gates.GateOutcome)
        assert out.decision == gates.default_for(gate)
        assert out.as_dict()["blocked"] is True
