"""Boundaries for the fleet decide-don't-poll rail
(`plugins/canopy/agent-core/decide_guard.py`).

This rail's whole value is that it fires on sessions the close-out rail is
deliberately blind to — ad-hoc work, which is most of what Jonathan asks the agent for
directly. That makes over-firing the dominant risk: unlike the close-out rail it
has no turn-entry gate to keep it quiet, so the ONLY thing standing between it
and constant nagging is the narrowness of what it matches.

So the true positives below are the cheap half. The cases that matter are the
three shapes that must NEVER block:

1. **The sanctioned inline-prose ask.** The fleet's recommend-and-act rule tells
   the agent to state a recommendation and end with a plain question when a real fork
   exists ("I'd close it rather than merge — ok?"). Matching that would make the
   rail fire on the agent doing exactly the right thing.
2. **The outbound-email gate.** Sending mail to a person is the ONE action that
   always waits for a human. "Want me to send this?" is the agent working correctly.
3. **A passing mention.** The rule is about how a message CLOSES. An offer
   quoted or considered mid-report is not handing the call back.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

HOOK = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "canopy" / "agent-core" / "decide_guard.py"
)


def _load():
    loader = importlib.machinery.SourceFileLoader("decide_guard", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def guard(tmp_path, monkeypatch):
    """The hook with its state dir redirected into tmp_path."""
    mod = _load()
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "decide-guard")
    return mod


def _assistant(text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


def _tool_use(name: str = "Bash") -> str:
    return json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": {"command": "ls"}}],
        },
    })


def _transcript(tmp_path, *lines: str) -> str:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def _run(guard, monkeypatch, capsys, payload) -> dict | None:
    """Drive main() with `payload` on stdin; return the emitted JSON or None."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


# --- the case the rail exists for -------------------------------------------

@pytest.mark.parametrize("closing", [
    "Want me to ship the fix as a PR?",
    "Want me to kick the deploy, or delete the remnant?",
    "Shall I open the PR?",
    "Should I merge this now?",
    "Would you like me to deploy it?",
    "Do you want me to fix the other two as well?",
    "Let me know if you'd like me to take it further.",
    "Say the word and I'll delete it.",
])
def test_a_closing_offer_to_act_blocks(guard, closing):
    """Every one of these is the agent knowing the end state and making Jonathan say it.
    The first two are verbatim from the 2026-08-27 session that motivated the rail."""
    assert guard.hands_back_a_call(f"I diagnosed it to the line.\n\n{closing}")


def test_it_blocks_once_and_then_never_again(guard, tmp_path, monkeypatch, capsys):
    """Worst case must be one extra beat, never a session that cannot end."""
    t = _transcript(tmp_path, _assistant("Found it. Want me to fix it?"))
    payload = {"transcript_path": t, "session_id": "s1"}

    first = _run(guard, monkeypatch, capsys, payload)
    assert first is not None and first["decision"] == "block"
    assert "whose call is this actually" in first["reason"]

    assert _run(guard, monkeypatch, capsys, payload) is None


# --- the three shapes that must never block ---------------------------------

@pytest.mark.parametrize("closing", [
    "This PR is superseded; I'd close it rather than merge — ok?",
    "I went with the project-scoped key. Sound good?",
    "Both readings are defensible and it turns on how much you care about the"
    " label vs the fork. Which matters more to you?",
    "I'd rather not guess at your priorities here — is labs or ace the one to"
    " fix first?",
])
def test_the_sanctioned_prose_ask_does_not_block(guard, closing):
    """the fleet's recommend-and-act rule tells the agent to end a genuine fork exactly like
    this. A rail that fires on the right behaviour teaches the agent to dismiss it."""
    assert not guard.hands_back_a_call(f"Here is what I found.\n\n{closing}")


@pytest.mark.parametrize("closing", [
    "I've drafted the reply to the funder thread. Want me to send it?",
    "Draft is ready via bin/hal-email --reply-all. Should I send?",
    "Want me to send this to sophie@example.org?",
])
def test_the_outbound_email_gate_does_not_block(guard, closing):
    """The ONE thing that always waits for a human. the agent asking here is correct."""
    assert not guard.hands_back_a_call(f"Summary above.\n\n{closing}")


def test_an_offer_mentioned_mid_report_does_not_block(guard):
    """The rule is about how a message CLOSES. Beyond the tail window, an offer is
    narration — including narration about this very rail."""
    text = (
        "I considered ending with 'want me to ship it' and decided that was the"
        " wrong move, so I shipped it instead.\n\n"
        + "Then I verified the deploy and confirmed the row split correctly. " * 30
        + "\n\nMerged as 2b2cc78 and live on labs."
    )
    assert not guard.hands_back_a_call(text)


# --- fail open, always -------------------------------------------------------

def test_no_transcript_path_does_not_block(guard, monkeypatch, capsys):
    assert _run(guard, monkeypatch, capsys, {"session_id": "s1"}) is None


def test_an_unreadable_transcript_does_not_block(guard, tmp_path, monkeypatch, capsys):
    payload = {"transcript_path": str(tmp_path / "missing.jsonl"), "session_id": "s1"}
    assert _run(guard, monkeypatch, capsys, payload) is None


def test_a_corrupt_line_does_not_blind_the_scan(guard, tmp_path):
    t = _transcript(
        tmp_path,
        "{not json at all",
        _assistant("Done — merged and deployed."),
        "}{",
        _assistant("Want me to also delete the remnant?"),
    )
    assert guard.hands_back_a_call(guard.final_assistant_text(t))


def test_an_unwritable_state_dir_fails_open(guard, tmp_path, monkeypatch, capsys):
    """`already_blocked` reporting True on failure is what keeps a broken state dir
    from becoming a session that can never stop."""
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "nope" / "deeper")
    monkeypatch.setattr(guard.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError))
    t = _transcript(tmp_path, _assistant("Want me to do it?"))
    assert _run(guard, monkeypatch, capsys, {"transcript_path": t, "session_id": "s2"}) is None


def test_a_missing_session_id_does_not_block(guard, tmp_path, monkeypatch, capsys):
    t = _transcript(tmp_path, _assistant("Want me to do it?"))
    assert _run(guard, monkeypatch, capsys, {"transcript_path": t}) is None


# --- it reads the right message ---------------------------------------------

def test_the_last_TEXT_message_wins_not_the_last_record(guard, tmp_path):
    """A message whose last act was a tool call has not handed anything back; the
    trailing tool_use records must not hide the text that preceded them."""
    t = _transcript(
        tmp_path,
        _assistant("Want me to look into it?"),
        _assistant("Never mind — I looked, fixed it, and shipped it."),
        _tool_use(),
    )
    assert guard.final_assistant_text(t) == "Never mind — I looked, fixed it, and shipped it."
    assert not guard.hands_back_a_call(guard.final_assistant_text(t))


def test_a_superseded_offer_does_not_block(guard, tmp_path):
    """An offer made mid-session and then acted on must not block at the end."""
    t = _transcript(
        tmp_path,
        _assistant("Want me to ship it?"),
        _assistant("Shipped as #624, merged, deployed, verified live."),
    )
    assert not guard.hands_back_a_call(guard.final_assistant_text(t))
