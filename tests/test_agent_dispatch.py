"""One-shot dispatch: record the work, then send an agent to do it.

The two halves already existed — `canopy agent add` writes a board task,
`POST /api/harness/turns/` enqueues a runner turn — but only as two raw calls,
with the sharp edges documented in prose inside Ada's conduct skill and nowhere
else. Anyone else wiring this up re-discovers them. These tests pin the edges
into the code:

  - a self-targeted turn without a `thread_key` types the prompt into the
    caller's OWN live session instead of a fresh one;
  - a `done` harness turn means SPAWNED, not WORKED, and must never be reported
    as a completed outcome;
  - a double-dispatch of the same work must not spawn two sessions.
"""
import json

import pytest
from click.testing import CliRunner

from orchestrator.agent_dispatch import (
    DispatchError,
    build_turn_payload,
    derive_idempotency_key,
    summarize_turn,
)
from orchestrator.cli import main


# --- the payload -------------------------------------------------------------

def test_payload_carries_slug_prompt_and_origin():
    p = build_turn_payload("hal", prompt="fix the thing", idempotency_key="k1")
    assert p["agent_slug"] == "hal"
    assert p["prompt"] == "fix the thing"
    assert p["origin"] == "api"
    assert p["idempotency_key"] == "k1"


def test_every_dispatch_gets_a_thread_key_so_it_lands_in_a_FRESH_session():
    """Without `origin_ref.thread_key` the runner keys the session as `<agent>:main` —
    which for a self-targeted turn IS the caller's own live session, so the prompt gets
    typed into the conversation that sent it instead of spawning a new one. A one-shot
    dispatch always wants isolation, so the key is unconditional rather than a rule the
    caller has to remember for the one case that bites."""
    p = build_turn_payload("ada", prompt="x", idempotency_key="k1")
    assert p["origin_ref"]["thread_key"] == "k1"
    p2 = build_turn_payload("hal", prompt="x", idempotency_key="k2")
    assert p2["origin_ref"]["thread_key"] == "k2", "non-self targets need it too"


def test_empty_prompt_is_omitted_not_sent_blank():
    """No prompt = default board drain. An empty string is a different instruction."""
    assert "prompt" not in build_turn_payload("hal", prompt="", idempotency_key="k")


def test_task_ref_is_threaded_into_the_payload():
    p = build_turn_payload("hal", prompt="x", idempotency_key="k", task_ext_id="T7")
    assert p["origin_ref"]["task_ext_id"] == "T7"


@pytest.mark.parametrize("slug", ["", "  ", None])
def test_a_dispatch_needs_a_target(slug):
    with pytest.raises(DispatchError):
        build_turn_payload(slug, prompt="x", idempotency_key="k")


# --- idempotency -------------------------------------------------------------

def test_same_work_derives_the_same_key():
    """Re-running the same dispatch must not spawn a second session."""
    a = derive_idempotency_key("hal", "Fix the cursor", "2026-07-28")
    b = derive_idempotency_key("hal", "Fix the cursor", "2026-07-28")
    assert a == b


def test_different_work_or_day_derives_a_different_key():
    base = derive_idempotency_key("hal", "Fix the cursor", "2026-07-28")
    assert derive_idempotency_key("hal", "Fix something else", "2026-07-28") != base
    assert derive_idempotency_key("ada", "Fix the cursor", "2026-07-28") != base
    assert derive_idempotency_key("hal", "Fix the cursor", "2026-07-29") != base


def test_key_is_filesystem_and_url_safe():
    k = derive_idempotency_key("hal", "Fix: the/cursor — now!", "2026-07-28")
    assert k.replace("-", "").isalnum()


# --- honest reporting --------------------------------------------------------

def test_a_done_turn_is_reported_as_LAUNCHED_not_completed():
    """The launch turn flips to `done` within seconds with result_note 'created
    session ...'. That is the RUNNER finishing, not the agent's work succeeding.
    Reporting it as a completed outcome is a real, recorded miss (2026-07-23)."""
    s = summarize_turn({"id": "t1", "status": "done",
                        "result_note": "created session 'hal-api-abcd-0728-1400'"})
    assert s["state"] == "launched"
    assert s["verified"] is False
    assert "hal-api-abcd-0728-1400" in (s["session_name"] or "")
    assert "launched" in s["headline"].lower()
    assert "complete" not in s["headline"].lower()


def test_a_queued_turn_reports_as_queued():
    s = summarize_turn({"id": "t1", "status": "queued", "result_note": ""})
    assert s["state"] == "queued" and s["verified"] is False


def test_a_failed_turn_is_surfaced_as_failed():
    s = summarize_turn({"id": "t1", "status": "lost", "result_note": "lease expired"})
    assert s["state"] == "failed"
    assert "lease expired" in s["headline"]


# --- CLI ---------------------------------------------------------------------

def _fake_transport(calls, turn_status="done"):
    def transport(method, url, headers, data):
        body = json.loads(data) if data else None
        calls.append((method, url, body))
        if "/harness/turns/" in url and method == "POST":
            return 201, json.dumps({"id": "turn-123", "status": turn_status,
                                    "result_note": "created session 'hal-api-x-0728'"})
        if "/tasks" in url:
            return 200, json.dumps({"synced": 1})
        return 200, json.dumps([])
    return transport


def test_cli_dispatch_creates_the_task_then_enqueues_the_turn(monkeypatch):
    calls = []
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", _fake_transport(calls))

    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal",
                                  "--title", "Fix the cursor", "--prompt", "do it",
                                  "--json-output"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["turn"]["state"] == "launched"
    assert out["turn"]["verified"] is False
    assert out["task_ext_id"]

    posts = [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert len(posts) == 1, "must enqueue exactly one turn"
    assert posts[0][2]["prompt"] == "do it"
    assert posts[0][2]["origin_ref"]["thread_key"]

    # The board record has to exist BEFORE the agent is sent at it, or the agent
    # arrives to work an item that isn't on its board yet.
    task_calls = [i for i, c in enumerate(calls) if "/tasks" in c[1]]
    turn_calls = [i for i, c in enumerate(calls) if "/harness/turns/" in c[1] and c[0] == "POST"]
    assert task_calls and turn_calls and min(task_calls) < min(turn_calls)


def test_cli_no_task_skips_the_board_write(monkeypatch):
    calls = []
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", _fake_transport(calls))

    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal",
                                  "--prompt", "just go", "--no-task", "--json-output"])
    assert r.exit_code == 0, r.output
    assert not [c for c in calls if "/tasks" in c[1] and c[0] == "POST"]
    assert json.loads(r.output)["task_ext_id"] is None


def test_cli_requires_a_title_when_creating_a_task(monkeypatch):
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--prompt", "x"])
    assert r.exit_code != 0
    assert "--title" in r.output


def test_cli_human_output_says_launched_and_how_to_check(monkeypatch):
    """The whole point of 'launched (unverified)' is that the reader needs a next step."""
    calls = []
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", _fake_transport(calls))

    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal",
                                  "--title", "T", "--prompt", "p"])
    assert r.exit_code == 0, r.output
    assert "launched" in r.output.lower()
    assert "canopy agent turns" in r.output, "must name the verification command"


def test_cli_turns_lists_recent_turns(monkeypatch):
    def transport(method, url, headers, data):
        return 200, json.dumps([{"id": "t1", "agent_slug": "hal", "status": "done",
                                 "created_at": "2026-07-28T13:00:00Z",
                                 "result_note": "created session 'hal-api-x'",
                                 "prompt": "fix the cursor"}])
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)

    r = CliRunner().invoke(main, ["agent", "turns", "--slug", "hal"])
    assert r.exit_code == 0, r.output
    assert "t1" in r.output and "launched" in r.output.lower()


def test_a_claimed_or_running_turn_does_not_report_it_was_never_spawned():
    """`queued`, `claimed` and `running` share the pending bucket — none has finished
    spawning, so none may claim LAUNCHED. But they are not the same claim to a human:
    rendering `running` as "the runner has not spawned it yet" states the exact
    falsehood `canopy project turns` exists to disprove. dimagi-internal/canopy#433."""
    from orchestrator.agent_dispatch import summarize_turn

    unclaimed = summarize_turn({"id": "t", "status": "queued"})
    assert unclaimed["state"] == "queued"
    assert "no runner has claimed it yet" in unclaimed["headline"]

    for status in ("claimed", "running"):
        s = summarize_turn({"id": "t", "status": status})
        assert s["state"] == "queued", "still pending — must not be promoted to launched"
        assert "has not spawned" not in s["headline"], (
            f"{status} reported as never-spawned: {s['headline']!r}")
        assert "executing" in s["headline"]
