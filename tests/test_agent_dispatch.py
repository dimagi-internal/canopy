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
import datetime as dt
import json

import pytest
from click.testing import CliRunner

from orchestrator.agent_dispatch import (
    DispatchError,
    build_turn_payload,
    derive_idempotency_key,
    dispatched_by,
    summarize_turn,
)
from orchestrator.cli import main


# --- the payload -------------------------------------------------------------

def test_payload_carries_slug_prompt_and_origin():
    p = build_turn_payload("hal", prompt="fix the thing", idempotency_key="k1")
    assert p["agent_slug"] == "hal"
    assert p["prompt"].startswith("fix the thing")
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
    assert posts[0][2]["prompt"].startswith("do it")
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


# --- #488: a dispatched prompt must SAY it was dispatched ---------------------
#
# The runner hands the prompt to Claude Code as input, so the transcript records it as
# typed by a human — truthfully, from the harness's point of view. This layer is the only
# one that knows better, so it is the only one that can say so.

def test_build_turn_payload_stamps_the_dispatch_marker():
    from orchestrator.agent_dispatch import DISPATCH_MARKER, build_turn_payload

    payload = build_turn_payload("hal", prompt="do the thing", idempotency_key="k")
    assert payload["prompt"].startswith("do the thing")
    assert payload["prompt"].endswith(DISPATCH_MARKER)


def test_build_turn_payload_still_omits_an_absent_prompt():
    """An absent prompt means "drain your board" — stamping must not invent one."""
    from orchestrator.agent_dispatch import build_turn_payload

    assert "prompt" not in build_turn_payload("hal", prompt="", idempotency_key="k")
    assert "prompt" not in build_turn_payload("hal", prompt="   ", idempotency_key="k")


def test_stamp_dispatched_is_idempotent():
    """A prompt built by one helper and passed through another must not collect two."""
    from orchestrator.agent_dispatch import DISPATCH_MARKER, stamp_dispatched

    once = stamp_dispatched("brief")
    assert stamp_dispatched(once) == once
    assert once.count(DISPATCH_MARKER) == 1


def test_project_dispatch_payload_stamps_too():
    from orchestrator.agent_dispatch import DISPATCH_MARKER
    from orchestrator.project_dispatch import build_project_turn_payload

    payload = build_project_turn_payload("canopy", prompt="fix it", idempotency_key="k")
    assert payload["prompt"].endswith(DISPATCH_MARKER)


# --- dispatcher lineage: origin_ref.dispatched_by ---------------------------
# The provenance line and the `canopy:dispatched-by=` marker both live inside the
# PROMPT, so the sender is legible to the receiving agent and invisible to anything
# querying the queue. "Which turns did I dispatch?" had no answer.

def test_origin_ref_records_who_dispatched():
    p = build_turn_payload("hal", prompt="fix the thing", idempotency_key="k1",
                           sender="ada")
    assert p["origin_ref"]["dispatched_by"] == "ada"


def test_a_board_drain_carries_lineage_even_with_no_prompt():
    """The prompt-less drain has NO marker at all — it is the case with the least
    provenance, so it is exactly the one that must carry the field."""
    p = build_turn_payload("echo", prompt="", idempotency_key="k2", sender="ada")
    assert "prompt" not in p
    assert p["origin_ref"]["dispatched_by"] == "ada"


def test_lineage_is_the_sender_not_the_target():
    """thread_key encodes the TARGET, which is why it cannot answer this question:
    two agents dispatching to the same target produce the same-shaped key."""
    p = build_turn_payload("ace", prompt="x", idempotency_key="dispatch-ace-abc",
                           sender="hal")
    assert p["origin_ref"]["dispatched_by"] == "hal"
    assert "ace" in p["origin_ref"]["thread_key"]


def test_lineage_is_omitted_rather_than_blank_when_unknown():
    """An empty slug must not write dispatched_by:'' — a reader filtering on the key
    would then match a turn whose sender is genuinely unknown."""
    p = build_turn_payload("hal", prompt="x", idempotency_key="k3", sender="")
    assert "dispatched_by" not in p["origin_ref"]


def test_the_prompt_marker_and_the_field_agree():
    p = build_turn_payload("hal", prompt="do it", idempotency_key="k4", sender="ada")
    assert dispatched_by(p["prompt"]) == p["origin_ref"]["dispatched_by"] == "ada"


# --- --runner: pin the turn to one box ---------------------------------------
#
# Server semantics these mirror (canopy-web apps/harness/services.py::claim_next_turn):
# only the pinned runner may claim; the online guard sits ABOVE pin matching, so a
# paused/stale runner leaves the turn QUEUED; a pin bypasses target matching, so the
# server would hand an agent turn to a box that never declared that agent.

from orchestrator.agent_dispatch import check_runner_pin, resolve_runner  # noqa: E402

JJ_ID = "cb9d5262-52da-4d4d-a294-c59c47cd3e15"
HAL_ID = "7421b48c-2516-4cfe-8cf8-53d3b43332e9"
FLEET = ["ace", "ada", "echo", "eva", "hal"]


def _runner(name, rid, *, status="online", paused=False, agents=FLEET, ready=True,
            paused_note="", ready_note=""):
    caps = {"projects": []}
    if agents is not None:
        caps["agents"] = agents
    return {"id": rid, "name": name, "status": status, "paused": paused,
            "paused_note": paused_note, "ready": ready, "ready_note": ready_note,
            "capabilities": caps}


RUNNERS = [
    _runner("jj-mbp-cdp", JJ_ID),
    _runner("haldimagi-mbp-cdp", HAL_ID),
    _runner("cloud-ec2-1", "117ee3fb-979c-41d3-988f-58420bc422f3", agents=None),
]


def test_resolve_runner_by_name_or_uuid():
    assert resolve_runner(RUNNERS, "jj-mbp-cdp")["id"] == JJ_ID
    assert resolve_runner(RUNNERS, JJ_ID)["name"] == "jj-mbp-cdp"
    assert resolve_runner(RUNNERS, JJ_ID.upper())["name"] == "jj-mbp-cdp"


def test_resolve_runner_unknown_lists_what_is_visible():
    with pytest.raises(DispatchError) as e:
        resolve_runner(RUNNERS, "jj-mbp")  # a prefix is NOT a match — wrong box is the failure
    msg = str(e.value)
    assert "no runner named 'jj-mbp'" in msg
    for r in RUNNERS:
        assert r["name"] in msg


def test_resolve_runner_ambiguous_name_demands_the_id():
    dupes = RUNNERS + [_runner("jj-mbp-cdp", "00000000-0000-0000-0000-000000000001")]
    with pytest.raises(DispatchError) as e:
        resolve_runner(dupes, "jj-mbp-cdp")
    assert "matches 2 runners" in str(e.value) and JJ_ID in str(e.value)


def test_pin_to_online_runner_serving_the_agent_is_clean():
    assert check_runner_pin(_runner("jj-mbp-cdp", JJ_ID), "eva") == ([], [], [])


def test_pin_refuses_a_runner_that_does_not_serve_the_agent():
    problems, _, _ = check_runner_pin(RUNNERS[2], "eva")
    assert problems and "does not serve agent 'eva'" in problems[0]
    problems, _, _ = check_runner_pin(_runner("x", "1", agents=["hal"]), "eva")
    assert "capabilities.agents: hal" in problems[0]


def test_pin_refuses_a_retired_runner():
    problems, not_ready, _ = check_runner_pin(_runner("x", "1", status="retired"), "eva")
    assert any("retired" in p for p in problems)
    assert not_ready == [], "retired is permanent, never 'wait for it'"


@pytest.mark.parametrize("status,paused", [("paused", True), ("stale", False),
                                           ("disconnected", False), ("degraded", False)])
def test_a_runner_that_is_not_online_would_leave_the_turn_queued(status, paused):
    problems, not_ready, _ = check_runner_pin(
        _runner("jj-mbp-cdp", JJ_ID, status=status, paused=paused, paused_note="token cap"),
        "eva")
    assert problems == []
    assert not_ready and "stays QUEUED" in not_ready[0]
    if paused:
        assert "canopy runner unpause jj-mbp-cdp" in not_ready[0]
        assert "token cap" in not_ready[0]


def test_online_but_not_ready_is_a_warning_not_a_hold():
    _, not_ready, warnings = check_runner_pin(
        _runner("x", "1", ready=False, ready_note="emdash CDP unreachable"), "eva")
    assert not_ready == []
    assert warnings and "CDP unreachable" in warnings[0]


def test_payload_carries_runner_id_only_when_pinned():
    unpinned = build_turn_payload("eva", prompt="p", idempotency_key="k")
    assert "runner_id" not in unpinned
    pinned = build_turn_payload("eva", prompt="p", idempotency_key="k", runner_id=JJ_ID)
    assert pinned["runner_id"] == JJ_ID
    # everything else identical — the pin changes WHERE, not what
    assert {k: v for k, v in pinned.items() if k != "runner_id"} == unpinned


def test_a_pinned_key_never_dedupes_onto_an_unpinned_one():
    base = derive_idempotency_key("eva", "Copy allowlist", "2026-09-28")
    assert derive_idempotency_key("eva", "Copy allowlist", "2026-09-28", runner_id="") == base
    on_jj = derive_idempotency_key("eva", "Copy allowlist", "2026-09-28", runner_id=JJ_ID)
    on_hal = derive_idempotency_key("eva", "Copy allowlist", "2026-09-28", runner_id=HAL_ID)
    assert len({base, on_jj, on_hal}) == 3
    assert derive_idempotency_key("eva", "Copy allowlist", "2026-09-28",
                                  runner_id=JJ_ID.upper()) == on_jj


def _pin_transport(calls, runners=RUNNERS):
    inner = _fake_transport(calls, turn_status="queued")

    def transport(method, url, headers, data):
        if method == "GET" and "/harness/runners/" in url:
            calls.append((method, url, None))
            return 200, json.dumps(runners)
        return inner(method, url, headers, data)
    return transport


def _patch(monkeypatch, transport):
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)


def test_cli_runner_by_name_pins_the_turn_by_uuid(monkeypatch):
    calls = []
    _patch(monkeypatch, _pin_transport(calls))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--title", "Copy allowlist",
                                  "--prompt", "read ~/.eva/allowlist.txt", "--runner", "jj-mbp-cdp",
                                  "--json-output"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["runner"]["id"] == JJ_ID and out["runner"]["warnings"] == []
    posts = [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert len(posts) == 1
    body = posts[0][2]
    assert body["runner_id"] == JJ_ID
    # stamp + thread_key kept; key folds the runner in
    assert "canopy:dispatched-prompt" in body["prompt"]
    assert body["origin_ref"]["thread_key"] == body["idempotency_key"] == out["idempotency_key"]
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    assert out["idempotency_key"] == derive_idempotency_key(
        "eva", "Copy allowlist", day, runner_id=JJ_ID)
    assert out["idempotency_key"] != derive_idempotency_key("eva", "Copy allowlist", day)


def test_cli_unknown_runner_fails_before_touching_the_board(monkeypatch):
    calls = []
    _patch(monkeypatch, _pin_transport(calls))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--title", "T",
                                  "--prompt", "p", "--runner", "nope"])
    assert r.exit_code != 0
    assert "no runner named 'nope'" in r.output and "jj-mbp-cdp" in r.output
    assert not [c for c in calls if c[0] == "POST"], "a refused pin must write nothing"


def test_cli_refuses_a_runner_that_does_not_serve_the_agent(monkeypatch):
    calls = []
    _patch(monkeypatch, _pin_transport(calls))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--title", "T",
                                  "--prompt", "p", "--runner", "cloud-ec2-1",
                                  "--queue-if-not-ready"])
    assert r.exit_code != 0
    assert "does not serve agent 'eva'" in r.output
    assert not [c for c in calls if c[0] == "POST"]


def test_cli_paused_runner_is_refused_without_the_escape(monkeypatch):
    calls = []
    paused = [_runner("jj-mbp-cdp", JJ_ID, status="paused", paused=True)]
    _patch(monkeypatch, _pin_transport(calls, paused))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--title", "T",
                                  "--prompt", "p", "--runner", "jj-mbp-cdp"])
    assert r.exit_code != 0
    assert "stays QUEUED" in r.output and "--queue-if-not-ready" in r.output
    assert not [c for c in calls if c[0] == "POST"]


def test_cli_paused_runner_with_the_escape_enqueues_and_says_so(monkeypatch):
    calls = []
    paused = [_runner("jj-mbp-cdp", JJ_ID, status="paused", paused=True)]
    _patch(monkeypatch, _pin_transport(calls, paused))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--title", "T",
                                  "--prompt", "p", "--runner", "jj-mbp-cdp",
                                  "--queue-if-not-ready"])
    assert r.exit_code == 0, r.output
    assert "warning:" in r.output and "stays QUEUED" in r.output
    assert "Runner: jj-mbp-cdp" in r.output
    posts = [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert posts and posts[0][2]["runner_id"] == JJ_ID


def test_cli_queue_if_not_ready_needs_a_runner(monkeypatch):
    _patch(monkeypatch, _pin_transport([]))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--no-task",
                                  "--prompt", "p", "--queue-if-not-ready"])
    assert r.exit_code != 0 and "only applies with --runner" in r.output


def test_cli_unpinned_dispatch_sends_no_runner_and_lists_no_runners(monkeypatch):
    calls = []
    _patch(monkeypatch, _pin_transport(calls))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--no-task",
                                  "--prompt", "p", "--json-output"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["runner"] is None
    assert not [c for c in calls if "/harness/runners/" in c[1]]
    posts = [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert "runner_id" not in posts[0][2]


# --- --mode: choose this turn's mode ------------------------------------------
#
# canopy-web's TurnIn.turn_mode is the top rung of its turn-mode ladder; `auto` is
# owner/admin-only server-side. The CLI passes it through, folds it into the key, and
# reports what the server RECORDED rather than what was asked.

def _mode_transport(calls, *, record=True, runners=RUNNERS):
    def transport(method, url, headers, data):
        body = json.loads(data) if data else None
        calls.append((method, url, body))
        if method == "GET" and "/harness/runners/" in url:
            return 200, json.dumps(runners)
        if "/harness/turns/" in url and method == "POST":
            out = {"id": "turn-9", "status": "queued", "result_note": ""}
            if record:
                out["requested_turn_mode"] = (body or {}).get("turn_mode") or ""
            return 201, json.dumps(out)
        if "/tasks" in url:
            return 200, json.dumps({"synced": 1})
        return 200, json.dumps([])
    return transport


def test_payload_carries_turn_mode_only_when_requested():
    plain = build_turn_payload("hal", prompt="p", idempotency_key="k")
    assert "turn_mode" not in plain
    auto = build_turn_payload("hal", prompt="p", idempotency_key="k", turn_mode="auto")
    assert auto["turn_mode"] == "auto"
    assert {k: v for k, v in auto.items() if k != "turn_mode"} == plain
    with pytest.raises(DispatchError):
        build_turn_payload("hal", prompt="p", idempotency_key="k", turn_mode="yolo")


def test_a_moded_key_never_dedupes_onto_an_unmoded_one():
    base = derive_idempotency_key("hal", "Ping", "2026-10-04")
    assert derive_idempotency_key("hal", "Ping", "2026-10-04", mode="") == base
    auto = derive_idempotency_key("hal", "Ping", "2026-10-04", mode="auto")
    manual = derive_idempotency_key("hal", "Ping", "2026-10-04", mode="manual")
    assert len({base, auto, manual}) == 3


def test_cli_mode_and_runner_together(monkeypatch):
    calls = []
    _patch(monkeypatch, _mode_transport(calls))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task",
                                  "--prompt", "ping", "--mode", "manual",
                                  "--runner", "haldimagi-mbp-cdp"])
    assert r.exit_code == 0, r.output
    [post] = [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert post[2]["turn_mode"] == "manual" and post[2]["runner_id"] == HAL_ID
    assert "Runner: haldimagi-mbp-cdp" in r.output
    assert "Mode:   manual" in r.output
    assert "Turn:   turn-9" in r.output


def test_cli_mode_json_reports_what_the_server_recorded(monkeypatch):
    calls = []
    _patch(monkeypatch, _mode_transport(calls))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task",
                                  "--prompt", "ping", "--mode", "auto", "--json-output"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["mode"] == {"requested": "auto", "recorded": "auto"}
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    assert out["idempotency_key"] == derive_idempotency_key("hal", "ping", day, mode="auto")


def test_cli_mode_warns_when_an_older_server_drops_it(monkeypatch):
    calls = []
    _patch(monkeypatch, _mode_transport(calls, record=False))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task",
                                  "--prompt", "ping", "--mode", "auto"])
    assert r.exit_code == 0, r.output
    assert "did not report recording it" in r.output


def test_cli_unmoded_dispatch_says_the_rules_decide(monkeypatch):
    calls = []
    _patch(monkeypatch, _mode_transport(calls))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task",
                                  "--prompt", "ping"])
    assert r.exit_code == 0, r.output
    [post] = [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]
    assert "turn_mode" not in post[2]
    assert "not requested" in r.output and "not pinned" in r.output


def test_cli_rejects_an_unknown_mode(monkeypatch):
    calls = []
    _patch(monkeypatch, _mode_transport(calls))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task",
                                  "--prompt", "ping", "--mode", "yolo"])
    assert r.exit_code != 0
    assert not [c for c in calls if c[0] == "POST"]


def test_cli_surfaces_a_403_for_auto(monkeypatch):
    def transport(method, url, headers, data):
        if "/harness/turns/" in url and method == "POST":
            return 403, json.dumps({"detail": "turn_mode=auto is for hal's owner or admins"})
        return 200, json.dumps([])
    _patch(monkeypatch, transport)
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task",
                                  "--prompt", "ping", "--mode", "auto"])
    assert r.exit_code != 0
    assert "owner or admins" in r.output or "403" in r.output
