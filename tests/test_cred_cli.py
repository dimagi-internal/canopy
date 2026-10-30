"""`canopy cred` — session identity (canopy#850 design revision). canopy-web mocked."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import cred_cli

RESOLVED = "resolved-value-never-printed"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fake home (cache + ~/.<slug>/.env), cwd outside any repo."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cred_cli.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cred_cli, "CACHE_DIR", home / ".canopy")
    monkeypatch.setattr(cred_cli, "CACHE_FILE", home / ".canopy" / "cred-cache.json")
    monkeypatch.setattr(cred_cli, "_human_token", lambda: "pat_human")
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture()
def web(monkeypatch):
    """Fake canopy-web: /access and /resolve per agent."""
    state = {"access": {}, "resolve": {}, "calls": [], "fail": False}

    def fake_call(method, path, body=None, **kw):
        state["calls"].append((method, path, kw.get("token")))
        if state["fail"]:
            raise cred_cli.canopy_web.CanopyError(f"{method} {path} -> 404: not found")
        slug = path.split("/")[3]
        if path.endswith("/credentials/access"):
            return state["access"].get(slug, {"agent": slug, "may_resolve": False,
                                              "via": None, "reason": ""})
        if path.split("?")[0].endswith("/credentials/resolve"):
            return state["resolve"].get(slug, {"values": {}})
        raise AssertionError(path)

    monkeypatch.setattr(cred_cli.canopy_web, "call", fake_call)
    return state


# ── session identity ──────────────────────────────────────────────────────────

def test_human_session_by_default():
    s = cred_cli.session_identity()
    assert s.kind == "human" and s.turn_id == ""


def test_runner_turn_names_its_agent(monkeypatch):
    monkeypatch.setenv("CANOPY_TURN_ID", "t-1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "ace")
    s = cred_cli.session_identity()
    assert (s.kind, s.agent, s.turn_id) == ("agent-turn", "ace", "t-1")


# ── check: runner turns ───────────────────────────────────────────────────────

def test_turn_may_act_as_its_own_agent_without_asking_anyone(monkeypatch, web):
    monkeypatch.setenv("CANOPY_TURN_ID", "t-1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "ace")
    v = cred_cli.decide("ace")
    assert v.allowed and v.via == "turn"
    assert web["calls"] == []


def test_turn_is_refused_any_other_agent(monkeypatch, web):
    monkeypatch.setenv("CANOPY_TURN_ID", "t-1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "ada")
    r = CliRunner().invoke(cred_cli.cred_group, ["check", "--agent", "ace"])
    assert r.exit_code == cred_cli.EXIT_REFUSED
    assert "may act only as 'ada'" in r.output and "dispatch" in r.output


def test_turn_without_an_agent_may_act_as_none(monkeypatch, web):
    monkeypatch.setenv("CANOPY_TURN_ID", "t-1")
    v = cred_cli.decide("ace")
    assert not v.allowed and v.exit_code == cred_cli.EXIT_REFUSED


# ── check: humans ─────────────────────────────────────────────────────────────

def test_canopy_web_unreachable_refuses(web):
    # An unreachable canopy-web never grants anything by itself.
    web["fail"] = True
    v = cred_cli.decide("ace")
    assert not v.allowed and v.notes


def test_a_server_that_still_sends_credential_source_is_tolerated(web):
    """An older canopy-web sends `credential_source` / `op_vault`: neither is required or used."""
    web["access"]["ace"] = {"credential_source": "legacy", "op_vault": "V", "may_resolve": True,
                            "via": "admin", "reason": "you are an admin of ace"}
    v = cred_cli.decide("ace")
    assert v.allowed and v.via == "admin"
    assert "credential_source" not in cred_cli.fetch_access("ace")


def test_follows_may_resolve(web):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin",
                            "reason": "you are an admin of ace"}
    v = cred_cli.decide("ace")
    assert v.allowed and v.via == "admin"


def test_refusal_names_the_admin_path(web):
    web["access"]["ace"] = {"may_resolve": False, "via": None, "reason": "not an owner or admin"}
    r = CliRunner().invoke(cred_cli.cred_group, ["check", "--agent", "ace"])
    assert r.exit_code == cred_cli.EXIT_REFUSED
    assert "not an owner or admin" in r.output and "admin of 'ace'" in r.output


def test_access_is_cached(web):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    cred_cli.fetch_access("ace")
    cred_cli.fetch_access("ace")
    assert len(web["calls"]) == 1
    assert (cred_cli.CACHE_FILE.stat().st_mode & 0o777) == 0o600


def test_check_json_and_exit_zero(web):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    r = CliRunner().invoke(cred_cli.cred_group, ["check", "--agent", "ace", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output.splitlines()[0])["allowed"] is True


def test_no_agent_is_a_usage_error(web):
    r = CliRunner().invoke(cred_cli.cred_group, ["check"])
    assert r.exit_code == cred_cli.EXIT_USAGE
    assert "this session is not an agent" in r.output


def test_default_agent_is_the_sessions(monkeypatch, web):
    monkeypatch.setenv("CANOPY_AGENT", "ace")
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    r = CliRunner().invoke(cred_cli.cred_group, ["check"])
    assert r.exit_code == 0 and "'ace'" in r.output


# ── env ───────────────────────────────────────────────────────────────────────

def test_env_refused_never_writes(web, _isolated):
    with pytest.raises(cred_cli.CredError) as e:
        cred_cli.ensure_env("ace")
    assert e.value.exit_code == cred_cli.EXIT_REFUSED
    assert not (_isolated / ".ace" / ".env").exists()


def test_env_writes_values_but_never_prints_them(web, _isolated):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    web["resolve"]["ace"] = {"values": {"SETTING_A": RESOLVED, "gog-token": "{json}"},
                             "op_sa_token": "opsx-marker"}
    r = CliRunner().invoke(cred_cli.cred_group, ["env", "--agent", "ace"])
    assert r.exit_code == 0, r.output
    assert RESOLVED not in r.output and "opsx-marker" not in r.output
    path = _isolated / ".ace" / ".env"
    assert str(path) in r.output
    text = path.read_text()
    assert f"SETTING_A={RESOLVED}" in text and "gog-token" not in text.split("\n", 1)[1]
    assert "opsx-marker" not in text
    assert (path.stat().st_mode & 0o777) == 0o600


def test_env_is_resolved_once_then_present_until_refreshed(web, _isolated):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    web["resolve"]["ace"] = {"values": {"SETTING_A": RESOLVED}}
    path, how = cred_cli.ensure_env("ace")
    assert path == _isolated / ".ace" / ".env" and how.startswith("resolved from canopy-web")
    _, how2 = cred_cli.ensure_env("ace")
    assert how2 == "present"
    resolves = lambda: len([c for c in web["calls"] if "/credentials/resolve" in c[1]])  # noqa: E731
    assert resolves() == 1
    cred_cli.ensure_env("ace", refresh=True)
    assert resolves() == 2


def test_a_turn_resolves_its_own_agents_env(monkeypatch, web, _isolated):
    monkeypatch.setenv("CANOPY_TURN_ID", "t-1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "ace")
    web["resolve"]["ace"] = {"values": {"SETTING_A": RESOLVED}}
    path, how = cred_cli.ensure_env("ace")
    assert RESOLVED in path.read_text() and how.startswith("resolved from canopy-web")


# ── whoami ────────────────────────────────────────────────────────────────────

def test_whoami_human_and_turn(monkeypatch):
    r = CliRunner().invoke(cred_cli.cred_group, ["whoami"])
    assert "identity: human" in r.output and "why:" in r.output
    monkeypatch.setenv("CANOPY_TURN_ID", "t-9")
    monkeypatch.setenv("CANOPY_AGENT", "eva")
    r = CliRunner().invoke(cred_cli.cred_group, ["whoami", "--json"])
    assert json.loads(r.output)["agent"] == "eva"


def test_non_member_404_says_get_invited(web):
    web["fail"] = True  # fake raises "-> 404"
    v = cred_cli.decide("ace")
    assert not v.allowed and "not a member of its workspace" in v.message


# ── check: humans, an env already resolved on this machine ────────────────────

def _env_file(home, slug, mode=0o600, body="SETTING=x\n"):
    path = home / f".{slug}" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return path


def test_human_with_a_private_resolved_env_never_asks_canopy_web(web, _isolated):
    """The one-time resolve is enough: an unreachable canopy-web must not refuse the
    human who already resolved this agent's env here (Jonathan, 2026-10-09)."""
    _env_file(_isolated, "ace")
    v = cred_cli.decide("ace")
    assert v.allowed and v.via == "local-env" and web["calls"] == []


def test_a_world_readable_or_empty_env_does_not_count(web, _isolated):
    _env_file(_isolated, "ace", mode=0o644)
    assert not cred_cli.decide("ace").allowed
    _env_file(_isolated, "ace", body="")
    assert not cred_cli.decide("ace").allowed


def test_refresh_re_proves_access_despite_a_resolved_env(web, _isolated):
    _env_file(_isolated, "ace")
    v = cred_cli.decide("ace", refresh=True)
    assert not v.allowed and v.via is None


def test_a_turn_is_never_let_in_by_another_agents_env(monkeypatch, web, _isolated):
    _env_file(_isolated, "ace")
    monkeypatch.setenv("CANOPY_TURN_ID", "t-1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "ada")
    assert not cred_cli.decide("ace").allowed


# ── cred sync: keep ~/.<slug>/.env current with the vault, cheaply ─────────────

@pytest.fixture()
def synced(monkeypatch, _isolated):
    return _isolated / ".ace" / ".env"


def test_sync_refreshes_a_canopy_vault_agent(synced, web):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    web["resolve"]["ace"] = {"values": {"SETTING_A": "v1"}}
    assert cred_cli.sync_env("ace", now=1000) == "re-injected from canopy-web"
    assert "SETTING_A=v1" in synced.read_text() and oct(synced.stat().st_mode)[-3:] == "600"
    assert cred_cli.sync_env("ace", now=1100) == "skipped: checked recently"
    assert cred_cli.sync_env("ace", now=2000) == "unchanged"
    web["resolve"]["ace"] = {"values": {"SETTING_A": "v2"}}
    assert cred_cli.sync_env("ace", now=3000) == "re-injected from canopy-web"
    assert "SETTING_A=v2" in synced.read_text()


def test_sync_skips_when_canopy_web_will_not_resolve(synced, web):
    assert cred_cli.sync_env("ace", now=1000).startswith("skipped: canopy-web will not resolve")
    assert not any("/credentials/resolve" in c[1] for c in web["calls"]) and not synced.exists()


def test_sync_failure_keeps_the_file_and_throttles(synced, web, monkeypatch):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    synced.parent.mkdir(parents=True, exist_ok=True)
    synced.write_text("SETTING=old\n")

    def boom(slug, refs=None):
        raise cred_cli.CredError("could not resolve 'ace's credentials from canopy-web: 503")
    monkeypatch.setattr(cred_cli, "fetch_canopy_web_values", boom)
    with pytest.raises(cred_cli.CredError, match="503"):
        cred_cli.sync_env("ace", now=1000)
    assert synced.read_text() == "SETTING=old\n"
    assert cred_cli.sync_env("ace", now=1100) == "skipped: checked recently"


def test_sync_cli_never_prints_a_value(synced, web):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    web["resolve"]["ace"] = {"values": {"SETTING_A": "hunter2"}}
    res = CliRunner().invoke(cred_cli.cred_group, ["sync", "--agent", "ace"])
    assert res.exit_code == 0 and "re-injected" in res.output and "hunter2" not in res.output


# ──────────────────────────────────────────────────────────────────────────────
# google-token: the agent's own Google login, as a short-lived token
# ──────────────────────────────────────────────────────────────────────────────

ACCESS = "ya29.access-never-printed"
REFRESH = "1//refresh-never-printed"


@pytest.fixture()
def google(monkeypatch, tmp_path):
    """Fake gog (token export), gog client credentials, agent identity and Google."""
    from orchestrator import agent_email

    creds = tmp_path / "credentials-canopy.json"
    creds.write_text(json.dumps({"client_id": "cid", "client_secret": "csec"}))
    monkeypatch.setattr(agent_email, "gog_client_credentials", lambda home, client: str(creds))
    monkeypatch.setattr(agent_email, "find_agent_repo", lambda slug: tmp_path)
    monkeypatch.setattr(agent_email, "resolve_email_identity",
                        lambda repo: agent_email.EmailIdentity("eva", "eva@dimagi-ai.com", "canopy"))
    monkeypatch.setattr(agent_email, "reconcile_client",
                        lambda ident, **kw: agent_email.ClientReconciliation(
                            account=ident.account, declared="canopy", client="canopy"))
    for var in ("CANOPY_TURN_ID", "CANOPY_AGENT_SLUG", "CANOPY_AGENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cred_cli, "_turn_id", lambda: ("", ""))
    state = {"gog": [], "posts": [], "exported": [], "gog_rc": 0}

    def gog(argv, **kw):
        state["gog"].append(argv)
        out = argv[argv.index("--out") + 1]
        state["exported"].append(out)
        if state["gog_rc"]:
            return subprocess.CompletedProcess(argv, 1, "", "no token for eva@dimagi-ai.com")
        Path(out).write_text(json.dumps({"refresh_token": REFRESH}))
        return subprocess.CompletedProcess(argv, 0, "", "")

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return self.body

    def urlopen(url, data=None, timeout=None):
        state["posts"].append((url, data))
        return Resp(json.dumps({"access_token": ACCESS, "expires_in": 3599,
                                "scope": "drive"}).encode())

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    state["runner"] = gog
    return state


def test_google_token_mints_a_private_file_and_drops_the_refresh_token(google):
    path = cred_cli.google_token("eva", runner=google["runner"], now=1000.0)
    data = json.loads(path.read_text())
    assert data["access_token"] == ACCESS and data["account"] == "eva@dimagi-ai.com"
    assert data["expires_at"] == 1000.0 + 3599
    assert REFRESH not in path.read_text()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not os.path.exists(google["exported"][0])  # the exported refresh token is gone
    assert b"refresh_token=" in google["posts"][0][1]


def test_google_token_reuses_a_token_with_life_left(google):
    cred_cli.google_token("eva", runner=google["runner"], now=1000.0)
    cred_cli.google_token("eva", runner=google["runner"], now=1000.0 + 3000)
    assert len(google["posts"]) == 1
    cred_cli.google_token("eva", runner=google["runner"], now=1000.0 + 3400)  # < 5 min left
    assert len(google["posts"]) == 2


def test_google_token_refuses_a_turn_for_another_agent(google, monkeypatch):
    monkeypatch.setattr(cred_cli, "_turn_id", lambda: ("t-1", "$CANOPY_TURN_ID"))
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "ada")
    with pytest.raises(cred_cli.CredError) as e:
        cred_cli.google_token("eva", runner=google["runner"])
    assert e.value.exit_code == cred_cli.EXIT_REFUSED
    assert google["gog"] == [] and google["posts"] == []


def test_google_token_names_the_login_to_fix_when_gog_has_none(google):
    google["gog_rc"] = 1
    with pytest.raises(cred_cli.CredError) as e:
        cred_cli.google_token("eva", runner=google["runner"])
    assert "gog login eva@dimagi-ai.com --client canopy" in str(e.value)
    assert google["posts"] == []


def test_google_token_cli_prints_the_path_never_the_token(google, monkeypatch):
    real = cred_cli.google_token
    monkeypatch.setattr(cred_cli, "google_token",
                        lambda slug: real(slug, runner=google["runner"]))
    res = CliRunner().invoke(cred_cli.cred_group, ["google-token", "--agent", "eva"])
    assert res.exit_code == 0, res.output
    assert ACCESS not in res.output and res.output.strip().endswith("google-token/eva.json")


# ── exec: the agent's env in the CHILD only (canopy#946) ──────────────────────

_CHILD = [sys.executable, "-c",
          "import os,sys; v=os.environ.get('SETTING_A'); "
          "sys.exit(0 if v == sys.argv[1] else 7)", RESOLVED]


def test_exec_from_canopy_web_reaches_the_child_and_nothing_else(web, _isolated):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    web["resolve"]["ace"] = {"values": {"SETTING_A": RESOLVED, "gog-token": "{json}"}}
    r = CliRunner().invoke(cred_cli.cred_group, ["exec", "--agent", "ace", "--", *_CHILD])
    assert r.exit_code == 0, r.output
    assert RESOLVED not in r.output
    assert not (_isolated / ".ace").exists()                 # no ~/.ace/.env written
    assert "SETTING_A" not in os.environ                      # nor this process's env


def test_exec_ignores_an_already_resolved_env_file_for_the_decision(web, _isolated):
    _env_file(_isolated, "ace")   # a resolved file must not stand in for access
    web["access"]["ace"] = {"may_resolve": False,
                            "reason": "not an admin"}
    r = CliRunner().invoke(cred_cli.cred_group, ["exec", "--agent", "ace", "--", "true"])
    assert r.exit_code == cred_cli.EXIT_REFUSED


def test_exec_refuses_another_agent_in_a_runner_turn(monkeypatch, web):
    monkeypatch.setenv("CANOPY_TURN_ID", "t-1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "ada")
    r = CliRunner().invoke(cred_cli.cred_group, ["exec", "--agent", "ace", "--", "true"])
    assert r.exit_code == cred_cli.EXIT_REFUSED
    assert web["resolve"] == {} and not any("resolve" in c[1] for c in web["calls"])


def test_exec_canopy_web_refs_are_sent_to_the_server(web, _isolated):
    web["access"]["ace"] = {"may_resolve": True, "via": "admin"}
    web["resolve"]["ace"] = {"values": {"SETTING_A": RESOLVED}}
    values, _ = cred_cli.exec_env("ace", refs=["SETTING_A"])
    assert values == {"SETTING_A": RESOLVED}
    assert any(c[1].endswith("/credentials/resolve?refs=SETTING_A") for c in web["calls"])




# ── connection-token (salesforce-token is its alias; these drive the alias) ──────

SF_TOKEN = "00Dxx!sf-access-token-never-printed"
RID, TID = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


@pytest.fixture()
def sf(monkeypatch):
    """Fake canopy-web at the `canopy_web.call` boundary; `reply` is a body or a CanopyError."""
    import datetime as dt
    state = {"calls": [], "reply": None}

    def default():
        exp = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()
        return {"access_token": SF_TOKEN, "instance_url": "https://dimagi.my.salesforce.com",
                "expires_at": exp, "account_label": "jj@dimagi.com"}

    def fake_call(method, path, body=None, **kw):
        state["calls"].append({"method": method, "path": path, "body": body, "token": kw.get("token")})
        reply = state["reply"] if state["reply"] is not None else default()
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(cred_cli.canopy_web, "call", fake_call)
    monkeypatch.delenv("CANOPY_TURN_ID", raising=False)
    monkeypatch.delenv("CANOPY_AGENT_SLUG", raising=False)
    monkeypatch.delenv("CANOPY_AGENT", raising=False)
    return state


def _conflict(error, **extra):
    body = {"title": "Not connected", "detail": "Your Salesforce is not connected.", "error": error, **extra}
    return cred_cli.canopy_web.CanopyError(f"POST /x -> 409: {json.dumps(body)}")


def _sf(*args, env=None):
    return CliRunner().invoke(cred_cli.cred_group, ["salesforce-token", *args], env=env)


def _assert_no_token(res):
    assert SF_TOKEN not in res.output
    assert SF_TOKEN not in (getattr(res, "stderr", "") or "")


def test_sf_human_writes_a_private_file_and_prints_only_its_path(sf, tmp_path):
    res = _sf()
    path = tmp_path / "home" / ".canopy" / "connections" / "salesforce.json"
    assert res.exit_code == 0, res.output
    assert res.output.strip() == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    staged = json.loads(path.read_text())
    assert staged["access_token"] == SF_TOKEN and staged["account_label"] == "jj@dimagi.com"
    assert staged["instance_url"] == "https://dimagi.my.salesforce.com"
    assert isinstance(staged["expires_at"], (int, float))
    assert sf["calls"] == [{"method": "POST", "path": "/api/me/connections/salesforce/token",
                            "body": None, "token": "pat_human"}]
    _assert_no_token(res)


def test_sf_human_reuses_the_file_until_five_minutes_before_expiry(sf, tmp_path):
    _sf()
    _sf()
    assert len(sf["calls"]) == 1
    path = tmp_path / "home" / ".canopy" / "connections" / "salesforce.json"
    staged = json.loads(path.read_text())
    staged["expires_at"] = time.time() + 120      # inside the 5 minute margin
    path.write_text(json.dumps(staged))
    _sf()
    assert len(sf["calls"]) == 2


def test_sf_human_409_prints_the_connect_url_and_fails(sf):
    # The server's real shape: an absolute URL to the bare start view.
    sf["reply"] = _conflict("not_connected",
                            connect_url="https://canopy.dimagi.com/auth/connections/salesforce/start/")
    res = _sf()
    assert res.exit_code != 0
    assert "Connect it in canopy-web: https://canopy.dimagi.com/auth/connections/salesforce/start/" in res.output
    assert "Your Salesforce is not connected." in res.output
    _assert_no_token(res)


def test_sf_runner_stages_under_the_agent_dir_via_the_runner_route(sf, tmp_path):
    res = _sf("--runner", "--agent", "eva", "--runner-id", RID, "--turn", TID, env={"CANOPY_TOKEN": "crt_runner"})
    path = tmp_path / "home" / ".canopy" / "connections" / "eva" / "salesforce.json"
    assert res.exit_code == 0, res.output
    assert res.output.strip() == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["access_token"] == SF_TOKEN
    assert sf["calls"] == [{"method": "POST",
                            "path": f"/api/harness/runners/{RID}/turns/{TID}/connection-token",
                            "body": {"provider": "salesforce"}, "token": "crt_runner"}]
    _assert_no_token(res)


def test_sf_runner_not_connected_exits_3_and_stages_nothing(sf, tmp_path):
    sf["reply"] = _conflict("not_connected")
    res = _sf("--runner", "--agent", "eva", "--runner-id", RID, "--turn", TID, env={"CANOPY_TOKEN": "crt_runner"})
    assert res.exit_code == 3
    assert not (tmp_path / "home" / ".canopy" / "connections" / "eva" / "salesforce.json").exists()


def test_sf_runner_other_refusals_are_nonzero_with_the_detail(sf):
    sf["reply"] = _conflict("reconnect_required",
                            detail="eva's Salesforce connection no longer works; connect it again.")
    res = _sf("--runner", "--agent", "eva", "--runner-id", RID, "--turn", TID, env={"CANOPY_TOKEN": "crt_runner"})
    assert res.exit_code not in (0, 3)
    assert "no longer works" in res.output


def test_sf_runner_needs_its_token_ids_and_agent(sf):
    res = _sf("--runner", "--agent", "eva", "--runner-id", RID, "--turn", TID, env={"CANOPY_TOKEN": ""})
    assert res.exit_code == cred_cli.EXIT_USAGE and not sf["calls"]
    res = _sf("--runner", "--agent", "eva", env={"CANOPY_TOKEN": "crt_runner"})
    assert res.exit_code == cred_cli.EXIT_USAGE and not sf["calls"]


def _stage(tmp_path, slug, expires_in):
    p = tmp_path / "home" / ".canopy" / "connections" / slug / "salesforce.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"access_token": SF_TOKEN, "instance_url": "https://x.my.salesforce.com",
                             "expires_at": time.time() + expires_in, "account_label": "a"}))
    return p


def test_sf_turn_prints_the_staged_path_without_the_network(sf, tmp_path):
    p = _stage(tmp_path, "eva", 1800)
    res = _sf("--agent", "eva")
    assert res.exit_code == 0 and res.output.strip() == str(p)
    assert sf["calls"] == []
    _assert_no_token(res)


def test_sf_turn_errors_when_nothing_is_staged(sf):
    res = _sf("--agent", "eva")
    assert res.exit_code != 0 and sf["calls"] == []
    assert "no salesforce token staged for eva" in res.output


def test_sf_turn_errors_when_the_staged_token_is_about_to_lapse(sf, tmp_path):
    _stage(tmp_path, "eva", 120)
    res = _sf("--agent", "eva")
    assert res.exit_code != 0 and sf["calls"] == []
    _assert_no_token(res)


def test_sf_turn_refuses_another_agents_token(sf, tmp_path, monkeypatch):
    _stage(tmp_path, "hal", 1800)
    monkeypatch.setenv("CANOPY_TURN_ID", "t1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "eva")
    res = _sf("--agent", "hal")
    assert res.exit_code == cred_cli.EXIT_REFUSED


def test_sf_human_path_is_refused_inside_an_agent_turn(sf, tmp_path, monkeypatch):
    human = tmp_path / "home" / ".canopy" / "connections" / "salesforce.json"
    human.parent.mkdir(parents=True)
    human.write_text(json.dumps({"access_token": SF_TOKEN, "instance_url": "x",
                                 "expires_at": time.time() + 1800, "account_label": "a"}))
    monkeypatch.setenv("CANOPY_TURN_ID", "t1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "eva")
    res = _sf()
    assert res.exit_code == cred_cli.EXIT_REFUSED
    assert "--agent eva" in res.output
    assert sf["calls"] == []
    _assert_no_token(res)


# ── connection-token: --refresh and --min-life ───────────────────────────────

def test_sf_human_refresh_skips_the_file_and_asks_the_server_to_refresh(sf, tmp_path):
    _sf()
    assert len(sf["calls"]) == 1
    res = _sf("--refresh")
    assert res.exit_code == 0, res.output
    assert sf["calls"][1]["path"] == "/api/me/connections/salesforce/token?refresh=true"
    _assert_no_token(res)


def test_sf_runner_forwards_min_life_and_refresh(sf, tmp_path):
    res = _sf("--runner", "--agent", "eva", "--runner-id", RID, "--turn", TID, "--min-life", "900", "--refresh",
              env={"CANOPY_TOKEN": "crt_runner"})
    assert res.exit_code == 0, res.output
    assert sf["calls"][0]["body"] == {"provider": "salesforce", "min_life_seconds": 900, "refresh": True}


def test_sf_min_life_and_refresh_belong_to_the_server_modes(sf, tmp_path):
    _stage(tmp_path, "eva", 1800)
    assert _sf("--agent", "eva", "--refresh").exit_code == cred_cli.EXIT_USAGE
    assert _sf("--min-life", "900").exit_code == cred_cli.EXIT_USAGE
    assert sf["calls"] == []


# ── connection-token: any provider, by the same paths and routes ──────────────

def _ct(*args, env=None):
    return CliRunner().invoke(cred_cli.cred_group, ["connection-token", *args], env=env)


def test_connection_token_names_its_files_and_routes_by_provider(sf, tmp_path):
    res = _ct("intacct")
    person = tmp_path / "home" / ".canopy" / "connections" / "intacct.json"
    assert res.exit_code == 0, res.output
    assert res.output.strip() == str(person)
    assert sf["calls"][-1]["path"] == "/api/me/connections/intacct/token"
    res = _ct("intacct", "--runner", "--agent", "eva", "--runner-id", RID, "--turn", TID,
              env={"CANOPY_TOKEN": "crt_runner"})
    staged = tmp_path / "home" / ".canopy" / "connections" / "eva" / "intacct.json"
    assert res.exit_code == 0 and res.output.strip() == str(staged)
    assert sf["calls"][-1]["body"] == {"provider": "intacct"}
    res = _ct("intacct", "--agent", "eva")
    assert res.exit_code == 0 and res.output.strip() == str(staged)
    _assert_no_token(res)


def test_connection_token_without_instance_url_is_fine(sf, tmp_path):
    """A provider with a fixed API host (Google) sends an empty `instance_url`."""
    sf["reply"] = {"access_token": SF_TOKEN, "instance_url": "", "expires_at": time.time() + 3600,
                   "account_label": "jj@dimagi.com"}
    res = _ct("gmail")
    assert res.exit_code == 0, res.output
    assert json.loads((tmp_path / "home" / ".canopy" / "connections" / "gmail.json").read_text())["instance_url"] == ""


@pytest.mark.parametrize("bad", ["../x", "Sales Force", "", "a/b", "x" * 41])
def test_connection_token_refuses_a_key_that_is_not_a_provider(sf, bad):
    res = _ct(bad)
    assert res.exit_code == cred_cli.EXIT_USAGE and sf["calls"] == []


def test_salesforce_token_is_connection_token_salesforce(sf, tmp_path):
    assert _sf().output == _ct("salesforce").output


def test_connection_token_inside_a_turn_points_at_the_agents_own(sf, monkeypatch):
    monkeypatch.setenv("CANOPY_TURN_ID", "t1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "eva")
    res = _ct("intacct")
    assert res.exit_code == cred_cli.EXIT_REFUSED
    assert "connection-token intacct --agent eva" in res.output and sf["calls"] == []


# ── connection-token: --workspace ─────────────────────────────────────────────

def test_connection_token_workspace_is_sent_as_a_query_param_and_keys_the_cache(sf, tmp_path):
    res = _ct("salesforce", "--workspace", "dimagi")
    base = tmp_path / "home" / ".canopy" / "connections"
    assert res.exit_code == 0, res.output
    assert res.output.strip() == str(base / "salesforce@dimagi.json")
    assert sf["calls"][-1]["path"] == "/api/me/connections/salesforce/token?workspace=dimagi"
    _ct("salesforce", "--workspace", "dimagi")
    assert len(sf["calls"]) == 1                      # reused from its own file
    _ct("salesforce", "--workspace", "other-ws", "--refresh")
    assert sf["calls"][-1]["path"] == "/api/me/connections/salesforce/token?refresh=true&workspace=other-ws"
    _ct("salesforce")
    assert (base / "salesforce.json").exists() and (base / "salesforce@other-ws.json").exists()
    assert sf["calls"][-1]["path"] == "/api/me/connections/salesforce/token"


def test_salesforce_token_alias_takes_workspace(sf):
    assert _sf("--workspace", "dimagi").exit_code == 0
    assert sf["calls"][-1]["path"].endswith("?workspace=dimagi")


def test_connection_token_ambiguous_names_the_workspaces_and_the_flag(sf):
    sf["reply"] = _conflict("ambiguous", workspaces=["dimagi", "ace"],
                            connect_url="https://canopy.dimagi.com/auth/connections/salesforce/start/")
    res = _ct("salesforce")
    assert res.exit_code == 1
    assert "dimagi, ace" in res.output
    assert "canopy cred connection-token salesforce --workspace dimagi" in res.output
    _assert_no_token(res)


def test_connection_token_workspace_is_refused_for_agent_scoped_modes(sf, tmp_path):
    res = _ct("salesforce", "--workspace", "dimagi", "--runner", "--agent", "eva", "--runner-id", RID,
              "--turn", TID, env={"CANOPY_TOKEN": "crt_runner"})
    assert res.exit_code == cred_cli.EXIT_USAGE and "--workspace" in res.output
    _stage(tmp_path, "eva", 1800)
    res = _ct("salesforce", "--workspace", "dimagi", "--agent", "eva")
    assert res.exit_code == cred_cli.EXIT_USAGE and "--workspace" in res.output
    assert sf["calls"] == []


@pytest.mark.parametrize("bad", ["../x", "A B", "a/b", "x" * 65])
def test_connection_token_refuses_a_workspace_that_is_not_a_slug(sf, bad):
    res = _ct("salesforce", "--workspace", bad)
    assert res.exit_code == cred_cli.EXIT_USAGE and sf["calls"] == []
