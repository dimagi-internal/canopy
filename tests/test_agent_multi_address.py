"""An agent can hold several mail addresses, and its Google account is not its mail.

Ada keeps her Gmail mailbox ada@dimagi-ai.com and ALSO answers at ada@dimagi.ai, which
canopy-web hosts on SES. A new agent may have ONLY <slug>@dimagi.ai for mail while its
Drive/Docs/gog work runs as <slug>@dimagi-ai.com. config/agent.json says so:

    "email":          the primary mail address
    "google_account": the Google account gog runs as (default: email)
    "addresses":      further mail addresses the agent answers at

These tests pin: the identity parse and its defaults; a UUID thread routing to SES whatever
the primary is (and the fallback for an older canopy-web); `send --from`; self-detection
across every own address; the guard against gog running as an SES address; the register
body; and the readiness probe reading config instead of the hardcoded domain.

Run: uv run --extra browser pytest tests/test_agent_multi_address.py
"""
import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from orchestrator import agent_email, agent_health, agent_web, canopy_web
from orchestrator import review_receipt as _rr
from orchestrator.agent_email import (
    AgentEmailError,
    EmailIdentity,
    _is_self,
    _reply_all_of,
    email_group,
    resolve_email_identity,
)

UUID = "3f2a9c1e-0000-4000-8000-000000000001"
GMAIL = "ada@dimagi-ai.com"
SES = "ada@dimagi.ai"
SES_ROUTE = {"backend": "ses", "workspace": "dimagi", "agent": "ada", "address": SES}
PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    for cache in (agent_email._BACKEND_CACHE, agent_email._RESOLVE_FAILURES,
                  agent_email._THREAD_CACHE):
        cache.clear()
    monkeypatch.setattr(agent_email, "engine_staleness_error", lambda *a, **k: None)
    monkeypatch.setenv("CANOPY_REVIEW_RECEIPTS_DIR", str(tmp_path / "receipts"))
    yield
    for cache in (agent_email._BACKEND_CACHE, agent_email._RESOLVE_FAILURES,
                  agent_email._THREAD_CACHE):
        cache.clear()


def _repo(tmp_path, **agent_json):
    repo = tmp_path / "ada"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "ada"}))
    (repo / "config").mkdir()
    (repo / "config" / "agent.json").write_text(json.dumps(
        {"gog_client": "canopy", **agent_json}))
    return repo


def _not_found(path="/api/mail/resolve"):
    return canopy_web.CanopyError(f"GET {path} -> 404: not found")


class Web:
    """Stands in for canopy_web.call. `addresses` maps an address to its resolve payload
    (absent = 404); `threads` maps a thread UUID to its payload, or an exception for a
    server that does not know `?thread=`."""

    def __init__(self, addresses=None, threads=None):
        self.addresses = addresses or {}
        self.threads = threads if threads is not None else {}
        self.calls = []

    def __call__(self, method, path, body=None, **kw):
        from urllib.parse import parse_qs, urlparse
        self.calls.append((method, path, body))
        if path.startswith("/api/mail/resolve"):
            q = {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}
            if "thread" in q:
                got = self.threads.get(q["thread"], _not_found(path))
            else:
                got = self.addresses.get(q.get("address", ""), _not_found(path))
            if isinstance(got, Exception):
                raise got
            return got
        if method == "GET" and "/api/mail/threads/" in path:
            return {"thread_id": UUID, "subject": "s", "messages": []}
        return {"id": 9, "thread_id": UUID, "message_id": "<m@x>"}


@pytest.fixture
def no_gog(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"gog spawned: {a}")

    monkeypatch.setattr(subprocess, "run", boom)
    for name in ("send", "mark_read", "archive", "derive_reply_all", "reconcile_client"):
        monkeypatch.setattr(agent_email, name, boom)


def _run(*args):
    return CliRunner().invoke(email_group, list(args))


def _body(tmp_path, text="Hello there"):
    p = tmp_path / "body.txt"
    p.write_text(text)
    _rr.record("ada", text, caught=[], verdict="clean")
    return str(p)


# ---- identity --------------------------------------------------------------------------

def test_an_agent_with_only_email_keeps_the_old_identity(tmp_path):
    ident = resolve_email_identity(_repo(tmp_path, email=GMAIL))
    assert (ident.account, ident.email, ident.google_account) == (GMAIL, GMAIL, "")
    assert ident.addresses == frozenset({GMAIL})


def test_google_account_and_addresses_are_parsed_and_lowercased(tmp_path):
    ident = resolve_email_identity(_repo(
        tmp_path, email="Ada@Dimagi.ai", google_account="ada@dimagi-ai.com",
        addresses=["ADA@marshellis.com"]))
    assert ident.account == "ada@dimagi-ai.com"   # gog runs as the Google account
    assert ident.email == "Ada@Dimagi.ai"          # mail goes from the primary
    assert ident.addresses == frozenset(
        {"ada@dimagi.ai", "ada@dimagi-ai.com", "ada@marshellis.com"})


def test_a_single_string_address_is_accepted(tmp_path):
    ident = resolve_email_identity(_repo(tmp_path, email=GMAIL, addresses=SES))
    assert ident.addresses == frozenset({GMAIL, SES})


def test_an_identity_built_the_old_way_fills_email_and_addresses():
    ident = EmailIdentity(slug="hal", account="Hal@Dimagi-AI.com", client="canopy")
    assert ident.email == "Hal@Dimagi-AI.com"
    assert ident.addresses == frozenset({"hal@dimagi-ai.com"})


# ---- routing: a UUID thread is SES whatever the primary --------------------------------

def test_a_uuid_thread_routes_to_ses_for_a_gmail_primary_agent(monkeypatch, tmp_path, no_gog):
    web = Web(threads={UUID: SES_ROUTE})
    monkeypatch.setattr(canopy_web, "call", web)
    repo = _repo(tmp_path, email=GMAIL, addresses=[SES])
    ident, route = agent_email._route_for(str(repo), None, None, None, thread_id=UUID)
    assert route == SES_ROUTE
    assert ident.account == SES and GMAIL in ident.addresses
    assert ("GET", f"/api/mail/resolve?thread={UUID}", None) in web.calls


def test_an_older_server_falls_back_to_resolving_each_own_address(monkeypatch, tmp_path,
                                                                  no_gog):
    """canopy-web without `?thread=` 422s it: try `?address=` per own address, primary
    first, and take the first SES answer."""
    old = {"backend": "ses", "workspace": "dimagi", "agent": "ada"}   # no "address" yet
    web = Web(addresses={SES: old},
              threads={UUID: canopy_web.CanopyError("GET /api/mail/resolve -> 422: address")})
    monkeypatch.setattr(canopy_web, "call", web)
    repo = _repo(tmp_path, email=GMAIL, addresses=[SES])
    ident, route = agent_email._route_for(str(repo), None, None, None, thread_id=UUID)
    assert route["workspace"] == "dimagi" and ident.account == SES
    resolves = [p for _, p, _ in web.calls if p.startswith("/api/mail/resolve?address=")]
    assert resolves[0].endswith("ada%40dimagi-ai.com")   # primary asked first


def test_a_uuid_no_address_resolves_for_is_refused_by_name(monkeypatch, tmp_path, no_gog):
    monkeypatch.setattr(canopy_web, "call", Web())
    repo = _repo(tmp_path, email=GMAIL)
    with pytest.raises(AgentEmailError, match="addresses"):
        agent_email._route_for(str(repo), None, None, None, thread_id=UUID)


def test_a_gmail_thread_id_keeps_the_primary_route(monkeypatch, tmp_path):
    web = Web(threads={UUID: SES_ROUTE})
    monkeypatch.setattr(canopy_web, "call", web)
    monkeypatch.setattr(agent_email, "_identity_from_opts",
                        lambda *a: EmailIdentity(slug="ada", account=GMAIL, client="canopy"))
    repo = _repo(tmp_path, email=GMAIL, addresses=[SES])
    ident, route = agent_email._route_for(str(repo), None, None, None,
                                          thread_id="18c2f0a9b1d2e3f4")
    assert route is None and ident.account == GMAIL
    assert not any("thread=" in p for _, p, _ in web.calls)


def test_reply_on_a_uuid_thread_goes_through_ses_for_a_gmail_primary(monkeypatch, tmp_path,
                                                                    no_gog):
    web = Web(threads={UUID: SES_ROUTE})
    monkeypatch.setattr(canopy_web, "call", web)
    repo = _repo(tmp_path, email=GMAIL, addresses=[SES])
    r = _run("send", "--repo", str(repo), "--reply-all", "--thread-id", UUID,
             "--subject", "Re: x", "--body-file", _body(tmp_path))
    assert r.exit_code == 0, r.output
    post = [c for c in web.calls if c[0] == "POST"]
    assert post and post[0][1] == f"/api/mail/threads/dimagi/{UUID}/reply"
    assert "from_address" not in post[0][2]


def test_mark_read_routes_each_thread_by_its_own_ref(monkeypatch, tmp_path):
    web = Web(threads={UUID: SES_ROUTE})
    monkeypatch.setattr(canopy_web, "call", web)
    gmail_done = []
    monkeypatch.setattr(agent_email, "_identity_from_opts",
                        lambda *a: EmailIdentity(slug="ada", account=GMAIL, client="canopy"))
    monkeypatch.setattr(agent_email, "mark_read", lambda ident, tids: gmail_done.extend(tids) or [
        {"thread_id": t, "ok": True, "error": ""} for t in tids])
    repo = _repo(tmp_path, email=GMAIL, addresses=[SES])
    r = _run("mark-read", "--repo", str(repo), "18c2f0a9b1d2e3f4", UUID)
    assert r.exit_code == 0, r.output
    assert gmail_done == ["18c2f0a9b1d2e3f4"]
    assert ("POST", f"/api/mail/threads/dimagi/{UUID}/state", {"unread": False}) in web.calls
    assert r.output.splitlines() == [f"18c2f0a9b1d2e3f4 -> read", f"{UUID} -> read"]


# ---- send --from -----------------------------------------------------------------------

def test_from_an_own_ses_address_sends_through_ses_with_from_address(monkeypatch, tmp_path,
                                                                    no_gog):
    web = Web(addresses={SES: SES_ROUTE})
    monkeypatch.setattr(canopy_web, "call", web)
    repo = _repo(tmp_path, email=GMAIL, addresses=[SES])
    r = _run("send", "--repo", str(repo), "--from", "Ada@Dimagi.ai", "--to", "bob@x.org",
             "--subject", "Hi", "--body-file", _body(tmp_path))
    assert r.exit_code == 0, r.output
    post = [c for c in web.calls if c[0] == "POST"]
    assert post[0][1] == "/api/mail/send/dimagi"
    assert post[0][2]["from_address"] == "Ada@Dimagi.ai"
    assert json.loads(r.output)["backend"] == "ses"


def test_from_an_address_that_is_not_the_agents_is_refused(monkeypatch, tmp_path, no_gog):
    monkeypatch.setattr(canopy_web, "call", Web(addresses={SES: SES_ROUTE}))
    repo = _repo(tmp_path, email=GMAIL, addresses=[SES])
    r = _run("send", "--repo", str(repo), "--from", "eva@dimagi-ai.com", "--to", "b@x.org",
             "--subject", "Hi", "--body-file", _body(tmp_path))
    assert r.exit_code != 0
    assert "not one of this agent's own addresses" in r.output
    assert GMAIL in r.output and SES in r.output        # names the allowed set


def test_from_is_for_new_threads_only(tmp_path):
    repo = _repo(tmp_path, email=GMAIL, addresses=[SES])
    r = _run("send", "--repo", str(repo), "--from", SES, "--reply-all", "--thread-id", UUID,
             "--subject", "Re", "--body-file", _body(tmp_path))
    assert r.exit_code != 0 and "NEW threads" in r.output


def test_from_the_google_account_takes_the_gmail_path(monkeypatch, tmp_path):
    """An SES-primary agent naming its Google account sends through Gmail as it."""
    monkeypatch.setattr(canopy_web, "call", Web(addresses={SES: SES_ROUTE}))
    sent = []
    monkeypatch.setattr(agent_email, "reconcile_client",
                        lambda ident, **k: SimpleNamespace(changed=False, note=""))
    monkeypatch.setattr(agent_email, "send",
                        lambda ident, **k: sent.append(ident.account) or {"thread_id": "t"})
    repo = _repo(tmp_path, email=SES, google_account=GMAIL)
    r = _run("send", "--repo", str(repo), "--from", GMAIL, "--to", "b@x.org",
             "--subject", "Hi", "--body-file", _body(tmp_path))
    assert r.exit_code == 0, r.output
    assert sent == [GMAIL]


def test_without_from_an_ses_primary_sends_through_ses_unchanged(monkeypatch, tmp_path,
                                                                 no_gog):
    web = Web(addresses={SES: SES_ROUTE})
    monkeypatch.setattr(canopy_web, "call", web)
    repo = _repo(tmp_path, email=SES, google_account=GMAIL)
    r = _run("send", "--repo", str(repo), "--to", "b@x.org", "--subject", "Hi",
             "--body-file", _body(tmp_path))
    assert r.exit_code == 0, r.output
    post = [c for c in web.calls if c[0] == "POST"][0]
    assert post[1] == "/api/mail/send/dimagi" and "from_address" not in post[2]


# ---- self-detection across every own address -----------------------------------------

def _msg(mid, frm, to="", cc=""):
    return {"id": mid, "payload": {"headers": [
        {"name": "From", "value": frm}, {"name": "To", "value": to},
        {"name": "Cc", "value": cc}]}}


def test_is_self_counts_every_own_address():
    ident = EmailIdentity(slug="ada", account=GMAIL, client="canopy", addresses={SES})
    assert _is_self(f"Ada <{SES}>", ident)
    assert _is_self(GMAIL, ident)
    assert not _is_self("bob@x.org", ident)
    assert _is_self(GMAIL, GMAIL)    # the one-address form still works


def test_reply_all_skips_the_agents_reply_from_its_other_address():
    ident = EmailIdentity(slug="ada", account=GMAIL, client="canopy", addresses={SES})
    msgs = [_msg("m1", "Bob <bob@x.org>", to=GMAIL, cc="carol@x.org"),
            _msg("m2", f"Ada <{SES}>", to="bob@x.org", cc=f"carol@x.org, {GMAIL}")]
    ra = _reply_all_of(msgs, ident)
    assert ra["to"] == "bob@x.org" and ra["reply_to_message_id"] == "m1"
    cc = {a.strip() for a in ra["cc"].split(",") if a.strip()}
    assert cc == {"carol@x.org"}         # neither of the agent's addresses


def test_dropped_participants_never_counts_an_own_address(monkeypatch):
    ident = EmailIdentity(slug="ada", account=GMAIL, client="canopy", addresses={SES})
    monkeypatch.setattr(agent_email, "derive_reply_all",
                        lambda *a, **k: ("bob@x.org", f"{SES}, carol@x.org", "m1"))
    assert agent_email.dropped_participants(ident, thread_id="t", to="bob@x.org",
                                            cc=None) == ["carol@x.org"]


def test_derive_reply_all_message_mode_drops_every_own_address():
    ident = EmailIdentity(slug="ada", account=GMAIL, client="canopy", addresses={SES})
    thread = {"thread": {"messages": [
        _msg("m1", "bob@x.org", to=f"{GMAIL}, {SES}", cc="carol@x.org")]}}

    def runner(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout=json.dumps(thread), stderr="")

    to, cc, mid = agent_email.derive_reply_all(ident, message_id="m1", runner=runner)
    assert (to, cc, mid) == ("bob@x.org", "carol@x.org", "m1")


def test_dangling_treats_a_last_message_from_either_address_as_ours(monkeypatch):
    ident = EmailIdentity(slug="ada", account=GMAIL, client="canopy", addresses={SES})
    monkeypatch.setattr(agent_email, "search_threads",
                        lambda *a, **k: [{"id": "t1", "subject": "s"}])
    reader = lambda *a, **k: {"messages": [
        {"from": "bob@x.org", "date": "Mon, 12 Oct 2026 09:00:00 +0000"},
        {"from": f"Ada <{SES}>", "date": "Mon, 12 Oct 2026 10:00:00 +0000"}]}
    assert agent_email.dangling_threads(ident, reader=reader, owners={},
                                        runner=lambda *a, **k: None) == []


# ---- gog never runs as an SES address --------------------------------------------------

def test_an_ses_primary_without_google_account_names_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(canopy_web, "call", Web(addresses={SES: SES_ROUTE}))
    repo = _repo(tmp_path, email=SES)
    with pytest.raises(AgentEmailError, match="google_account"):
        agent_email._identity_from_opts(str(repo), None, None, None)


def test_an_ses_primary_with_google_account_runs_gog_as_it(monkeypatch, tmp_path):
    web = Web(addresses={SES: SES_ROUTE})
    monkeypatch.setattr(canopy_web, "call", web)
    monkeypatch.setattr(agent_email, "reconcile_client",
                        lambda ident, **k: SimpleNamespace(changed=False, note=""))
    repo = _repo(tmp_path, email=SES, google_account=GMAIL)
    assert agent_email._identity_from_opts(str(repo), None, None, None).account == GMAIL
    assert not web.calls      # a declared google_account needs no probe


def test_a_workspace_domain_address_is_never_probed(monkeypatch, tmp_path):
    web = Web()
    monkeypatch.setattr(canopy_web, "call", web)
    monkeypatch.setattr(agent_email, "reconcile_client",
                        lambda ident, **k: SimpleNamespace(changed=False, note=""))
    agent_email._identity_from_opts(str(_repo(tmp_path, email=GMAIL)), None, None, None)
    assert not web.calls


# ---- a hosted-only agent is complete for mail, not misconfigured -------------------------

def test_hosted_only_is_an_ses_primary_with_no_google_account(monkeypatch):
    monkeypatch.setattr(canopy_web, "call", Web(addresses={SES: SES_ROUTE}))
    assert agent_email.hosted_only(EmailIdentity(slug="ada", account=SES, client="canopy"))
    assert not agent_email.hosted_only(EmailIdentity(
        slug="ada", account=GMAIL, client="canopy", email=SES, google_account=GMAIL))
    assert not agent_email.hosted_only(EmailIdentity(slug="ada", account=GMAIL, client="canopy"))


def test_hosted_only_is_false_when_canopy_web_cannot_say(monkeypatch):
    def down(*a, **k):
        raise canopy_web.CanopyError("GET /api/mail/resolve -> 503")
    monkeypatch.setattr(canopy_web, "call", down)
    # Unknown is not hosted: the gog checks run (and fail loudly) exactly as before.
    assert not agent_email.hosted_only(EmailIdentity(slug="ada", account=SES, client="canopy"))


def test_preflight_passes_a_hosted_only_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(canopy_web, "call", Web(addresses={SES: SES_ROUTE}))
    monkeypatch.setattr(agent_email, "preflight",
                        lambda *a, **k: pytest.fail("a hosted mailbox needs no gog probe"))
    result = CliRunner().invoke(email_group, ["preflight", "--repo", str(_repo(tmp_path, email=SES))])
    assert result.exit_code == 0, result.output
    assert "canopy-web hosts" in result.output and "google_account" in result.output


def test_dangling_skips_a_hosted_only_agent_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(canopy_web, "call", Web(addresses={SES: SES_ROUTE}))
    monkeypatch.setattr(agent_email, "dangling_threads",
                        lambda *a, **k: pytest.fail("no Gmail inbox to sweep"))
    result = CliRunner().invoke(email_group, ["dangling", "--repo", str(_repo(tmp_path, email=SES))])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Skipped:")
    assert "canopy email inbox" in result.output


def test_health_does_not_flag_a_hosted_only_agent_inbox(monkeypatch):
    monkeypatch.setattr(agent_health, "mailbox_is_hosted", lambda slug: True)
    monkeypatch.setattr(agent_health, "list_gog_accounts",
                        lambda **k: pytest.fail("a hosted mailbox has no gog inbox"))
    monkeypatch.setattr(agent_health, "probe_board",
                        lambda slug, **k: {"needs_you": [], "harness_turns": [],
                                           "turn_age_days": None})
    rep = agent_health.health_report("ada")
    assert rep["flags"] == [] and rep["ready"] is True
    assert rep["inbox"]["hosted"] is True


# ---- gating: --from is not the --account rail ------------------------------------------

@pytest.fixture
def guard(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    loader = importlib.machinery.SourceFileLoader(
        "gating_guard_multi_address", str(PLUGIN / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    (tmp_path / "gate" / "config").mkdir(parents=True)
    (tmp_path / "gate" / "config" / "gating.json").write_text(json.dumps(
        {"slug": "ada", "channels": ["email"], "deny": [], "approve": []}))
    return lambda cmd: mod.run(str(tmp_path / "gate"),
                               {"tool_name": "Bash", "tool_input": {"command": cmd}})


def test_send_from_passes_the_rail(guard):
    code, _, err = guard("canopy email send --repo . --from ada@dimagi.ai --to b@x.org "
                         "--subject s --body-file /tmp/b.txt")
    assert code == 0, err


def test_send_account_is_still_blocked(guard):
    code, _, err = guard("canopy email send --account eva@dimagi-ai.com --to b@x.org "
                         "--subject s --body-file /tmp/b.txt")
    assert code == 2
    assert "--from" in err    # the refusal names the sanctioned path


# ---- register carries google_account only when declared --------------------------------

def test_register_sends_google_account_only_when_declared(monkeypatch, tmp_path):
    bodies = []
    monkeypatch.setattr(agent_web, "_call", lambda path, body=None, method="POST":
                        bodies.append(body) or {})
    agent_web.register(_repo(tmp_path, email=GMAIL))
    assert "google_account" not in bodies[-1]
    other = tmp_path / "b"
    other.mkdir()
    agent_web.register(_repo(other, email=SES, google_account=GMAIL))
    assert bodies[-1]["google_account"] == GMAIL and bodies[-1]["email"] == SES


def test_register_retries_without_google_account_on_an_older_server(monkeypatch, tmp_path):
    bodies = []

    def call(path, body=None, method="POST"):
        bodies.append(body)
        if "google_account" in body:
            raise agent_web.AgentWebError(
                "POST /api/agents/ -> 422: google_account: Extra inputs are not permitted")
        return {"slug": "ada"}

    monkeypatch.setattr(agent_web, "_call", call)
    assert agent_web.register(_repo(tmp_path, email=SES, google_account=GMAIL)) == {
        "slug": "ada"}
    assert len(bodies) == 2 and "google_account" not in bodies[1]


# ---- readiness reads the Google account from config ------------------------------------

def test_health_resolves_the_configured_google_account(monkeypatch, tmp_path):
    repo = _repo(tmp_path, email=SES, google_account="ada-google@dimagi-ai.com")
    monkeypatch.setattr(agent_email, "find_agent_repo", lambda slug: repo)
    accounts = [{"email": "ada-google@dimagi-ai.com", "client": "canopy"},
                {"email": GMAIL, "client": "canopy"}]
    assert agent_health.resolve_mailbox("ada", accounts) == (
        "ada-google@dimagi-ai.com", "canopy")


def test_health_falls_back_to_the_convention_without_config(monkeypatch):
    def missing(slug):
        raise AgentEmailError("no repo")

    monkeypatch.setattr(agent_email, "find_agent_repo", missing)
    assert agent_health.google_account_for("zed") == "zed@dimagi-ai.com"


def test_an_ses_only_agent_can_still_record_a_review_receipt(monkeypatch, tmp_path):
    """The receipt gates every SES send and never calls gog, so it needs no Google account."""
    monkeypatch.setattr(canopy_web, "call", Web(addresses={SES: SES_ROUTE}))
    body = tmp_path / "b.txt"
    body.write_text("Hello")
    r = _run("review-receipt", "--repo", str(_repo(tmp_path, email=SES)),
             "--body-file", str(body))
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["slug"] == "ada"
