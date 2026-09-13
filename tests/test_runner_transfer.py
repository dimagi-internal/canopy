"""`canopy runner transfer` — move a live session onto another box.

Written against a real failure: on 2026-09-12 a session started by accident on
`cloud-ec2-1` had to be moved to `jj-mbp-cdp`, and there was no command for it.
Doing it by hand with `place` + `send` moved execution correctly and destroyed
the session's entire pre-transfer history. These tests pin the parts of the
client that stop a repeat: refusing a target that could never claim the turn,
refusing the no-op, surfacing the mid-turn conflict with its fix, and never
reporting the move as done.
"""
import json

import pytest
from click.testing import CliRunner

from orchestrator.cli import main

CLOUD = "117ee3fb-979c-41d3-988f-58420bc422f3"
LAPTOP = "cb9d5262-52da-4d4d-a294-c59c47cd3e15"
SID = "169212e2-e877-4b41-bd18-c4184d0f32e8"

RUNNERS = [
    {"id": CLOUD, "name": "cloud-ec2-1", "status": "online", "can_manage": True,
     "capabilities": {"projects": ["canopy-web"], "sessions": True}},
    {"id": LAPTOP, "name": "jj-mbp-cdp", "status": "online", "can_manage": True,
     "capabilities": {"projects": ["canopy-web"], "sessions": True}},
    {"id": "3f000000-0000-4000-8000-000000000000", "name": "build-box",
     "status": "online", "can_manage": True,
     "capabilities": {"projects": ["canopy-web"]}},  # sessions: absent
]

SESSIONS = [
    {"id": SID, "title": "widget design", "project": "canopy-web",
     "runner_name": "cloud-ec2-1", "status": "active"},
    {"id": "aaaaaaaa-0000-4000-8000-000000000000", "title": "teaching",
     "project": "canopy-web", "runner_name": "jj-mbp-cdp", "status": "active"},
]

TRANSFER_OK = {
    "session_id": SID, "runner": "jj-mbp-cdp", "transferred_from": "cloud-ec2-1",
    "index_offset": 20864, "turn_id": "f091a1c7-6156-4fb8-a398-5325b949f621",
}


@pytest.fixture
def fake_http(monkeypatch):
    calls = []
    responses = {
        ("GET", "harness/runners/"): (200, json.dumps(RUNNERS)),
        ("GET", "canopy-sessions/"): (200, json.dumps(SESSIONS)),
        ("POST", f"canopy-sessions/{SID}/transfer"): (200, json.dumps(TRANSFER_OK)),
    }

    def transport(method, url, headers, body):
        path = url.split("/api/")[1]
        calls.append((method, path, json.loads(body) if body else None))
        return responses.get((method, path), (200, "{}"))

    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    return calls, responses


def _invoke(*args):
    return CliRunner().invoke(main, ["runner", "transfer", *args])




def test_transfer_happy_path(fake_http):
    calls, _ = fake_http
    r = _invoke(SID, "--to", "jj-mbp-cdp", "--brief", "BRANCH: docs/widget-design")
    assert r.exit_code == 0, r.output
    post = next(c for c in calls if c[0] == "POST")
    assert post[1] == f"canopy-sessions/{SID}/transfer"
    assert post[2] == {"runner": LAPTOP, "brief": "BRANCH: docs/widget-design"}
    # LAUNCHED, never "done": the move being accepted is not the agent picking up.
    assert "LAUNCHED (unverified)" in r.output
    assert "cloud-ec2-1 -> jj-mbp-cdp" in r.output
    # The epoch base is surfaced as the proof history was carried.
    assert "20864" in r.output
    assert "carried across, not dropped" in r.output


def test_transfer_resolves_a_session_by_title_substring(fake_http):
    calls, _ = fake_http
    r = _invoke("widget", "--to", "jj-mbp-cdp", "--brief", "x")
    assert r.exit_code == 0, r.output
    assert any(c[1] == f"canopy-sessions/{SID}/transfer" for c in calls)


def test_an_ambiguous_session_lists_candidates_instead_of_picking_one(fake_http):
    """Picking wrong here moves the wrong live conversation onto another box."""
    _calls, responses = fake_http
    responses[("GET", "canopy-sessions/")] = (200, json.dumps([
        {"id": SID, "title": "widget design a", "runner_name": "cloud-ec2-1"},
        {"id": "bbbbbbbb-0000-4000-8000-000000000000", "title": "widget design b",
         "runner_name": "cloud-ec2-1"},
    ]))
    r = _invoke("widget", "--to", "jj-mbp-cdp")
    assert r.exit_code != 0
    assert "matches 2 sessions" in r.output
    assert SID in r.output


def test_transfer_refuses_a_target_that_is_not_session_capable(fake_http):
    """A pin to a session-incapable box is never claimable, so the turn would sit
    QUEUED forever with nothing saying why."""
    calls, _ = fake_http
    r = _invoke(SID, "--to", "build-box")
    assert r.exit_code != 0
    assert "not session-capable" in r.output
    assert not any(c[0] == "POST" for c in calls), "must not POST a doomed transfer"


def test_transfer_refuses_a_move_to_where_the_session_already_is(fake_http):
    calls, _ = fake_http
    r = _invoke(SID, "--to", "cloud-ec2-1")
    assert r.exit_code != 0
    assert "already on cloud-ec2-1" in r.output
    assert not any(c[0] == "POST" for c in calls)


def test_a_mid_turn_409_names_the_fix(fake_http):
    """The server refuses while the source box is mid-thought. The client's job is
    to say what to do about it rather than echo a status code."""
    _calls, responses = fake_http
    responses[("POST", f"canopy-sessions/{SID}/transfer")] = (
        409, json.dumps({"detail": "a turn is still executing — stop the session first"}))
    r = _invoke(SID, "--to", "jj-mbp-cdp")
    assert r.exit_code != 0
    assert "--stop" in r.output
    assert "still executing" in r.output


def test_stop_flag_cancels_the_source_turn_before_transferring(fake_http):
    calls, _ = fake_http
    r = _invoke(SID, "--to", "jj-mbp-cdp", "--stop", "--brief", "x")
    assert r.exit_code == 0, r.output
    posts = [c[1] for c in calls if c[0] == "POST"]
    assert posts == [f"canopy-sessions/{SID}/stop",
                     f"canopy-sessions/{SID}/transfer"], "stop must precede transfer"


def test_a_brief_file_is_read_from_disk(fake_http, tmp_path):
    calls, _ = fake_http
    f = tmp_path / "handoff.md"
    f.write_text("BRANCH: docs/x\nPR: #745 open\n")
    r = _invoke(SID, "--to", "jj-mbp-cdp", "--brief-file", str(f))
    assert r.exit_code == 0, r.output
    post = next(c for c in calls if c[0] == "POST")
    assert "PR: #745 open" in post[2]["brief"]


def test_no_brief_sends_the_default_and_says_so(fake_http):
    """A transfer with no brief still hands over SOMETHING — a cold session in a
    fresh worktree that is told nothing will invent a plan. And the operator is
    told the brief was the default, plus where a real one comes from."""
    calls, _ = fake_http
    r = _invoke(SID, "--to", "jj-mbp-cdp")
    assert r.exit_code == 0, r.output
    post = next(c for c in calls if c[0] == "POST")
    assert post[2]["brief"].strip(), "never POST an empty brief"
    assert "ask rather than guess" in post[2]["brief"]
    assert "DEFAULT" in r.output
    assert "user-switch" in r.output, "point at the tool that composes a real one"


def test_json_output_is_the_servers_answer(fake_http):
    r = _invoke(SID, "--to", "jj-mbp-cdp", "--brief", "x", "--json-output")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["index_offset"] == 20864


def test_transfer_refuses_a_runner_someone_else_owns(fake_http):
    """Ownership, not permissions — and usually an IDENTITY mix-up. Same refusal
    pause/unpause use, reused rather than re-spelled."""
    _calls, responses = fake_http
    others = [dict(x) for x in RUNNERS]
    others[1]["can_manage"] = False
    others[1]["paired_by_email"] = "someone@dimagi.com"
    responses[("GET", "harness/runners/")] = (200, json.dumps(others))
    r = _invoke(SID, "--to", "jj-mbp-cdp")
    assert r.exit_code != 0
    assert "someone@dimagi.com" in r.output
