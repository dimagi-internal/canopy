"""Unit tests for ``wait_for``'s per-action ``seconds:`` timeout override.

Two surfaces to pin:

  1. The Pydantic schema accepts ``seconds`` on a ``WaitForAction`` (so spec
     authors can write ``{kind: wait_for, target: X, seconds: 120}`` and have
     the YAML validate clean), and rejects non-numeric junk.
  2. The recorder's ``wait_for`` primitive + dispatcher route honour
     ``seconds`` — passing it through to ``wait_for_target(timeout_ms=...)``
     as ``int(seconds*1000)``. ``None`` falls back to
     ``RecorderConfig.wait_for_timeout_ms`` (12000ms default).

The motivation lives in the microplans-10-wards recording: the bulk-create
success card was fully painted ~55s after the click, but the recorder's
default 12s ``wait_for`` timeout fired first, so the spec had a blind
``hold seconds: 90`` to cover the worst case. With ``seconds: 120``, the
recorder exits the wait the moment the text appears and saves 50-100s of
dead air on the clip.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import TypeAdapter, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.narrative.models import Action, WaitForAction  # noqa: E402
from scripts.walkthrough._lib.config import RecorderConfig  # noqa: E402
from scripts.walkthrough._lib.recorder import execute_action, wait_for  # noqa: E402

ACTION = TypeAdapter(Action)


# ---- Pydantic schema: ``seconds`` is a real field on WaitForAction --------


def test_wait_for_action_accepts_seconds_float():
    a = ACTION.validate_python({"kind": "wait_for", "target": "Done", "seconds": 60.0})
    assert isinstance(a, WaitForAction)
    assert a.seconds == 60.0


def test_wait_for_action_seconds_defaults_to_none():
    """``seconds: None`` preserves the default 12s timeout — back-compat with
    every existing wait_for in the corpus that doesn't specify a timeout."""
    a = ACTION.validate_python({"kind": "wait_for", "target": "Done"})
    assert isinstance(a, WaitForAction)
    assert a.seconds is None


def test_wait_for_action_accepts_integer_seconds():
    """YAML ``seconds: 120`` (an int) coerces to float on the model."""
    a = ACTION.validate_python({"kind": "wait_for", "target": "Done", "seconds": 120})
    assert a.seconds == 120.0


def test_wait_for_action_rejects_non_numeric_seconds():
    """``seconds: fast`` is a typo, not a sentinel — fail loud at validation."""
    with pytest.raises(ValidationError):
        ACTION.validate_python({"kind": "wait_for", "target": "Done", "seconds": "fast"})


def test_wait_for_action_with_seconds_round_trips():
    """Real-world spec shape: a click + a long-timeout wait_for after."""
    raw = {
        "kind": "wait_for",
        "target": "Created 10 of 10 plans",
        "seconds": 120,
        "note": "exit early when bulk-create finishes",
    }
    a = ACTION.validate_python(raw)
    assert isinstance(a, WaitForAction)
    assert a.seconds == 120.0
    assert a.target == "Created 10 of 10 plans"
    assert a.note.startswith("exit early")


# ---- Recorder ``wait_for`` primitive forwards seconds → timeout_ms --------


def test_wait_for_primitive_with_seconds_overrides_default_timeout():
    """``seconds=120`` becomes ``timeout_ms=120000`` at ``wait_for_target``."""
    page = object()  # opaque — the patch makes its identity irrelevant
    with patch(
        "scripts.walkthrough._lib.recorder.wait_for_target", return_value=True
    ) as mock_wft:
        ok = wait_for(page, "Done", seconds=120.0)
    assert ok is True
    mock_wft.assert_called_once()
    _, kwargs = mock_wft.call_args
    assert kwargs["timeout_ms"] == 120000


def test_wait_for_primitive_without_seconds_uses_config_default():
    """``seconds=None`` falls back to ``RecorderConfig.wait_for_timeout_ms``."""
    page = object()
    with patch(
        "scripts.walkthrough._lib.recorder.wait_for_target", return_value=True
    ) as mock_wft:
        ok = wait_for(page, "Done")
    assert ok is True
    _, kwargs = mock_wft.call_args
    assert kwargs["timeout_ms"] == RecorderConfig().wait_for_timeout_ms  # 12000


def test_wait_for_primitive_respects_custom_config_default():
    """A spec with ``video_recorder_config: {wait_for_timeout_ms: 30000}``
    sets the default — ``seconds=None`` should pick that up, not the
    dataclass default."""
    page = object()
    custom_cfg = RecorderConfig(wait_for_timeout_ms=30000)
    with patch(
        "scripts.walkthrough._lib.recorder.wait_for_target", return_value=True
    ) as mock_wft:
        wait_for(page, "Done", config=custom_cfg)
    _, kwargs = mock_wft.call_args
    assert kwargs["timeout_ms"] == 30000


def test_wait_for_primitive_negative_seconds_floors_to_zero():
    """Negative ``seconds`` is treated as "don't wait" — flooring beats
    propagating a Playwright assertion error from a timeout < 0."""
    page = object()
    with patch(
        "scripts.walkthrough._lib.recorder.wait_for_target", return_value=True
    ) as mock_wft:
        wait_for(page, "Done", seconds=-5.0)
    _, kwargs = mock_wft.call_args
    assert kwargs["timeout_ms"] == 0


def test_wait_for_primitive_fractional_seconds_coerce_to_int_ms():
    """``seconds=0.25`` → 250ms (int) — wait_for_target's signature is int."""
    page = object()
    with patch(
        "scripts.walkthrough._lib.recorder.wait_for_target", return_value=True
    ) as mock_wft:
        wait_for(page, "Done", seconds=0.25)
    _, kwargs = mock_wft.call_args
    assert kwargs["timeout_ms"] == 250
    assert isinstance(kwargs["timeout_ms"], int)


# ---- Dispatcher routes ``seconds`` from the action dict to wait_for -------


def test_dispatcher_forwards_seconds_to_wait_for():
    """``execute_action({"kind": "wait_for", "target": X, "seconds": 60})``
    must reach the underlying ``wait_for_target`` call with
    ``timeout_ms=60000``. This is the contract the spec author depends on."""
    page = object()
    with patch(
        "scripts.walkthrough._lib.recorder.wait_for_target", return_value=True
    ) as mock_wft:
        r = execute_action(
            page,
            {"kind": "wait_for", "target": "Created 10 of 10 plans", "seconds": 60.0},
        )
    assert r.ok is True
    assert r.kind == "wait_for"
    _, kwargs = mock_wft.call_args
    assert kwargs["timeout_ms"] == 60000


def test_dispatcher_wait_for_without_seconds_uses_config_default():
    """No ``seconds`` on the action → default timeout. Back-compat: every
    existing wait_for in every spec keeps recording identically."""
    page = object()
    with patch(
        "scripts.walkthrough._lib.recorder.wait_for_target", return_value=True
    ) as mock_wft:
        execute_action(page, {"kind": "wait_for", "target": "Done"})
    _, kwargs = mock_wft.call_args
    assert kwargs["timeout_ms"] == RecorderConfig().wait_for_timeout_ms


def test_dispatcher_wait_for_timeout_tags_error_kind():
    """A timeout (``wait_for_target`` returns False) is reported with
    ``error_kind='timeout'`` regardless of whether seconds was custom or default."""
    page = object()
    with patch(
        "scripts.walkthrough._lib.recorder.wait_for_target", return_value=False
    ):
        r = execute_action(
            page, {"kind": "wait_for", "target": "Never appears", "seconds": 0.1}
        )
    assert r.ok is False
    assert r.error_kind == "timeout"
