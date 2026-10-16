"""`canopy slack ingest` — the deterministic half of the slack-channel-ingest skill,
against a mocked canopy-web (canopy-web#1533)."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from orchestrator import canopy_web, slack_ingest_cli

ID = "3f1c0000-0000-0000-0000-000000000001"


def _ingest(status="pending", **kw):
    return {"id": ID, "agent": "hal", "workspace": "dimagi", "channel": "C1", "channel_name": "canopy-support",
            "purpose": "digest", "follow": False, "status": status, "messages": 0, "error": "", **kw}


class Server:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        resp = self.routes[(method, path)]
        if isinstance(resp, list) and resp and isinstance(resp[0], dict) and "status" in resp[0] \
                and method == "GET" and path.endswith(ID):
            return resp.pop(0) if len(resp) > 1 else resp[0]
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.setattr(slack_ingest_cli.time, "sleep", lambda s: None)

    def _install(server):
        monkeypatch.setattr(slack_ingest_cli.canopy_web, "call", server)
        return server
    return _install


def _run(*args):
    return CliRunner().invoke(slack_ingest_cli.slack_group, ["ingest", *args])


def test_request_posts_the_body_and_says_nothing_is_read_yet(serve):
    srv = serve(Server({("POST", "/api/slack-ingests/"): _ingest()}))
    r = _run("request", "--agent", "hal", "--channel", " C1 ", "--purpose", " digest ", "--follow")
    assert r.exit_code == 0, r.output
    assert srv.calls == [("POST", "/api/slack-ingests/",
                          {"agent": "hal", "channel": "C1", "purpose": "digest", "follow": True})]
    assert "nothing is read until someone there approves" in r.output


def test_request_on_an_old_canopy_web_says_so(serve):
    serve(Server({("POST", "/api/slack-ingests/"):
                  canopy_web.CanopyError("POST /api/slack-ingests/ -> 404: Not Found")}))
    r = _run("request", "--agent", "hal", "--channel", "C1", "--purpose", "x")
    assert r.exit_code == 1 and "not deployed" in r.output


def test_a_refusal_is_passed_through(serve):
    serve(Server({("POST", "/api/slack-ingests/"):
                  canopy_web.CanopyError("POST /api/slack-ingests/ -> 409: channel ingest is off")}))
    r = _run("request", "--agent", "hal", "--channel", "C1", "--purpose", "x")
    assert r.exit_code == 1 and "channel ingest is off" in r.output


@pytest.mark.parametrize("final, code", [("ready", 0), ("declined", 4), ("revoked", 4), ("failed", 5)])
def test_wait_polls_until_a_person_answers(serve, final, code):
    srv = serve(Server({("GET", f"/api/slack-ingests/{ID}"):
                        [_ingest("pending"), _ingest("reading"), _ingest(final)]}))
    r = _run("wait", ID, "--interval", "1")
    assert r.exit_code == code, r.output
    assert len(srv.calls) == 3


def test_wait_times_out_as_still_open_not_as_a_refusal(serve, monkeypatch):
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(slack_ingest_cli.time, "monotonic", lambda: next(clock))
    serve(Server({("GET", f"/api/slack-ingests/{ID}"): [_ingest("pending")]}))
    r = _run("wait", ID, "--timeout", "250", "--interval", "1")
    assert r.exit_code == slack_ingest_cli.EXIT_WAITING, r.output
    assert "pending" in r.output


T = f"/api/slack-ingests/{ID}/transcript"


def _t(md="# Slack #canopy-support\n", **kw):
    return {"id": ID, "status": "ready", "messages": 2, "markdown": md, "total": 2, "after": 0, "mark": 0,
            "first_ts": "", "last_ts": "", **kw}


def test_the_first_read_is_the_whole_channel_and_sets_the_mark(serve, tmp_path):
    srv = serve(Server({("GET", f"/api/slack-ingests/{ID}"): _ingest("ready", read_mark=0),
                        ("GET", T): _t(mark=17),
                        ("POST", f"/api/slack-ingests/{ID}/read-mark"): _ingest("ready", read_mark=17)}))
    out = tmp_path / "c.md"
    r = _run("transcript", ID, "--out", str(out))
    assert r.exit_code == 0, r.output
    assert out.read_text() == "# Slack #canopy-support\n"
    assert "wrote 2 message(s) (ready), the whole transcript" in r.output and "read mark -> 17" in r.output
    assert srv.calls[1][:2] == ("GET", T)                    # no `after` on a first read
    assert srv.calls[-1] == ("POST", f"/api/slack-ingests/{ID}/read-mark", {"mark": 17})


def test_by_default_a_read_catches_up_from_the_last_one(serve, tmp_path):
    srv = serve(Server({("GET", f"/api/slack-ingests/{ID}"): _ingest("ready", read_mark=17),
                        ("GET", f"{T}?after=17"): _t("# new only\n", messages=3, total=9, after=17, mark=23,
                                                     first_ts="1700000600.000000", last_ts="1700000650.000000"),
                        ("POST", f"/api/slack-ingests/{ID}/read-mark"): _ingest("ready", read_mark=23)}))
    out = tmp_path / "c.md"
    r = _run("transcript", ID, "--out", str(out))
    assert r.exit_code == 0, r.output
    assert out.read_text() == "# new only\n"
    assert "wrote 3 new message(s) since the last read, 2023-11-14 22:23 → 2023-11-14 22:24 UTC" in r.output
    assert "9 in the whole transcript" in r.output
    assert srv.calls[-1] == ("POST", f"/api/slack-ingests/{ID}/read-mark", {"mark": 23})


def test_nothing_new_leaves_the_mark_alone(serve, tmp_path):
    srv = serve(Server({("GET", f"/api/slack-ingests/{ID}"): _ingest("ready", read_mark=23),
                        ("GET", f"{T}?after=23"): _t(messages=0, total=9, after=23, mark=23)}))
    r = _run("transcript", ID, "--out", str(tmp_path / "c.md"))
    assert r.exit_code == 0, r.output
    assert "nothing new since the last read (9 in the whole transcript)" in r.output
    assert not [c for c in srv.calls if c[0] == "POST"]


def test_all_rereads_everything_and_no_mark_peeks(serve, tmp_path):
    srv = serve(Server({("GET", T): _t(total=2, mark=23),
                        ("POST", f"/api/slack-ingests/{ID}/read-mark"): _ingest("ready", read_mark=23)}))
    r = _run("transcript", ID, "--all", "--no-mark", "--out", str(tmp_path / "c.md"))
    assert r.exit_code == 0, r.output
    assert srv.calls == [("GET", T, None)]                    # no status read, no mark


def test_the_mark_does_not_move_when_the_write_fails(serve, tmp_path):
    srv = serve(Server({("GET", f"/api/slack-ingests/{ID}"): _ingest("ready", read_mark=0),
                        ("GET", T): _t(mark=17)}))
    r = _run("transcript", ID, "--out", str(tmp_path / "missing-dir" / "c.md"))
    assert r.exit_code != 0
    assert not [c for c in srv.calls if c[0] == "POST"]


def test_an_old_canopy_web_without_a_read_mark_gets_a_whole_read(serve, tmp_path):
    srv = serve(Server({("GET", f"/api/slack-ingests/{ID}"): _ingest("ready"),
                        ("GET", T): {"id": ID, "status": "ready", "messages": 2, "markdown": "# all\n"}}))
    r = _run("transcript", ID, "--out", str(tmp_path / "c.md"))
    assert r.exit_code == 0, r.output
    assert "no read mark" in r.output and "the whole transcript" in r.output
    assert not [c for c in srv.calls if c[0] == "POST"]


def test_to_stdout_only_the_markdown_goes_to_stdout(serve):
    serve(Server({("GET", f"/api/slack-ingests/{ID}"): _ingest("ready", read_mark=0),
                  ("GET", T): _t(mark=17),
                  ("POST", f"/api/slack-ingests/{ID}/read-mark"): _ingest("ready", read_mark=17)}))
    r = _run("transcript", ID)                                # click >= 8.2 keeps stderr apart
    assert r.exit_code == 0, r.output
    assert r.stdout == "# Slack #canopy-support\n"
    assert "read mark -> 17" in r.stderr


def test_revoke_and_list(serve):
    srv = serve(Server({("POST", f"/api/slack-ingests/{ID}/revoke"): _ingest("revoked"),
                        ("GET", "/api/slack-ingests/?agent=hal"): [_ingest("ready", messages=6)]}))
    assert "revoked" in _run("revoke", ID).output
    r = _run("list", "--agent", "hal")
    assert "ready" in r.output and "6 msg" in r.output
    assert ("GET", "/api/slack-ingests/?agent=hal", None) in srv.calls


def test_an_allow_listed_request_says_it_is_reading_now(serve):
    serve(Server({("POST", "/api/slack-ingests/"): _ingest(status="reading")}))
    r = _run("request", "--agent", "hal", "--channel", "#canopy-support", "--purpose", "digest")
    assert r.exit_code == 0, r.output
    assert "allow-listed for hal: canopy is reading it now" in r.output


ENTRY = {"id": "a1", "agent": "hal", "channel": "C1", "channel_name": "canopy-support", "follow": False,
         "note": "champions", "added_by_email": "jj@dimagi.com", "added_at": ""}
CFG = {"channel_ingest": {"enabled": False, "allowed": [ENTRY]}}


def test_allow_posts_to_the_workspace_allow_list(serve):
    srv = serve(Server({("POST", "/api/slack-config/dimagi/channel-ingest/allowed"): CFG}))
    r = _run("allow", "--workspace", "dimagi", "--agent", "hal", "--channel", "#canopy-support",
             "--note", " champions ")
    assert r.exit_code == 0, r.output
    assert srv.calls == [("POST", "/api/slack-config/dimagi/channel-ingest/allowed",
                          {"agent": "hal", "channel": "#canopy-support", "follow": False, "note": "champions"})]
    assert "hal <- #canopy-support  read once  by jj@dimagi.com — champions" in r.output


def test_allowed_lists_the_entries(serve):
    serve(Server({("GET", "/api/slack-config/dimagi"): CFG}))
    r = _run("allowed", "--workspace", "dimagi")
    assert r.exit_code == 0 and "a1  hal <- #canopy-support" in r.output


def test_disallow_by_agent_and_channel_name_finds_the_entry(serve):
    srv = serve(Server({("GET", "/api/slack-config/dimagi"): CFG,
                        ("DELETE", "/api/slack-config/dimagi/channel-ingest/allowed/a1"):
                            {"channel_ingest": {"enabled": False, "allowed": []}}}))
    r = _run("disallow", "--workspace", "dimagi", "--agent", "hal", "--channel", "#canopy-support")
    assert r.exit_code == 0, r.output
    assert srv.calls[-1][:2] == ("DELETE", "/api/slack-config/dimagi/channel-ingest/allowed/a1")
    assert "no allow-listed channels" in r.output


def test_disallow_needs_an_id_or_agent_and_channel(serve):
    serve(Server({}))
    assert _run("disallow", "--workspace", "dimagi", "--agent", "hal").exit_code != 0
