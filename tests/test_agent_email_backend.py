"""`canopy email` per-address backend switch: Gmail (gog) vs the SES mail backend.

canopy-web decides which backend serves a mailbox (GET /api/mail/resolve). 404 means
Gmail; the Gmail argv must stay byte-for-byte what it was before the switch existed.
"""
import json
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from orchestrator import agent_email, canopy_web
from orchestrator import review_receipt as _rr
from orchestrator.agent_email import EmailIdentity, build_send_command, email_group

UUID = "3f2a9c1e-0000-4000-8000-000000000001"
SES_ADDR = "ada@marshellis.com"
GMAIL_ADDR = "hal@dimagi-ai.com"
RESOLVE_OK = {"backend": "ses", "workspace": "marshellis", "agent": "ada"}

THREAD = {
    "thread_id": UUID, "address": SES_ADDR, "agent": "ada", "subject": "Quarterly plan",
    "message_count": 2, "unread": True, "archived": False,
    "last_message_at": "2026-10-12T10:00:00+00:00",
    "messages": [
        {"id": 1, "direction": "in", "message_id": "<a@x>", "from_addr": "bob@example.com",
         "from_name": "Bob", "to": [SES_ADDR], "cc": ["cc@example.com"],
         "subject": "Quarterly plan", "text": "Hello Ada", "html": "<p>Hello Ada</p>",
         "delivery_status": "", "created_at": "2026-10-12T09:00:00+00:00",
         "attachments": [{"filename": "plan.pdf", "content_type": "application/pdf",
                          "size": 1234, "url": "https://s3/plan.pdf"}]},
        {"id": 2, "direction": "out", "message_id": "<b@x>", "from_addr": SES_ADDR,
         "from_name": "Ada", "to": ["bob@example.com"], "cc": [], "subject": "Re: Quarterly plan",
         "text": "Hi Bob", "html": "", "delivery_status": "sent",
         "created_at": "2026-10-12T10:00:00+00:00", "attachments": []},
    ],
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    agent_email._BACKEND_CACHE.clear()
    agent_email._RESOLVE_FAILURES.clear()
    monkeypatch.setattr(agent_email, "engine_staleness_error", lambda *a, **k: None)
    monkeypatch.setenv("CANOPY_REVIEW_RECEIPTS_DIR", str(tmp_path / "receipts"))
    yield
    agent_email._BACKEND_CACHE.clear()
    agent_email._RESOLVE_FAILURES.clear()


class FakeWeb:
    """Stands in for canopy_web.call; records (method, path, body)."""

    def __init__(self, resolve=RESOLVE_OK, thread=THREAD):
        self.calls = []
        self.resolve = resolve  # dict, or an Exception instance to raise
        self.thread = thread

    def __call__(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if path.startswith("/api/mail/resolve"):
            if isinstance(self.resolve, Exception):
                raise self.resolve
            return self.resolve
        if method == "GET" and "/api/mail/threads/" in path:
            return self.thread
        return {"id": 9, "thread_id": UUID}


@pytest.fixture
def web(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(canopy_web, "call", w)
    return w


@pytest.fixture
def no_gog(monkeypatch):
    spawned = []

    def boom(*a, **k):
        spawned.append(a)
        raise AssertionError(f"gog spawned for an SES mailbox: {a}")

    # `runner=subprocess.run` is bound at def time, so patching subprocess.run is not
    # enough — block the Gmail engine entry points the CLI dispatches to, and gog itself.
    monkeypatch.setattr(subprocess, "run", boom)
    for name in ("send", "mark_read", "archive", "derive_reply_all", "reconcile_client"):
        monkeypatch.setattr(agent_email, name, boom)
    return spawned


@pytest.fixture
def gog_guard(monkeypatch):
    """Gmail-path tests inject their own runner; this makes any real gog spawn fail."""
    def boom(*a, **k):
        raise AssertionError(f"real subprocess spawned: {a}")
    monkeypatch.setattr(subprocess, "run", boom)


def _body(tmp_path, text="Hello there"):
    p = tmp_path / "body.txt"
    p.write_text(text)
    return str(p)


def _review(text="Hello there"):
    _rr.record("ada", text, caught=[], verdict="clean")


def _run(*args):
    return CliRunner().invoke(email_group, list(args))


def _not_found():
    return canopy_web.CanopyError("GET /api/mail/resolve -> 404: not an SES mailbox")


# ---- (a) Gmail argv byte-for-byte --------------------------------------------------

def test_gmail_argv_snapshots():
    ident = EmailIdentity(slug="hal", account=GMAIL_ADDR, client="canopy")
    cmd = build_send_command(ident, to="a@b.com", subject="S", plain_path="/tmp/p.txt",
                             html_body="<p>x</p>", cc="c@d.com", reply_to_message_id="m1",
                             attachments=["/tmp/f.pdf"])
    assert cmd == ["gog", "gmail", "send", "--account", GMAIL_ADDR, "--client", "canopy",
                   "--to", "a@b.com", "--subject", "S", "--body-file", "/tmp/p.txt",
                   "--body-html", "<p>x</p>", "--json", "--cc", "c@d.com",
                   "--reply-to-message-id", "m1", "--attach", "/tmp/f.pdf"]
    # mark-read / archive argv
    calls = []

    def runner(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    agent_email.mark_read(ident, ["t1"], runner=runner)
    agent_email.archive(ident, ["t1"], runner=runner)
    assert calls == [
        ["gog", "gmail", "thread", "modify", "t1", "--remove", "UNREAD",
         "--account", GMAIL_ADDR, "--client", "canopy"],
        ["gog", "gmail", "thread", "modify", "t1", "--remove", "INBOX,UNREAD",
         "--account", GMAIL_ADDR, "--client", "canopy"],
    ]


def test_404_resolve_keeps_gmail_send_argv(monkeypatch, gog_guard, tmp_path):
    monkeypatch.setattr(canopy_web, "call", FakeWeb(resolve=_not_found()))
    body = _body(tmp_path)
    _rr.record("hal", "Hello there", caught=[], verdict="clean")
    seen = []

    def fake_run(cmd, capture_output, text, timeout):
        seen.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "m", "threadId": "t"}),
                               stderr="")

    real_send = agent_email.send
    monkeypatch.setattr(agent_email, "send",
                        lambda *a, **k: real_send(*a, runner=fake_run, **k))
    r = _run("send", "--account", GMAIL_ADDR, "--agent", "hal", "--client", "canopy",
             "--to", "a@b.com", "--subject", "S", "--body-file", body)
    assert r.exit_code == 0, r.output
    ident = EmailIdentity(slug="hal", account=GMAIL_ADDR, client="canopy")
    plain = agent_email.normalize("Hello there")
    expected = build_send_command(ident, to="a@b.com", subject="S", plain_path="X",
                                  html_body=agent_email.to_html(plain), cc=None,
                                  reply_to_message_id=None, attachments=[])
    assert len(seen) == 1, (r.output, r.exception)
    got = list(seen[0])
    # the temp body-file path is the only varying element
    i = got.index("--body-file")
    got[i + 1] = "X"
    assert got == expected


# ---- (b) SES routing ---------------------------------------------------------------

def test_ses_reply_posts_to_rest_and_never_spawns_gog(web, no_gog, tmp_path):
    _review()
    r = _run("send", "--account", SES_ADDR, "--agent", "ada", "--reply-all",
             "--thread-id", UUID, "--subject", "Re: x", "--body-file", _body(tmp_path))
    assert r.exit_code == 0, r.output
    posts = [c for c in web.calls if c[0] == "POST"]
    assert len(posts) == 1
    method, path, body = posts[0]
    assert path == f"/api/mail/threads/marshellis/{UUID}/reply"
    assert body["text"].strip() == "Hello there"
    assert "<p>" in body["html"]
    assert "to" not in body and "cc" not in body  # server derives reply-all
    assert no_gog == []


def test_ses_new_thread_send(web, no_gog, tmp_path):
    _review()
    r = _run("send", "--account", SES_ADDR, "--to", "bob@example.com, eve@example.com",
             "--cc", "c@example.com", "--subject", "Hi", "--body-file", _body(tmp_path))
    assert r.exit_code == 0, r.output
    (method, path, body), = [c for c in web.calls if c[0] == "POST"]
    assert path == "/api/mail/send/marshellis"
    assert body["agent"] == "ada"
    assert body["to"] == ["bob@example.com", "eve@example.com"]
    assert body["cc"] == ["c@example.com"]
    assert body["subject"] == "Hi"


def test_ses_send_requires_review_receipt(web, no_gog, tmp_path):
    r = _run("send", "--account", SES_ADDR, "--to", "bob@example.com", "--subject", "Hi",
             "--body-file", _body(tmp_path, "never reviewed"))
    assert r.exit_code != 0
    assert not [c for c in web.calls if c[0] == "POST"]


def test_ses_send_refuses_attachments(web, no_gog, tmp_path):
    _review()
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")
    r = _run("send", "--account", SES_ADDR, "--to", "bob@example.com", "--subject", "Hi",
             "--body-file", _body(tmp_path), "--attach", str(f))
    assert r.exit_code != 0
    assert "attachments are not supported for SES mailboxes yet" in r.output
    assert not [c for c in web.calls if c[0] == "POST"]


def test_ses_mark_read_and_archive_post_state(web, no_gog):
    r = _run("mark-read", "--account", SES_ADDR, UUID)
    assert r.exit_code == 0, r.output
    assert f"{UUID} -> read" in r.output
    r = _run("archive", "--account", SES_ADDR, UUID)
    assert r.exit_code == 0, r.output
    assert f"{UUID} -> archived" in r.output
    posts = [(c[1], c[2]) for c in web.calls if c[0] == "POST"]
    assert posts == [
        (f"/api/mail/threads/marshellis/{UUID}/state", {"unread": False}),
        (f"/api/mail/threads/marshellis/{UUID}/state", {"archived": True, "unread": False}),
    ]


def test_ses_state_failure_reported_per_thread(monkeypatch, no_gog):
    w = FakeWeb()
    orig = w.__call__

    def flaky(method, path, body=None, **kw):
        if method == "POST":
            raise canopy_web.CanopyError("POST x -> 404: no such thread")
        return orig(method, path, body, **kw)

    monkeypatch.setattr(canopy_web, "call", flaky)
    r = _run("mark-read", "--account", SES_ADDR, UUID)
    assert r.exit_code == 1
    assert "ERROR" in r.output and "no such thread" in r.output


# ---- (c) thread verb ---------------------------------------------------------------

def test_thread_prints_messages_readably(web, no_gog):
    r = _run("thread", UUID, "--account", SES_ADDR)
    assert r.exit_code == 0, r.output
    assert ("GET", f"/api/mail/threads/marshellis/{UUID}", None) in web.calls
    for needle in ("Quarterly plan", "Bob", "bob@example.com", "Hello Ada", "Hi Bob",
                   "cc@example.com", "plan.pdf"):
        assert needle in r.output


def test_thread_on_gmail_mailbox_says_ses_only(monkeypatch):
    monkeypatch.setattr(canopy_web, "call", FakeWeb(resolve=_not_found()))
    r = _run("thread", "abc123", "--account", GMAIL_ADDR)
    assert r.exit_code != 0
    assert "SES-only" in r.output and "email read" in r.output


# ---- (d) resolution: fallbacks, caching --------------------------------------------

def test_404_is_gmail_silently(monkeypatch, capsys):
    monkeypatch.setattr(canopy_web, "call", FakeWeb(resolve=_not_found()))
    assert agent_email._ses_route(GMAIL_ADDR) is None
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("exc", [
    OSError("network unreachable"),
    canopy_web.CanopyError("GET /api/mail/resolve -> 503: upstream down"),
    canopy_web.CanopyError("GET /api/mail/resolve -> 401: bad token"),
    RuntimeError("no canopy-web token"),
])
def test_resolve_failure_falls_back_to_gmail_loudly_once(monkeypatch, capsys, exc):
    w = FakeWeb(resolve=exc)
    monkeypatch.setattr(canopy_web, "call", w)
    assert agent_email._ses_route(GMAIL_ADDR) is None
    assert agent_email._ses_route(GMAIL_ADDR) is None  # cached: no second probe, no second line
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert GMAIL_ADDR in err and "gmail" in err.lower()
    assert len([c for c in w.calls if c[1].startswith("/api/mail/resolve")]) == 1


def test_resolve_is_cached_per_address(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(canopy_web, "call", w)
    assert agent_email._ses_route("Ada@Marshellis.com")["workspace"] == "marshellis"
    assert agent_email._ses_route(SES_ADDR) is not None
    assert len([c for c in w.calls if c[1].startswith("/api/mail/resolve")]) == 1


def test_resolve_query_url_encodes_the_address(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(canopy_web, "call", w)
    agent_email._ses_route("a+b@marshellis.com")
    assert w.calls[0][1] == "/api/mail/resolve?address=a%2Bb%40marshellis.com"


def test_backend_helper_returns_literal(monkeypatch):
    monkeypatch.setattr(canopy_web, "call", FakeWeb())
    assert agent_email._backend(SES_ADDR) == "ses"
    monkeypatch.setattr(canopy_web, "call", FakeWeb(resolve=_not_found()))
    assert agent_email._backend(GMAIL_ADDR) == "gmail"


# ---- fix round 1 ---------------------------------------------------------------------

def test_ses_rejects_non_uuid_thread_id_without_http(web, no_gog):
    n = len(web.calls)
    for verb in ("mark-read", "archive"):
        r = _run(verb, "--account", SES_ADDR, "../x")
        assert r.exit_code == 1 and "not an SES thread id" in r.output
    r = _run("thread", "../x", "--account", SES_ADDR)
    assert r.exit_code != 0 and "not an SES thread id" in r.output
    assert [c for c in web.calls[n:] if "threads" in c[1]] == []


def test_ses_reply_rejects_non_uuid_thread_id(web, no_gog, tmp_path):
    _review()
    r = _run("send", "--account", SES_ADDR, "--agent", "ada", "--reply-all",
             "--thread-id", "../x", "--subject", "s", "--body-file", _body(tmp_path))
    assert r.exit_code != 0 and "not an SES thread id" in r.output
    assert not [c for c in web.calls if c[0] == "POST"]


@pytest.mark.parametrize("resp", [
    {"backend": "ses"}, {"backend": "ses", "workspace": ""},
    {"backend": "ses", "workspace": 7}, {"backend": "weird"}, ["x"],
])
def test_malformed_200_falls_back_loudly(monkeypatch, capsys, resp):
    monkeypatch.setattr(canopy_web, "call", FakeWeb(resolve=resp))
    assert agent_email._ses_route(SES_ADDR) is None
    assert "falling back to gmail" in capsys.readouterr().err


def test_ses_reply_to_message_id_without_thread_id_refused(web, no_gog, tmp_path):
    _review()
    r = _run("send", "--account", SES_ADDR, "--to", "bob@example.com", "--subject", "s",
             "--reply-to-message-id", "<m@x>", "--body-file", _body(tmp_path))
    assert r.exit_code != 0 and "--thread-id" in r.output
    assert not [c for c in web.calls if c[0] == "POST"]


@pytest.mark.parametrize("verb,label", [("mark-read", "UNREAD"), ("archive", "INBOX,UNREAD")])
def test_404_resolve_keeps_gog_path_for_state_verbs(monkeypatch, gog_guard, verb, label):
    monkeypatch.setattr(canopy_web, "call", FakeWeb(resolve=_not_found()))
    seen = []

    def runner(cmd, capture_output, text, timeout):
        seen.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    for name in ("mark_read", "archive"):
        real = getattr(agent_email, name)
        monkeypatch.setattr(agent_email, name,
                            lambda *a, _r=real, **k: _r(*a, runner=runner, **k))
    r = _run(verb, "--account", GMAIL_ADDR, "--agent", "hal", "--client", "canopy", "t1")
    assert r.exit_code == 0, r.output
    assert seen == [["gog", "gmail", "thread", "modify", "t1", "--remove", label,
                     "--account", GMAIL_ADDR, "--client", "canopy"]]


def test_gmail_failure_after_failed_lookup_mentions_it(monkeypatch, gog_guard):
    monkeypatch.setattr(canopy_web, "call",
                        FakeWeb(resolve=OSError("network unreachable")))
    monkeypatch.setattr(agent_email, "mark_read", lambda ident, ids, **k: [
        {"thread_id": i, "ok": False, "error": "gog: not found"} for i in ids])
    r = _run("mark-read", "--account", SES_ADDR, "--agent", "ada", "--client", "canopy", "t1")
    assert r.exit_code == 1
    assert "gog: not found" in r.output
    assert "mail backend lookup failed earlier: network unreachable" in r.output
    assert "canopy-web was unreachable" in r.output


def test_gmail_failure_after_404_has_no_note(monkeypatch, gog_guard):
    monkeypatch.setattr(canopy_web, "call", FakeWeb(resolve=_not_found()))
    monkeypatch.setattr(agent_email, "archive", lambda ident, ids, **k: [
        {"thread_id": i, "ok": False, "error": "gog: boom"} for i in ids])
    r = _run("archive", "--account", GMAIL_ADDR, "--agent", "hal", "--client", "canopy", "t1")
    assert "gog: boom" in r.output and "lookup failed" not in r.output


def test_send_error_after_failed_lookup_mentions_it(monkeypatch, gog_guard, tmp_path):
    monkeypatch.setattr(canopy_web, "call", FakeWeb(resolve=OSError("dns down")))
    _rr.record("ada", "Hello there", caught=[], verdict="clean")

    def failing(*a, **k):
        raise agent_email.AgentEmailError("gog gmail send failed (exit 1)")

    monkeypatch.setattr(agent_email, "send", failing)
    r = _run("send", "--account", SES_ADDR, "--agent", "ada", "--client", "canopy",
             "--to", "a@b.com", "--subject", "s", "--body-file", _body(tmp_path))
    assert r.exit_code != 0
    assert "send failed" in r.output and "lookup failed earlier: dns down" in r.output


def test_ses_state_verbs_never_touch_identity_resolution(web, no_gog, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("resolve_email_identity called for an SES mailbox")

    monkeypatch.setattr(agent_email, "resolve_email_identity", boom)
    for verb, word in (("mark-read", "read"), ("archive", "archived")):
        r = _run(verb, "--account", SES_ADDR, UUID)
        assert r.exit_code == 0, r.output
        assert f"{UUID} -> {word}" in r.output
