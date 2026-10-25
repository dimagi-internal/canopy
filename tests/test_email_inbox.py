"""`canopy email inbox` — the hosted-mailbox sweep.

canopy-web stores every message to an address it hosts, including mail that never started a
turn (a no-reply sender its admission rules refuse). Before this command an agent could read
ONE hosted thread by UUID but never list them, so that mail was stored and never seen.

These tests pin: unread-only by default, `--all`, `--archived`, the `--json` shape, the
Gmail-only message (exit 0), a Gmail+hosted agent listing its hosted address, and a
canopy-web it could not ask being an error rather than "Gmail-only".

Run: uv run --extra browser pytest tests/test_email_inbox.py
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from click.testing import CliRunner

from orchestrator import agent_email, canopy_web
from orchestrator.agent_email import email_group

SES = "bailey@marshellis.com"
GMAIL = "bailey@dimagi-ai.com"
ROUTE = {"backend": "ses", "workspace": "marshellis", "agent": "bailey", "address": SES}
UNREAD = {"thread_id": "11111111-0000-4000-8000-000000000001", "address": SES,
          "agent": "bailey", "subject": "Verify your email", "message_count": 1,
          "unread": True, "archived": False,
          "last_message_at": "2026-10-25T02:14:00+00:00",
          "counterpart": "googleworkspace-noreply@google.com"}
READ = {**UNREAD, "thread_id": "22222222-0000-4000-8000-000000000002", "unread": False,
        "subject": "Trip plan", "last_message_at": "2026-10-26T09:00:00+00:00",
        "counterpart": "gina@example.com"}
ARCHIVED = {**READ, "thread_id": "33333333-0000-4000-8000-000000000003", "archived": True,
            "subject": "Old receipt"}


@pytest.fixture(autouse=True)
def _fresh():
    for cache in (agent_email._BACKEND_CACHE, agent_email._RESOLVE_FAILURES,
                  agent_email._THREAD_CACHE):
        cache.clear()
    yield
    for cache in (agent_email._BACKEND_CACHE, agent_email._RESOLVE_FAILURES,
                  agent_email._THREAD_CACHE):
        cache.clear()


class Web:
    """Stands in for canopy_web.call: resolves `addresses` (absent = 404, or the exception
    given) and lists threads filtered by `archived`, as canopy-web does."""

    def __init__(self, addresses=None, threads=(UNREAD, READ, ARCHIVED)):
        self.addresses = addresses if addresses is not None else {SES: ROUTE}
        self.threads = list(threads)
        self.calls = []

    def __call__(self, method, path, body=None, **kw):
        self.calls.append((method, path))
        q = {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}
        if path.startswith("/api/mail/resolve"):
            got = self.addresses.get(
                q.get("address", ""), canopy_web.CanopyError(f"GET {path} -> 404: nope"))
            if isinstance(got, Exception):
                raise got
            return got
        assert method == "GET" and path.startswith("/api/mail/threads/marshellis?"), path
        archived = q["archived"] == "true"
        return {"agent_address": SES,
                "items": [t for t in self.threads if t["archived"] == archived]}


def _repo(tmp_path, **agent_json):
    repo = tmp_path / "bailey"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "bailey"}))
    (repo / "config").mkdir()
    (repo / "config" / "agent.json").write_text(json.dumps(agent_json))
    return repo


def _inbox(repo: Path, *args):
    return CliRunner().invoke(email_group, ["inbox", "--repo", str(repo), *args])


def test_default_lists_only_unread_hosted_threads(monkeypatch, tmp_path):
    web = Web()
    monkeypatch.setattr(canopy_web, "call", web)
    res = _inbox(_repo(tmp_path, email=SES))
    assert res.exit_code == 0, res.output
    assert UNREAD["thread_id"] in res.output and "Verify your email" in res.output
    assert READ["thread_id"] not in res.output
    line = next(l for l in res.output.splitlines() if UNREAD["thread_id"] in l)
    assert line.startswith("*") and "googleworkspace-noreply@google.com" in line
    assert "2026-10-25 02:14" in line
    assert ("GET", "/api/mail/threads/marshellis?agent=bailey&archived=false") in web.calls


def test_all_includes_read_threads_newest_first(monkeypatch, tmp_path):
    monkeypatch.setattr(canopy_web, "call", Web())
    res = _inbox(_repo(tmp_path, email=SES), "--all")
    assert res.exit_code == 0, res.output
    rows = [l for l in res.output.splitlines() if not l.startswith("#")]
    assert [READ["thread_id"] in rows[0], UNREAD["thread_id"] in rows[1]] == [True, True]
    assert rows[0].startswith(" ")   # read: no unread marker
    assert ARCHIVED["thread_id"] not in res.output


def test_archived_lists_archived_threads(monkeypatch, tmp_path):
    web = Web()
    monkeypatch.setattr(canopy_web, "call", web)
    res = _inbox(_repo(tmp_path, email=SES), "--archived")
    assert res.exit_code == 0, res.output
    assert ARCHIVED["thread_id"] in res.output and UNREAD["thread_id"] not in res.output
    assert ("GET", "/api/mail/threads/marshellis?agent=bailey&archived=true") in web.calls


def test_json_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(canopy_web, "call", Web())
    res = _inbox(_repo(tmp_path, email=SES), "--json")
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["agent"] == "bailey"
    assert out["mailboxes"] == [{"address": SES, "workspace": "marshellis", "agent": "bailey"}]
    assert [t["thread_id"] for t in out["threads"]] == [UNREAD["thread_id"]]
    t = out["threads"][0]
    for key in ("subject", "counterpart", "unread", "last_message_at", "message_count"):
        assert t[key] == UNREAD[key]
    assert t["workspace"] == "marshellis"


def test_empty_hosted_inbox_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(canopy_web, "call", Web(threads=[READ]))
    res = _inbox(_repo(tmp_path, email=SES))
    assert res.exit_code == 0, res.output
    assert "no unread threads" in res.output


def test_gmail_only_agent_points_at_the_gmail_sweep(monkeypatch, tmp_path):
    web = Web()
    monkeypatch.setattr(canopy_web, "call", web)
    res = _inbox(_repo(tmp_path, email=GMAIL))
    assert res.exit_code == 0, res.output
    assert "no canopy-hosted mailbox" in res.output
    assert "canopy email dangling" in res.output
    assert web.calls == []   # a fleet Workspace address is a Google account: never probed


def test_gmail_only_json_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(canopy_web, "call", Web(addresses={}))
    res = _inbox(_repo(tmp_path, email="someone@example.org"), "--json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == {"agent": "bailey", "mailboxes": [], "threads": []}


def test_gmail_primary_with_a_hosted_address_lists_the_hosted_one(monkeypatch, tmp_path):
    monkeypatch.setattr(canopy_web, "call", Web())
    res = _inbox(_repo(tmp_path, email=GMAIL, addresses=[SES]))
    assert res.exit_code == 0, res.output
    assert f"# {SES} (workspace marshellis)" in res.output
    assert UNREAD["thread_id"] in res.output


def test_unreachable_canopy_web_is_an_error_not_gmail_only(monkeypatch, tmp_path):
    down = canopy_web.CanopyError("GET /api/mail/resolve -> 502: bad gateway")
    monkeypatch.setattr(canopy_web, "call", Web(addresses={SES: down}))
    res = _inbox(_repo(tmp_path, email=SES))
    assert res.exit_code != 0
    assert "could not ask canopy-web" in res.output and "502" in res.output
