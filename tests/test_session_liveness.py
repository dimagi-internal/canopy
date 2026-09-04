"""Liveness detection for still-running sessions (canopy#593).

The behaviour under test is a WRONG NUMBER, not an error: every case here would
otherwise produce a confident false "this turn skipped a step" finding, which is the
failure `agent_review`'s own comments record as dispatching work at an agent.
"""
from __future__ import annotations

import os
import time

import pytest

from orchestrator.session_liveness import (
    DEFAULT_RECENT_MIN,
    Liveness,
    live_session_ids,
    mark_live,
    recent_window_seconds,
)

SID = "96444cd8-d035-4536-af9d-9da49f2b356c"
OTHER = "3efbbc75-71fb-49ee-ac14-a4771085dff6"


def _transcript(tmp_path, sid, age_seconds=0.0):
    p = tmp_path / f"{sid}.jsonl"
    p.write_text('{"type":"user"}\n', encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(p, (old, old))
    return p


# --- the two sources, and why neither alone is enough -------------------------------

def test_live_process_beats_a_stale_transcript(tmp_path):
    """THE case canopy#593's own proposed fix would have got wrong.

    A manual-mode turn parked at an approval gate writes nothing while it waits on a
    human. Measured 2026-09-04: hal session 96444cd8 sat 56.8 minutes stale and was a
    live process throughout. mtime alone calls that finished and scores its checklist.
    """
    t = _transcript(tmp_path, SID, age_seconds=57 * 60)
    lv = Liveness(frozenset({SID}), degraded=False)
    assert lv.is_live(t, window=DEFAULT_RECENT_MIN * 60) is True


def test_recent_transcript_beats_an_absent_process(tmp_path):
    """The resume gap: mid-`--resume` a session has no process at all, and that gap is
    exactly when a recovery dispatch fires."""
    t = _transcript(tmp_path, SID, age_seconds=5)
    lv = Liveness(frozenset(), degraded=False)
    assert lv.is_live(t, window=DEFAULT_RECENT_MIN * 60) is True


def test_finished_session_is_not_live(tmp_path):
    """Neither source fires -> gradeable. Without this the fix is a no-op that
    suppresses every checklist and looks like a clean bill of health."""
    t = _transcript(tmp_path, SID, age_seconds=6 * 3600)
    lv = Liveness(frozenset({OTHER}), degraded=False)
    assert lv.is_live(t, window=DEFAULT_RECENT_MIN * 60) is False


# --- enumeration --------------------------------------------------------------------

def test_enumerates_both_session_id_and_resume():
    """`--resume` is load-bearing: a resumed session carries no other scope, and
    missing it is what made the original argv check report 0 while four turns ran."""
    ps = (
        f"claude --session-id {SID} --model opus\n"
        f"/usr/local/bin/claude --resume {OTHER}\n"
        "grep claude something-else\n"
    )
    lv = live_session_ids(runner=lambda: ps, env={})
    assert lv.live_ids == frozenset({SID, OTHER})
    assert lv.degraded is False


def test_unreadable_process_table_is_degraded_not_empty():
    """'I could not look' must never render as 'nobody is there' — live-turns.sh's
    invariant, and the one to test any future version against."""
    def boom():
        raise OSError("ps unavailable")

    lv = live_session_ids(runner=boom, env={})
    assert lv.live_ids == frozenset()
    assert lv.degraded is True


def test_session_ids_env_override_is_a_test_seam():
    lv = live_session_ids(runner=lambda: "", env={"CANOPY_LIVE_TURNS_SESSION_IDS": f"{SID} {OTHER}"})
    assert lv.live_ids == frozenset({SID, OTHER})


# --- window parsing -----------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "abc", "-5", None])
def test_malformed_window_falls_back_rather_than_raising(raw):
    env = {} if raw is None else {"CANOPY_LIVE_TURNS_RECENT_MIN": raw}
    assert recent_window_seconds(env=env) == DEFAULT_RECENT_MIN * 60


def test_window_is_configurable_and_shared_with_live_turns_sh():
    assert recent_window_seconds(env={"CANOPY_LIVE_TURNS_RECENT_MIN": "30"}) == 1800


# --- batch --------------------------------------------------------------------------

def test_mark_live_enumerates_once_for_the_batch(tmp_path):
    live = _transcript(tmp_path, SID, age_seconds=57 * 60)
    done = _transcript(tmp_path, OTHER, age_seconds=6 * 3600)
    lv = Liveness(frozenset({SID}), degraded=False)
    out = mark_live([live, done], liveness=lv)
    assert out[str(live)] is True
    assert out[str(done)] is False
