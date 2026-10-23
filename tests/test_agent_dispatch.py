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


@pytest.fixture(autouse=True)
def _no_local_use_note(request, monkeypatch):
    """The local-use note is its own concern (tests at the end of this file); every other
    test here pins dispatch's stdout, which the note must never touch."""
    if "local_use" not in request.node.name:
        monkeypatch.setattr("orchestrator.agent_dispatch.local_use_note_for", lambda slug, call: "")


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


# An untitled dispatch is keyed on its WHOLE prompt (2026-10-04: a corrected re-dispatch
# to ace whose brief opened like the first returned the earlier finished turn).
_BRIEF = "Fix the failing labs export test. " * 4          # > 80 chars of shared head


def test_untitled_dispatches_with_different_prompts_are_different_work():
    first = derive_idempotency_key("ace", "", "2026-10-04", prompt=_BRIEF + "Use the v1 API.")
    fixed = derive_idempotency_key("ace", "", "2026-10-04", prompt=_BRIEF + "Use the v2 API.")
    assert first != fixed


def test_an_identical_untitled_retry_still_dedupes():
    a = derive_idempotency_key("ace", "", "2026-10-04", prompt=_BRIEF, mode="auto")
    b = derive_idempotency_key("ace", "", "2026-10-04", prompt=_BRIEF, mode="auto")
    assert a == b
    assert derive_idempotency_key("ace", "", "2026-10-04", prompt=_BRIEF, mode="manual") != a


def test_a_titled_key_ignores_the_prompt_and_is_unchanged():
    base = derive_idempotency_key("hal", "Fix the cursor", "2026-07-28")
    assert derive_idempotency_key("hal", "Fix the cursor", "2026-07-28", prompt="anything") == base


def test_cli_untitled_redispatch_with_a_corrected_prompt_gets_a_new_key(monkeypatch):
    calls = []
    _patch(monkeypatch, _mode_transport(calls))
    keys = []
    for brief in (_BRIEF + "v1", _BRIEF + "v2", _BRIEF + "v2"):
        r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task",
                                      "--prompt", brief, "--json-output"])
        assert r.exit_code == 0, r.output
        keys.append(json.loads(r.output)["idempotency_key"])
    assert keys[0] != keys[1] and keys[1] == keys[2]


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


def test_pin_to_a_cloud_runner_with_no_agent_list_is_allowed():
    # cloud-ec2-1 really reports kind=cloud and capabilities without "agents";
    # it runs ada/eva turns by routing (2026-10-05: an ada pin there was refused).
    cloud = {**RUNNERS[2], "kind": "cloud"}
    assert check_runner_pin(cloud, "ada") == ([], [], [])
    declared = {**cloud, "capabilities": {"agents": ["hal"]}}
    problems, _, _ = check_runner_pin(declared, "ada")
    assert problems, "a cloud box that DOES declare agents is held to its list"


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


def test_payload_carries_acts_as_only_when_asked():
    plain = build_turn_payload("hal", prompt="p", idempotency_key="k")
    assert "acts_as" not in plain
    capped = build_turn_payload("hal", prompt="p", idempotency_key="k", acts_as="editor")
    assert capped["acts_as"] == "editor"
    assert {k: v for k, v in capped.items() if k != "acts_as"} == plain
    with pytest.raises(DispatchError):
        build_turn_payload("hal", prompt="p", idempotency_key="k", acts_as="admin")


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
    assert out["idempotency_key"] == derive_idempotency_key("hal", "", day, mode="auto",
                                                            prompt="ping")


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


# --- a PERSON dispatching from their own Claude Code ------------------------
# 2026-10-19: a teammate's `canopy agent dispatch --slug ace` from outside any agent repo was
# stamped "Dispatched by another canopy agent" — false, and the receiving session had to call
# who_is_asking to find out a human sent it.

def test_a_person_is_named_as_a_person_not_an_agent():
    p = build_turn_payload("ace", prompt="read-only test", idempotency_key="k5",
                           sender="", person="someone@dimagi.com")
    assert "Sent by someone@dimagi.com" in p["prompt"]
    assert "not by a canopy agent" in p["prompt"]
    assert "NOT approval for any outbound action" in p["prompt"]
    assert p["origin_ref"]["dispatched_by_person"] == "someone@dimagi.com"
    assert "dispatched_by" not in p["origin_ref"]
    assert dispatched_by(p["prompt"]) == ""   # the agent-slug marker stays agents-only


def test_an_agent_sender_outranks_the_person_whose_token_it_uses():
    p = build_turn_payload("hal", prompt="x", idempotency_key="k6",
                           sender="ada", person="jj@dimagi.com")
    assert "ada, a canopy agent" in p["prompt"]
    assert "jj@dimagi.com" not in p["prompt"]
    assert "dispatched_by_person" not in p["origin_ref"]


def test_cli_dispatch_from_outside_an_agent_repo_names_the_person(monkeypatch):
    """The 2026-10-19 path end to end: no agent repo underfoot, so the CLI reads /api/me/
    and the receiving agent is told a person sent it, by name."""
    calls = []
    inner = _fake_transport(calls)

    def transport(method, url, headers, data):
        if url.endswith("/api/me/"):
            calls.append((method, url, None))
            return 200, json.dumps({"email": "someone@dimagi.com"})
        return inner(method, url, headers, data)

    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    monkeypatch.setattr("orchestrator.agent_dispatch.local_agent_slug", lambda start=None: "")

    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "ace", "--no-task",
                                  "--prompt", "read-only test", "--json-output"])
    assert r.exit_code == 0, r.output
    post = [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]][0][2]
    assert "Sent by someone@dimagi.com" in post["prompt"]
    assert post["origin_ref"]["dispatched_by_person"] == "someone@dimagi.com"


# --- acting on behalf of the person in the turn (canopy-web turn_asker) -------------

def _behalf_setup(monkeypatch, tmp_path, *, verified=True, kind="user"):
    seen = []

    def transport(method, url, headers, data):
        seen.append((method, url, dict(headers)))
        if "/harness/turns/" in url and method == "POST":
            return 201, json.dumps({"id": "t-9", "status": "queued", "origin_ref": {
                "on_behalf_of": {"email": "jj@dimagi.com", "decided": ["pin eva's turn to cloud-ec2-2"]}}})
        return 200, json.dumps([])

    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    monkeypatch.setattr("orchestrator.provenance.CALLER_ROOT", tmp_path)
    monkeypatch.setenv("CANOPY_TURN_ID", "11111111-2222-3333-4444-555555555555")
    (tmp_path / "11111111-2222-3333-4444-555555555555.json").write_text(json.dumps({
        "verified": verified, "who": {"kind": kind, "user": {"email": "jj@dimagi.com"}}}))
    return seen


def test_on_behalf_of_caller_names_the_turns_verified_asker_and_reports_what_was_recorded(
        monkeypatch, tmp_path):
    seen = _behalf_setup(monkeypatch, tmp_path)
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--no-task", "--prompt", "go",
                                  "--on-behalf-of", "caller", "--json-output"])
    assert r.exit_code == 0, r.output
    post = [h for m, u, h in seen if m == "POST" and "/harness/turns/" in u][0]
    assert post["X-Canopy-On-Behalf-Of"] == "jj@dimagi.com"
    assert json.loads(r.output)["on_behalf_of"]["decided"] == ["pin eva's turn to cloud-ec2-2"]


def test_without_the_flag_the_agent_acts_for_itself(monkeypatch, tmp_path):
    seen = _behalf_setup(monkeypatch, tmp_path)
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--no-task", "--prompt", "go",
                                  "--json-output"])
    assert r.exit_code == 0, r.output
    assert all("X-Canopy-On-Behalf-Of" not in h for _, _, h in seen)
    assert json.loads(r.output)["on_behalf_of"] is None


@pytest.mark.parametrize("verified,kind", [(False, "user"), (True, "agent")])
def test_caller_with_no_verified_person_is_refused_before_anything_is_sent(
        monkeypatch, tmp_path, verified, kind):
    seen = _behalf_setup(monkeypatch, tmp_path, verified=verified, kind=kind)
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "eva", "--title", "x", "--prompt", "go",
                                  "--on-behalf-of", "caller"])
    assert r.exit_code != 0 and "no verified person" in r.output
    assert not [s for s in seen if s[0] == "POST"]  # no board card, no turn


# --- dispatch is for unattended work; local use is how a person works with an agent -------
# canopy-web#1955: two members asked ACE 17 questions as one-shot dispatches because nothing
# told them `canopy agent use` existed.

from orchestrator import agent_dispatch as AD  # noqa: E402

_NOTE_ENV = ("CANOPY_TURN_ID", "CANOPY_AGENT", "CANOPY_AGENT_SLUG")


def _as_person(monkeypatch, tmp_path):
    for var in _NOTE_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no agent repo underfoot
    monkeypatch.setattr("orchestrator.canopy_web._dispatched_agent_slug", lambda: "")


def _web(licences=(), via=None, fail=()):
    calls = []

    def call(method, path, body=None, **kw):
        calls.append(path)
        if any(f in path for f in fail):
            raise RuntimeError("boom")
        if path == "/api/me/":
            return {"id": 7, "email": "member@dimagi.com"}
        if path.endswith("/licenses"):
            return list(licences)
        if path.endswith("/credentials/access"):
            return {"via": via}
        raise AssertionError(path)
    call.calls = calls
    return call


def test_local_use_note_names_canopy_agent_use_and_unattended():
    note = AD.local_use_note("ace")
    assert "canopy agent use ace" in note and "unattended" in note and "\n" not in note


def test_local_use_note_for_a_person_without_a_licence(monkeypatch, tmp_path):
    _as_person(monkeypatch, tmp_path)
    assert AD.local_use_note_for("ace", _web()) == AD.local_use_note("ace")


def test_local_use_note_silent_with_an_active_licence(monkeypatch, tmp_path):
    _as_person(monkeypatch, tmp_path)
    lic = [{"user_id": 7, "email": "member@dimagi.com", "status": "active"}]
    assert AD.local_use_note_for("ace", _web(licences=lic)) == ""
    # a requested (not yet approved) or revoked licence is not local use
    for st in ("requested", "revoked"):
        assert AD.local_use_note_for("ace", _web(licences=[{**lic[0], "status": st}]))


def test_local_use_note_silent_for_an_admin(monkeypatch, tmp_path):
    _as_person(monkeypatch, tmp_path)
    assert AD.local_use_note_for("ace", _web(via="admin")) == ""


def test_local_use_note_never_raises_when_canopy_web_fails(monkeypatch, tmp_path):
    _as_person(monkeypatch, tmp_path)
    web = _web(fail=("/api/me/", "/licenses", "/access"))
    assert AD.local_use_note_for("ace", web) == AD.local_use_note("ace")


@pytest.mark.parametrize("var", ["CANOPY_TURN_ID", "CANOPY_AGENT"])
def test_local_use_note_silent_inside_an_agents_turn(monkeypatch, tmp_path, var):
    """Agents dispatching to each other is what dispatch is for: no note, no extra calls."""
    _as_person(monkeypatch, tmp_path)
    monkeypatch.setenv(var, "hal" if var == "CANOPY_AGENT" else "turn-1")
    web = _web()
    assert AD.local_use_note_for("ace", web) == "" and web.calls == []


def test_local_use_note_silent_in_an_agent_repo(monkeypatch, tmp_path):
    _as_person(monkeypatch, tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(json.dumps({"slug": "hal"}))
    assert AD.local_use_note_for("ace", _web()) == ""


def test_cli_dispatch_prints_the_local_use_note_on_stderr_only(monkeypatch):
    calls = []
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", _fake_transport(calls))
    monkeypatch.setattr("orchestrator.agent_dispatch.local_use_note_for",
                        lambda slug, call: AD.local_use_note(slug))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "ace", "--no-task",
                                  "--prompt", "q", "--json-output"])
    assert r.exit_code == 0, r.output
    json.loads(r.stdout)  # stdout stays pure JSON
    assert "canopy agent use ace" in r.stderr and "unattended" in r.stderr


def test_cli_dispatch_still_dispatches_when_the_note_check_blows_up(monkeypatch):
    calls = []
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", _fake_transport(calls))

    def boom(slug, call):
        raise RuntimeError("x")
    monkeypatch.setattr("orchestrator.agent_dispatch.local_use_note_for", boom)
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "ace", "--no-task", "--prompt", "q"])
    assert r.exit_code == 0, r.output
    assert [c for c in calls if c[0] == "POST" and "/harness/turns/" in c[1]]


def test_dispatch_help_points_people_to_local_use_and_reply():
    r = CliRunner().invoke(main, ["agent", "dispatch", "--help"])
    text = " ".join(r.output.split())
    assert "UNATTENDED" in text and "canopy agent use <slug>" in text
    assert "canopy agent reply" in text


# --- canopy agent reply: a follow-up reaches the session it follows ------------------------

SID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"


@pytest.mark.parametrize("ref", [SID, SID.upper(), f"https://canopy.dimagi.com/w/connect/chat/{SID}"])
def test_session_send_path_takes_an_id_or_a_link(ref):
    assert AD.session_send_path(ref) == f"/api/canopy-sessions/{SID}/send"


@pytest.mark.parametrize("ref", ["", "ace-main", "turn-123"])
def test_session_send_path_refuses_anything_else(ref):
    with pytest.raises(DispatchError):
        AD.session_send_path(ref)


def test_cli_reply_sends_into_the_existing_session(monkeypatch):
    calls = []

    def transport(method, url, headers, data):
        calls.append((method, url, json.loads(data) if data else None))
        return 200, json.dumps({"turn_id": "turn-9", "message": {"id": "m1"}})
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    r = CliRunner().invoke(main, ["agent", "reply", f"https://x/w/connect/chat/{SID}",
                                  "--prompt", "and the rest?", "--json-output"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {"session": SID, "turn_id": "turn-9", "message_id": "m1"}
    assert calls == [("POST", f"https://x/api/canopy-sessions/{SID}/send", {"text": "and the rest?"})]
    # never a new turn: a reply must not touch the harness turn queue or the board
    assert not [c for c in calls if "/harness/turns/" in c[1] or "/tasks" in c[1]]


def test_cli_reply_needs_a_prompt_and_a_real_session(monkeypatch):
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    r = CliRunner().invoke(main, ["agent", "reply", SID])
    assert r.exit_code != 0 and "--prompt" in r.output
    r = CliRunner().invoke(main, ["agent", "reply", "not-a-session", "--prompt", "x"])
    assert r.exit_code != 0 and "not a canopy session id" in r.output


# --- reply by TURN id: dispatch prints only a turn id (canopy-web#1974) ---------------------

TID = "7f3523ba-0000-4000-8000-000000000001"


def _reply_world(monkeypatch, turn=None, turn_status=200):
    calls = []

    def transport(method, url, headers, data):
        calls.append((method, url, json.loads(data) if data else None))
        if method == "GET" and "/api/harness/turns/" in url:
            if turn_status != 200:
                return turn_status, json.dumps({"detail": "turn not found"})
            return 200, json.dumps(turn)
        return 200, json.dumps({"turn_id": "turn-9", "message": {"id": "m1"}})
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    return calls


def test_cli_reply_follows_a_turn_id_to_its_session(monkeypatch):
    calls = _reply_world(monkeypatch, {"id": TID, "status": "running", "agent_slug": "ace",
                                       "canopy_session_id": SID})
    r = CliRunner().invoke(main, ["agent", "reply", TID, "--prompt", "more", "--json-output"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["session"] == SID
    assert calls[-1] == ("POST", f"https://x/api/canopy-sessions/{SID}/send", {"text": "more"})


def test_cli_reply_to_a_queued_turn_says_there_is_no_session_yet(monkeypatch):
    calls = _reply_world(monkeypatch, {"id": TID, "status": "queued", "agent_slug": "ace",
                                       "canopy_session_id": None})
    r = CliRunner().invoke(main, ["agent", "reply", TID, "--prompt", "more"])
    assert r.exit_code != 0
    text = " ".join(r.output.split())
    assert "no session yet" in text and "queued" in text and "canopy agent turns --slug ace" in text
    assert not [c for c in calls if c[0] == "POST"], "nothing may be sent anywhere"


def test_cli_reply_bare_session_id_still_works_when_it_is_no_turn(monkeypatch):
    calls = _reply_world(monkeypatch, turn_status=404)
    r = CliRunner().invoke(main, ["agent", "reply", SID, "--prompt", "more"])
    assert r.exit_code == 0, r.output
    assert calls[-1][1] == f"https://x/api/canopy-sessions/{SID}/send"


def test_cli_reply_on_an_older_canopy_web_names_the_gap(monkeypatch):
    calls = _reply_world(monkeypatch, {"id": TID, "status": "done", "agent_slug": "ace"})
    r = CliRunner().invoke(main, ["agent", "reply", TID, "--prompt", "more"])
    assert r.exit_code != 0
    assert "does not say which session" in " ".join(r.output.split())
    assert not [c for c in calls if c[0] == "POST"]


def test_cli_reply_with_a_link_never_probes_the_turn_api(monkeypatch):
    calls = _reply_world(monkeypatch, turn_status=500)
    r = CliRunner().invoke(main, ["agent", "reply", f"https://x/w/connect/chat/{SID}",
                                  "--prompt", "more"])
    assert r.exit_code == 0, r.output
    assert not [c for c in calls if "/harness/turns/" in c[1]]


def test_cli_dispatch_tells_how_to_follow_up_without_polling(monkeypatch):
    calls = []
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport",
                        _fake_transport(calls, turn_status="queued"))
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task", "--prompt", "p"])
    assert r.exit_code == 0, r.output
    assert "canopy agent reply turn-123" in r.output
    # One enqueue, and no re-reads of the turn waiting for its session.
    assert not [c for c in calls if c[0] == "GET" and "/harness/turns/" in c[1]]


def test_cli_dispatch_prints_the_session_link_when_the_server_has_one(monkeypatch):
    def transport(method, url, headers, data):
        if "/harness/turns/" in url and method == "POST":
            return 200, json.dumps({"id": "turn-123", "status": "running",
                                    "canopy_session_id": SID,
                                    "canopy_session_url": f"https://x/w/connect/chat/{SID}"})
        return 200, json.dumps([])
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task", "--prompt", "p"])
    assert r.exit_code == 0, r.output
    assert f"Session: https://x/w/connect/chat/{SID}" in r.output
    r = CliRunner().invoke(main, ["agent", "dispatch", "--slug", "hal", "--no-task", "--prompt", "p",
                                  "--json-output"])
    out = json.loads(r.output[:r.output.rindex("}") + 1])  # a stderr note may follow the JSON
    assert out["turn"]["canopy_session_id"] == SID


def test_cli_turns_shows_each_turns_session(monkeypatch):
    def transport(method, url, headers, data):
        return 200, json.dumps([{"id": "t1", "agent_slug": "hal", "status": "done",
                                 "created_at": "2026-07-28T13:00:00Z", "prompt": "x",
                                 "canopy_session_id": SID,
                                 "canopy_session_url": f"https://x/w/connect/chat/{SID}"},
                                {"id": "t2", "agent_slug": "hal", "status": "queued",
                                 "created_at": "2026-07-28T13:01:00Z", "prompt": "y",
                                 "canopy_session_id": None, "canopy_session_url": None}])
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://x")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    r = CliRunner().invoke(main, ["agent", "turns", "--slug", "hal"])
    assert r.exit_code == 0, r.output
    assert f"session: https://x/w/connect/chat/{SID}" in r.output
    assert r.output.count("session: ") == 1
    r = CliRunner().invoke(main, ["agent", "turns", "--slug", "hal", "--json-output"])
    rows = json.loads(r.output)
    assert rows[0]["canopy_session_id"] == SID and rows[1]["canopy_session_id"] is None
