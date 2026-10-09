"""`canopy cred` — session identity (canopy#850 design revision). op + canopy-web mocked."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import cred_cli

SECRET = "s3cr3t-value-never-printed"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fake home (cache + ~/.<slug>/.env), no ambient op key, cwd outside any repo."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cred_cli.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cred_cli, "CACHE_DIR", home / ".canopy")
    monkeypatch.setattr(cred_cli, "CACHE_FILE", home / ".canopy" / "cred-cache.json")
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
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
            return state["access"].get(slug, {"agent": slug, "credential_source": "1password",
                                              "may_resolve": False, "via": None, "reason": ""})
        if path.endswith("/credentials/resolve"):
            return state["resolve"].get(slug, {"values": {}})
        raise AssertionError(path)

    monkeypatch.setattr(cred_cli.canopy_web, "call", fake_call)
    return state


class FakeOp:
    """Records op invocations; `readable` = vaults the USER's op can read."""

    def __init__(self, readable=(), sa_readable=(), signed_in=True):
        self.readable, self.sa_readable, self.signed_in = set(readable), set(sa_readable), signed_in
        self.calls = []

    def __call__(self, argv, **kw):
        env = kw.get("env") or {}
        sa = "OP_SERVICE_ACCOUNT_TOKEN" in env
        self.calls.append((argv, sa))
        if argv[:3] == ["op", "vault", "get"]:
            vault = argv[3]
            if not sa and not self.signed_in:
                return subprocess.CompletedProcess(argv, 1, "", "[ERROR] You are not currently signed in.")
            ok = vault in (self.sa_readable if sa else self.readable)
            return subprocess.CompletedProcess(argv, 0 if ok else 1, "{}" if ok else "",
                                               "" if ok else f'[ERROR] "{vault}" isn\'t a vault in this account.')
        if argv[:2] == ["op", "inject"]:
            out = argv[argv.index("-o") + 1]
            Path(out).write_text(f"TOKEN={SECRET}\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


def _which(present=True):
    return lambda name: "/usr/bin/op" if present else None


def _agent_repo(tmp_path, slug, tpl=True, op_vault=None):
    repo = tmp_path / "repos" / slug
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    (repo / "config").mkdir()
    (repo / "config" / "agent.json").write_text(json.dumps({"op_vault": op_vault} if op_vault else {}))
    if tpl:
        (repo / ".env.tpl").write_text("TOKEN=op://Agent-X/item/credential\n")
    return repo


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
    v = cred_cli.decide("ace", runner=FakeOp(), which=_which())
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
    v = cred_cli.decide("ace", runner=FakeOp(), which=_which())
    assert not v.allowed and v.exit_code == cred_cli.EXIT_REFUSED


# ── check: humans, 1Password backend ──────────────────────────────────────────

def test_human_with_vault_access_may_act(web):
    op = FakeOp(readable={"Agent-Ace"})
    v = cred_cli.decide("ace", runner=op, which=_which())
    assert v.allowed and v.via == "1password" and v.vault == "Agent-Ace"
    assert op.calls[0][0] == ["op", "vault", "get", "Agent-Ace", "--format", "json"]


def test_vault_name_comes_from_agent_json_when_declared(tmp_path, monkeypatch, web):
    repo = _agent_repo(tmp_path, "ace", op_vault="ACE")
    monkeypatch.chdir(repo)
    v = cred_cli.decide("ace", runner=FakeOp(readable={"ACE"}), which=_which())
    assert v.allowed and v.vault == "ACE"


def test_op_missing_says_install_and_signin(web):
    v = cred_cli.decide("ace", runner=FakeOp(), which=_which(False))
    assert not v.allowed
    assert "install the 1Password CLI".lower() in v.message.lower()
    assert "op signin" in v.message and "share 'Agent-Ace'" in v.message


def test_op_not_signed_in_says_signin(web):
    v = cred_cli.decide("ace", runner=FakeOp(signed_in=False), which=_which())
    assert not v.allowed and v.reason == "1password: signin" and "op signin" in v.message


def test_no_vault_access_says_ask_the_owner(web):
    v = cred_cli.decide("hal", runner=FakeOp(readable={"Agent-Ace"}), which=_which())
    assert not v.allowed and "Ask the vault owner to share 'Agent-Hal'" in v.message


def test_users_op_never_borrows_a_staged_agent_key_first(monkeypatch, web):
    """A hook may have staged ACE's service-account key into this session; the probe
    asks the user's own op first, and the key only reads ACE's own vault."""
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_ace")
    op = FakeOp(readable=set(), sa_readable={"Agent-Ace"})
    assert not cred_cli.decide("hal", runner=op, which=_which()).allowed
    assert op.calls[0][1] is False  # user first
    v = cred_cli.decide("ace", runner=op, which=_which())
    assert v.allowed and v.op_mode == "service-account"


def test_positive_op_verdict_is_cached(web):
    op = FakeOp(readable={"Agent-Ace"})
    cred_cli.decide("ace", runner=op, which=_which())
    cred_cli.decide("ace", runner=op, which=_which())
    assert len(op.calls) == 1
    cred_cli.decide("ace", refresh=True, runner=op, which=_which())
    assert len(op.calls) == 2


def test_canopy_web_unreachable_falls_back_to_1password(web):
    web["fail"] = True
    v = cred_cli.decide("ace", runner=FakeOp(), which=_which())
    assert not v.allowed and v.source == "1password" and v.notes


# ── check: humans, canopy-web backend ─────────────────────────────────────────

def test_canopy_web_backend_follows_may_resolve(web):
    web["access"]["ace"] = {"credential_source": "canopy-web", "may_resolve": True,
                            "via": "admin", "reason": "you are an admin of ace"}
    v = cred_cli.decide("ace", runner=FakeOp(), which=_which(False))
    assert v.allowed and v.via == "admin"


def test_canopy_web_backend_refusal_names_the_admin_path(web):
    web["access"]["ace"] = {"credential_source": "canopy-web", "may_resolve": False,
                            "via": None, "reason": "not an owner or admin"}
    r = CliRunner().invoke(cred_cli.cred_group, ["check", "--agent", "ace"])
    assert r.exit_code == cred_cli.EXIT_REFUSED
    assert "not an owner or admin" in r.output and "admin of 'ace'" in r.output


def test_access_is_cached(web):
    web["access"]["ace"] = {"credential_source": "canopy-web", "may_resolve": True, "via": "admin"}
    cred_cli.fetch_access("ace")
    cred_cli.fetch_access("ace")
    assert len(web["calls"]) == 1
    assert (cred_cli.CACHE_FILE.stat().st_mode & 0o777) == 0o600


def test_check_json_and_exit_zero(monkeypatch, web):
    monkeypatch.setattr(cred_cli, "probe_op",
                        lambda vault, **kw: cred_cli.OpProbe(True, mode="user"))
    r = CliRunner().invoke(cred_cli.cred_group, ["check", "--agent", "ace", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output.splitlines()[0])["allowed"] is True


def test_no_agent_is_a_usage_error(web):
    r = CliRunner().invoke(cred_cli.cred_group, ["check"])
    assert r.exit_code == cred_cli.EXIT_USAGE
    assert "this session is not an agent" in r.output


def test_default_agent_is_the_sessions(monkeypatch, web):
    monkeypatch.setenv("CANOPY_AGENT", "ace")
    monkeypatch.setattr(cred_cli, "probe_op",
                        lambda vault, **kw: cred_cli.OpProbe(True, mode="user"))
    r = CliRunner().invoke(cred_cli.cred_group, ["check"])
    assert r.exit_code == 0 and "'ace'" in r.output


# ── env ───────────────────────────────────────────────────────────────────────

def test_env_injects_from_the_agent_repo_once(tmp_path, monkeypatch, web, _isolated):
    repo = _agent_repo(tmp_path, "ace")
    monkeypatch.chdir(repo)
    op = FakeOp(readable={"Agent-Ace"})
    path, how = cred_cli.ensure_env("ace", runner=op, which=_which())
    assert path == _isolated / ".ace" / ".env"
    assert (path.stat().st_mode & 0o777) == 0o600 and SECRET in path.read_text()
    assert "1Password" in how
    injects = [c for c in op.calls if c[0][:2] == ["op", "inject"]]
    assert injects[0][0][:5] == ["op", "inject", "-f", "-i", str(repo / ".env.tpl")]
    # present → no second resolve
    _, how2 = cred_cli.ensure_env("ace", runner=op, which=_which())
    assert how2 == "present" and len([c for c in op.calls if c[0][1] == "inject"]) == 1
    # --refresh → resolves again
    cred_cli.ensure_env("ace", refresh=True, runner=op, which=_which())
    assert len([c for c in op.calls if c[0][1] == "inject"]) == 2


def test_env_refused_never_writes(web, _isolated):
    with pytest.raises(cred_cli.CredError) as e:
        cred_cli.ensure_env("ace", runner=FakeOp(), which=_which(False))
    assert e.value.exit_code == cred_cli.EXIT_REFUSED
    assert not (_isolated / ".ace" / ".env").exists()


def test_env_from_canopy_web_writes_values_but_never_prints_them(web, _isolated):
    web["access"]["ace"] = {"credential_source": "canopy-web", "may_resolve": True, "via": "admin"}
    web["resolve"]["ace"] = {"values": {"API_KEY": SECRET, "gog-token": "{json}"},
                             "op_sa_token": "ops_x"}
    r = CliRunner().invoke(cred_cli.cred_group, ["env", "--agent", "ace"])
    assert r.exit_code == 0, r.output
    assert SECRET not in r.output and "ops_x" not in r.output
    path = _isolated / ".ace" / ".env"
    assert str(path) in r.output
    text = path.read_text()
    assert f"API_KEY={SECRET}" in text and "gog-token" not in text.split("\n", 1)[1]
    assert (path.stat().st_mode & 0o777) == 0o600


def test_turn_env_uses_its_service_account_key(tmp_path, monkeypatch, web):
    repo = _agent_repo(tmp_path, "ace")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CANOPY_TURN_ID", "t-1")
    monkeypatch.setenv("CANOPY_AGENT_SLUG", "ace")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_ace")
    op = FakeOp()
    cred_cli.ensure_env("ace", runner=op, which=_which())
    inject = [c for c in op.calls if c[0][1] == "inject"][0]
    assert inject[1] is True  # SA key kept in op's env


def test_env_without_a_repo_says_where_to_get_it(monkeypatch, web):
    monkeypatch.setattr(cred_cli, "_agent_repo", lambda slug: None)
    with pytest.raises(cred_cli.CredError) as e:
        cred_cli.ensure_env("ace", runner=FakeOp(readable={"Agent-Ace"}), which=_which())
    assert "gh repo clone dimagi-internal/ace" in str(e.value.message)


# ── whoami ────────────────────────────────────────────────────────────────────

def test_whoami_human_and_turn(monkeypatch):
    r = CliRunner().invoke(cred_cli.cred_group, ["whoami"])
    assert "identity: human" in r.output and "why:" in r.output
    monkeypatch.setenv("CANOPY_TURN_ID", "t-9")
    monkeypatch.setenv("CANOPY_AGENT", "eva")
    r = CliRunner().invoke(cred_cli.cred_group, ["whoami", "--json"])
    assert json.loads(r.output)["agent"] == "eva"
