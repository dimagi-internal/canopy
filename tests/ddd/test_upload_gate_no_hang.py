"""The external_release gate must never silently hang.

Regression for the Nutrition Demo freeze (2026-07-22): "publish it" ran the
release gate, which posted a review and then polled canopy-web for 24h in
silence for a UI click that a non-interactive run can never make. These tests
lock in that a non-interactive caller HOLDS instead of blocking, and that the
interactive path is bounded + heartbeated.
"""
from __future__ import annotations

import types

import pytest

import scripts.ddd.review as rv
import scripts.ddd.upload as up


def _post_stub(*_a, **_k):
    return {"id": "rev1", "url": "http://canopy/review/rev1/", "workspace": "connect"}


def test_non_interactive_holds_without_blocking(monkeypatch):
    """stdin is not a TTY → the gate returns 'hold' and NEVER calls the poll."""
    monkeypatch.setattr(rv, "post_review_request", _post_stub)

    def _must_not_run(*_a, **_k):
        raise AssertionError("await_resolution must not be called non-interactively")

    monkeypatch.setattr(rv, "await_resolution", _must_not_run)
    monkeypatch.setattr(up.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))

    assert up._default_gate(object(), None, None) == "hold"


def test_interactive_waits_bounded_with_heartbeat(monkeypatch):
    """A TTY caller polls (bounded) and gets the resolved decision; on_wait wired."""
    monkeypatch.setattr(rv, "post_review_request", _post_stub)
    seen = {}

    def _fake_await(review_id, *, base_url=None, token=None, on_wait=None):
        seen["on_wait"] = on_wait
        return {"publish": "publish"}

    monkeypatch.setattr(rv, "await_resolution", _fake_await)
    monkeypatch.setattr(up.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))

    review_request = types.SimpleNamespace(decisions=[types.SimpleNamespace(id="publish")])
    assert up._default_gate(review_request, None, None) == "publish"
    assert callable(seen["on_wait"]), "interactive wait must pass a heartbeat callback"


def test_await_resolution_default_timeout_is_bounded():
    """The default poll timeout is minutes, not a silent day."""
    import inspect

    default = inspect.signature(rv.await_resolution).parameters["timeout"].default
    assert default <= 3600, f"default timeout {default}s is too long (was a 24h hang)"


def test_await_resolution_calls_on_wait_each_poll(monkeypatch):
    """on_wait fires before each sleep so the wait is never silent."""
    calls = {"review": 0}

    def _get_review(_review_id, **_k):
        calls["review"] += 1
        # resolve on the 3rd poll
        return {"status": "resolved", "response_json": {"publish": "publish"}} if calls["review"] >= 3 else {"status": "pending"}

    monkeypatch.setattr(rv, "get_review", _get_review)
    clock = [0.0]
    waits: list[float] = []
    out = rv.await_resolution(
        "rev1",
        poll_interval=1.0,
        timeout=100.0,
        on_wait=lambda e: waits.append(e),
        _sleep=lambda _s: None,
        _now=lambda: clock.__setitem__(0, clock[0] + 1.0) or clock[0],
    )
    assert out == {"publish": "publish"}
    assert len(waits) >= 1, "on_wait must fire while polling"
