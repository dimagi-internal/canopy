"""`canopy brain preview` — page canopy-web's brain preview and lay it out on disk.

Fixture responses in the shape canopy-web's preview endpoint answers. What must hold: every
page is read (cursor, until done) with a window frozen at the start; a session split across
pages is joined; the step-up path reads through the act-as grant and never prints its token;
only GETs are made; one file per session + index.json; the summary counts are right; a 404 / an
agent refusal says what to do instead of a traceback.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse

import pytest
from click.testing import CliRunner

from orchestrator import act_as_cli, brain_cli, canopy_web

BASE = "https://canopy.test"
NOW = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)
SECRET = "cat_SECRET-person-token"


def _session(sid, *, ws="dimagi", now_ok=False, agreed_ok=True, links=(), msgs=(0, 1),
             truncated=False):
    return {
        "id": sid, "title": f"title {sid}", "workspace": {"id": 1, "slug": ws},
        "started_at": "2026-11-20T08:00:00Z",
        "participants": [{"user_id": 7, "contact_id": None, "name": "Jonathan"}],
        "would_read": {
            "project": {"now": {"ok": now_ok, "reason": "" if now_ok else "not_consented: user 12 (project)"},
                        "if_everyone_agreed": {"ok": agreed_ok, "reason": "" if agreed_ok else "sealed: sensitive"}},
            "system": {"now": {"ok": True, "reason": ""}, "if_everyone_agreed": {"ok": True, "reason": ""}},
        },
        "links": [{"project": p, "tier": t, "how": "signals", "evidence": {"why": "x"}} for p, t in links],
        "transcript": {"view": "full", "truncated": truncated, "chars": 10 * len(msgs),
                       "messages": [{"index": i, "role": "user" if i % 2 == 0 else "assistant",
                                     "text": f"m{i}", "author": None} for i in msgs]},
    }


PAGE1 = {"window": {"since": "2026-11-19T12:00:00Z", "until": "2026-11-20T12:00:00Z"}, "view": "full",
         "sessions": [_session("s-1", now_ok=True, links=[("canopy", "core"), ("ace", "adjacent")]),
                      _session("s-2", links=[("canopy", "candidate")], msgs=(0, 1), truncated=True)],
         "next_after": "c1", "done": False}
PAGE2 = {"window": PAGE1["window"], "view": "full",
         # s-2's transcript continues on this page.
         "sessions": [{**_session("s-2", links=[], msgs=(2, 3)), "links": []},
                      _session("s-3", ws="", agreed_ok=False)],
         "next_after": None, "done": True}


class Server:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def answer(self, method, path, auth):
        self.calls.append((method, path, auth))
        u = urllib.parse.urlsplit(path)
        if u.path != "/api/brain/preview":
            return 404, '{"detail": "Not Found"}'
        q = dict(urllib.parse.parse_qsl(u.query))
        return 200, json.dumps(self.pages[q.get("after", "")])


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CANOPY_WEB_API_URL", BASE)
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    monkeypatch.setattr(brain_cli, "NOW", lambda: NOW)
    return tmp_path


@pytest.fixture()
def person(env, monkeypatch):
    """Own-login path: canopy_web.call with a person's PAT, over a fake transport."""
    srv = Server({"": PAGE1, "c1": PAGE2})
    monkeypatch.setenv("CANOPY_WEB_PAT", "person-pat")
    real_call = canopy_web.call

    def transport(method, url, headers, body):
        assert body is None
        return srv.answer(method, url[len(BASE):], headers.get("Authorization"))

    monkeypatch.setattr(canopy_web, "call",
                        lambda m, p, b=None, **k: real_call(m, p, b, transport=transport, **k))
    return srv


def run(*args):
    return CliRunner().invoke(brain_cli.brain_group, ["preview", *args])


def test_pages_until_done_and_writes_one_file_per_session(person, tmp_path):
    out = tmp_path / "out"
    r = run("--out", str(out))
    assert r.exit_code == 0, r.output
    # Two pages, cursor carried, window frozen and identical on both.
    assert [c[0] for c in person.calls] == ["GET", "GET"]
    q = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(c[1]).query)) for c in person.calls]
    assert "after" not in q[0] and q[1]["after"] == "c1"
    assert q[0]["since"] == q[1]["since"] == "2026-11-19T12:00:00Z"
    assert q[0]["until"] == q[1]["until"] == "2026-11-20T12:00:00Z"
    assert q[0]["view"] == "full" and q[0]["limit"] == "10"
    assert person.calls[0][2] == "Bearer person-pat"
    assert sorted(p.name for p in out.iterdir()) == ["index.json", "s-1.json", "s-2.json", "s-3.json"]
    s1 = json.loads((out / "s-1.json").read_text())
    assert s1["url"] == f"{BASE}/w/dimagi/sessions/s-1"
    assert json.loads((out / "s-3.json").read_text())["url"] == f"{BASE}/sessions/s-3"


def test_a_session_split_across_pages_is_joined(person, tmp_path):
    out = tmp_path / "out"
    assert run("--out", str(out)).exit_code == 0
    s2 = json.loads((out / "s-2.json").read_text())
    assert [m["index"] for m in s2["transcript"]["messages"]] == [0, 1, 2, 3]
    assert s2["transcript"]["truncated"] is False  # the last page's word wins
    assert s2["transcript"]["chars"] == 40
    assert [ln["tier"] for ln in s2["links"]] == ["candidate"]  # first page's links kept


def test_index_and_summary_counts(person, tmp_path):
    out = tmp_path / "out"
    r = run("--out", str(out))
    idx = json.loads((out / "index.json").read_text())
    c = idx["counts"]
    assert c["sessions"] == 3
    assert c["would_read"]["project"] == {"now": 1, "if_everyone_agreed": 2}
    assert c["would_read"]["system"] == {"now": 3, "if_everyone_agreed": 3}
    assert c["links"] == {"core": 1, "adjacent": 1, "candidate": 1}
    assert c["links_by_project"] == {"ace": {"adjacent": 1}, "canopy": {"core": 1, "candidate": 1}}
    assert [row["file"] for row in idx["sessions"]] == ["s-1.json", "s-2.json", "s-3.json"]
    assert idx["window"]["since"] == "2026-11-19T12:00:00Z" and idx["as"] == "own login"
    assert "sessions: 3" in r.output
    assert "core 1, adjacent 1, candidate 1 (across 2 projects)" in r.output
    assert "nothing was written to canopy-web" in r.output


def test_json_output(person, tmp_path):
    r = run("--out", str(tmp_path / "o"), "--json-output")
    doc = json.loads(r.output)
    assert doc["counts"]["sessions"] == 3 and doc["out"] == str(tmp_path / "o")


def test_default_out_is_a_fresh_temp_dir(person, monkeypatch, tmp_path):
    monkeypatch.setattr(brain_cli.tempfile, "mkdtemp", lambda prefix: str(tmp_path / (prefix + "x")))
    r = run()
    assert r.exit_code == 0, r.output
    assert (tmp_path / "brain-preview-x" / "index.json").exists()


@pytest.mark.parametrize("value,want", [
    ("24h", "2026-11-19T12:00:00Z"), ("90m", "2026-11-20T10:30:00Z"), ("7d", "2026-11-13T12:00:00Z"),
    ("2026-11-18", "2026-11-18T00:00:00Z"), ("2026-11-18T09:00:00+02:00", "2026-11-18T07:00:00Z"),
    ("2026-11-18T09:00:00Z", "2026-11-18T09:00:00Z"),
])
def test_since_accepts_relative_and_iso(value, want):
    assert brain_cli.parse_when(value, NOW) == want


def test_bad_since_and_inverted_window(person):
    assert run("--since", "yesterday").exit_code != 0
    r = run("--since", "2026-11-21")
    assert r.exit_code != 0 and "is not before" in r.output


def test_a_repeated_cursor_stops_instead_of_looping(env):
    pages = iter([{"sessions": [], "next_after": "c1"}, {"sessions": [], "next_after": "c1"}])
    fetch = lambda params: next(pages)
    with pytest.raises(brain_cli.PreviewError, match="same cursor"):
        list(brain_cli.walk(fetch, since="a", until="b", view="full"))


def test_flat_would_read_shape_is_counted_under_all():
    s = {"id": "x", "would_read": {"now": {"ok": False}, "if_everyone_agreed": {"ok": True}}}
    assert brain_cli.counts([s])["would_read"] == {"all": {"now": 0, "if_everyone_agreed": 1}}


def test_a_canopy_web_without_the_route_says_so(env, monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_WEB_PAT", "person-pat")
    real_call = canopy_web.call
    monkeypatch.setattr(canopy_web, "call", lambda m, p, b=None, **k: real_call(
        m, p, b, transport=lambda *a: (404, '{"detail":"Not Found"}'), **k))
    r = run("--out", str(tmp_path / "o"))
    assert r.exit_code != 0 and "no brain preview yet" in r.output
    assert not (tmp_path / "o").exists()


def test_an_agent_login_refusal_points_at_step_up(env, monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_WEB_PAT", "agent-pat")
    real_call = canopy_web.call
    monkeypatch.setattr(canopy_web, "call", lambda m, p, b=None, **k: real_call(
        m, p, b, transport=lambda *a: (403, '{"detail":"people only"}'), **k))
    r = run("--out", str(tmp_path / "o"))
    assert r.exit_code != 0 and "--step-up" in r.output and "act-as-you" in r.output


# --- the step-up path ---------------------------------------------------------------

@pytest.fixture()
def grant(env, monkeypatch):
    srv = Server({"": PAGE1, "c1": PAGE2})
    monkeypatch.setattr(act_as_cli, "grant_key", lambda: "turn-1")
    act_as_cli.write_grant("turn-1", {"id": 7, "status": "approved", "scope": "read",
                                      "expires_at": "2099-01-01T00:00:00Z", "token": SECRET,
                                      "base_url": BASE})

    def transport(method, url, headers, body):
        return srv.answer(method, url[len(BASE):], headers.get("Authorization"))

    monkeypatch.setattr(act_as_cli, "TRANSPORT", transport)
    # The own-login path must not be touched on a step-up.
    monkeypatch.setattr(canopy_web, "call", lambda *a, **k: pytest.fail("used own login"))
    return srv


def test_step_up_reads_as_the_person_through_the_grant(grant, tmp_path):
    out = tmp_path / "out"
    r = run("--step-up", "--out", str(out))
    assert r.exit_code == 0, r.output
    assert [c[2] for c in grant.calls] == [f"Bearer {SECRET}"] * 2
    assert {c[0] for c in grant.calls} == {"GET"}
    assert SECRET not in r.output
    assert SECRET not in "".join(p.read_text() for p in out.iterdir())
    assert json.loads((out / "index.json").read_text())["as"] == "person via act-as step-up"
    assert "person via act-as step-up" in r.output


def test_step_up_without_a_grant_says_how_to_ask(env, monkeypatch, tmp_path):
    monkeypatch.setattr(act_as_cli, "grant_key", lambda: "turn-1")
    monkeypatch.setattr(act_as_cli, "_recover", lambda key: None)
    r = run("--step-up", "--out", str(tmp_path / "o"))
    assert r.exit_code != 0 and "canopy act-as request" in r.output


def test_step_up_404_says_the_route_is_missing(grant, monkeypatch, tmp_path):
    monkeypatch.setattr(act_as_cli, "TRANSPORT", lambda *a: (404, '{"detail":"Not Found"}'))
    r = run("--step-up", "--out", str(tmp_path / "o"))
    assert r.exit_code != 0 and "no brain preview yet" in r.output


def test_brain_is_registered_on_the_main_cli():
    from orchestrator.cli import main
    assert "preview" in main.commands["brain"].commands


# --- the report ---------------------------------------------------------------------

def _learner(out, sid, findings, read=True, not_read=""):
    d = out / "learner"
    d.mkdir(exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps(
        {"session_id": sid, "read": read, "not_read": not_read, "findings": findings}))


def test_report_ranks_merges_and_links_evidence(person, tmp_path):
    out = tmp_path / "out"
    assert run("--out", str(out)).exit_code == 0
    same = {"title": "Step-up link hidden until wait ends", "severity": "medium", "proof": "inferred",
            "what": "say the link first", "route": "skill PR", "target": "canopy:act-as-you"}
    _learner(out, "s-1", [{**same, "evidence": [{"index": 1, "quote": "where is the <link>?"}]},
                          {"title": "Low thing", "severity": "low", "proof": "proven", "what": "w"}])
    _learner(out, "s-2", [{**same, "severity": "high", "evidence": [{"index": 3, "quote": "q2"}]}])
    _learner(out, "s-3", [], read=False, not_read="sealed: sensitive")
    r = CliRunner().invoke(brain_cli.brain_group, ["report", str(out)])
    assert r.exit_code == 0, r.output
    page = (out / "report.html").read_text()
    # Merged into one recommendation, ranked first (high after merge), with both sessions' evidence.
    assert page.index("Step-up link hidden") < page.index("Low thing")
    assert page.count("Step-up link hidden until wait ends") == 1
    assert f"href='{BASE}/w/dimagi/sessions/s-1'" in page and f"href='{BASE}/w/dimagi/sessions/s-2'" in page
    assert "where is the &lt;link&gt;?" in page  # escaped
    assert "not_consented: user 12 (project)" in page and "sealed: sensitive" in page
    assert "<h3>canopy</h3>" in page and "<h3>ace</h3>" in page
    assert "nothing was written to canopy-web" in page


def test_report_prefers_the_consolidated_list(person, tmp_path):
    out = tmp_path / "out"
    assert run("--out", str(out)).exit_code == 0
    _learner(out, "s-1", [{"title": "From learner", "severity": "high"}])
    (out / "recommendations.json").write_text(json.dumps(
        [{"title": "Consolidated one", "severity": "low", "evidence": [{"session_id": "s-3"}]}]))
    assert CliRunner().invoke(brain_cli.brain_group, ["report", str(out)]).exit_code == 0
    page = (out / "report.html").read_text()
    assert "Consolidated one" in page and "From learner" not in page


def test_report_without_a_preview_says_so(tmp_path):
    r = CliRunner().invoke(brain_cli.brain_group, ["report", str(tmp_path)])
    assert r.exit_code != 0 and "no index.json" in r.output


def test_view_userinput_is_sent_and_the_old_typed_name_is_refused(person, tmp_path):
    r = run("--view", "userinput", "--out", str(tmp_path / "o"))
    assert r.exit_code == 0, r.output
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(person.calls[0][1]).query))
    assert q["view"] == "userinput"
    assert brain_cli.VIEWS == ("full", "conversation", "userinput", "opening")
    r = run("--view", "typed", "--out", str(tmp_path / "o2"))
    assert r.exit_code != 0 and "typed" in r.output
