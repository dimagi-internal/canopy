"""`canopy act-as` — an agent acts as its turn's asker after a passkey approval.

Mocked canopy-web throughout. What must never regress: the credential is stored 0600 in a
0700 dir and never printed; it is reused until it expires and refused after; `stop`
deletes it; the wait loop ends cleanly on declined / expired / still pending; an old
canopy-web (404) says so instead of a traceback.
"""
from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import act_as_cli

SECRET = "cat_SECRET-person-token-xyz"
BASE = "https://canopy.test"
ROOT = Path(__file__).parent.parent
PLUGIN = ROOT / "plugins" / "canopy"


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Server:
    """A fake canopy-web transport: a queue of answers per (method, path)."""

    def __init__(self, routes):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url[len(BASE):]
        self.calls.append((method, path, headers.get("Authorization"),
                           json.loads(body) if body else None))
        queue = self.routes.get((method, path))
        if not queue:
            return 404, '{"detail": "Not Found"}'
        status, doc = queue[0] if len(queue) == 1 else queue.pop(0)
        return status, doc if isinstance(doc, str) else json.dumps(doc)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CANOPY_WEB_API_URL", BASE)
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    monkeypatch.setattr(act_as_cli, "NOW", clock)
    monkeypatch.setattr(act_as_cli, "SLEEP", lambda s: setattr(clock, "t", clock.t + s))
    monkeypatch.setattr(act_as_cli, "agent_login", lambda: "agent-turn-login")
    monkeypatch.setattr(act_as_cli, "grant_key", lambda: "turn-1")
    monkeypatch.setattr(act_as_cli, "_claude_session", lambda: "claude-1")
    monkeypatch.setattr(act_as_cli.canopy_web, "agent_context_slug", lambda *a, **k: "ada")

    def install(routes):
        srv = Server(routes)
        monkeypatch.setattr(act_as_cli, "TRANSPORT", srv)
        return srv
    install.clock = clock
    install.home = tmp_path
    return install


def run(*args):
    return CliRunner().invoke(act_as_cli.act_as_group, list(args))


POST = ("POST", "/api/me/act-as-requests/")
GET = ("GET", "/api/me/act-as-requests/7")


def pending(clock):
    return 201, {"id": 7, "status": "pending", "scope": "read",
                 "approve_url": f"{BASE}/act-as/7/approve", "expires_at": _iso(clock.t + 600)}


def approved(clock, token=True, scope="read"):
    doc = {"id": 7, "status": "approved", "scope": scope, "expires_at": _iso(clock.t + 1800)}
    if token:
        doc["token"] = SECRET
    return 200, doc


def token_file(home: Path) -> Path:
    return home / ".canopy" / "act-as" / "turn-1.token"


def test_request_wait_stores_token_0600_and_never_prints_it(env):
    c = env.clock
    srv = env({POST: pending(c),
               GET: [(200, {"id": 7, "status": "pending", "scope": "read"}), approved(c)]})
    r = run("request", "--reason", "Read your session 4328 to see why it stalled")
    assert r.exit_code == 0, r.output
    assert f"Approve at: {BASE}/act-as/7/approve" in r.output
    assert "Approved." in r.output
    assert SECRET not in r.output
    # The request went out with the agent's turn login and the contract body.
    assert srv.calls[0] == ("POST", "/api/me/act-as-requests/", "Bearer agent-turn-login",
                            {"reason": "Read your session 4328 to see why it stalled",
                             "scope": "read"})
    f = token_file(env.home)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    doc = json.loads(f.read_text())
    assert doc["token"] == SECRET and doc["id"] == 7 and doc["scope"] == "read"
    assert doc["claude_session_id"] == "claude-1"
    # status never prints it either.
    s = run("status")
    assert s.exit_code == 0 and SECRET not in s.output and "approved" in s.output
    sj = run("status", "--json-output")
    assert SECRET not in sj.output and json.loads(sj.output)["credential"] is True


def test_token_is_reused_until_expiry_then_refused(env):
    c = env.clock
    srv = env({POST: pending(c), GET: approved(c),
               ("GET", "/api/sessions/4328/?view=full"): (200, {"id": 4328, "title": "x"})})
    assert run("request", "--reason", "Read your session 4328 please").exit_code == 0
    posts = [x for x in srv.calls if x[0] == "POST"]
    # A second request reuses the live grant: nothing new is asked.
    again = run("request", "--reason", "Read your session 4328 please")
    assert again.exit_code == 0 and "reusing" in again.output
    assert [x for x in srv.calls if x[0] == "POST"] == posts
    g = run("get", "/api/sessions/4328/", "--param", "view=full")
    assert g.exit_code == 0, g.output
    assert '"id": 4328' in g.output
    assert srv.calls[-1][2] == f"Bearer {SECRET}"
    # Past expires_at: ignored, deleted, refused — nothing sent.
    c.t += 1801
    n = len(srv.calls)
    late = run("get", "/api/sessions/4328/")
    assert late.exit_code != 0 and "no live act-as grant" in late.output
    assert len(srv.calls) == n
    assert not token_file(env.home).exists()


def test_stop_posts_and_deletes(env):
    c = env.clock
    srv = env({POST: pending(c), GET: approved(c),
               ("POST", "/api/me/act-as-requests/7/stop"): (200, {"id": 7, "status": "stopped"})})
    assert run("request", "--reason", "Read your session 4328 please").exit_code == 0
    r = run("stop")
    assert r.exit_code == 0 and "stopped act-as #7" in r.output
    assert srv.calls[-1][:3] == ("POST", "/api/me/act-as-requests/7/stop", "Bearer agent-turn-login")
    assert not token_file(env.home).exists()
    assert "no live act-as grant" in run("get", "/api/x/").output


def test_stop_deletes_even_when_server_refuses(env):
    c = env.clock
    env({POST: pending(c), GET: approved(c),
         ("POST", "/api/me/act-as-requests/7/stop"): (409, {"detail": "turn ended"})})
    assert run("request", "--reason", "Read your session 4328 please").exit_code == 0
    r = run("stop")
    assert r.exit_code == 0
    assert not token_file(env.home).exists()


@pytest.mark.parametrize("final", ["declined", "expired"])
def test_wait_ends_on_declined_or_expired(env, final):
    c = env.clock
    env({POST: pending(c), GET: (200, {"id": 7, "status": final, "scope": "read"})})
    r = run("request", "--reason", "Read your session 4328 please")
    assert r.exit_code == 1
    assert f"was {final}" in r.output and "do NOT act as them" in r.output
    assert not token_file(env.home).exists()


def test_still_pending_at_timeout_exits_4_and_status_resumes(env):
    c = env.clock
    srv = env({POST: pending(c), GET: (200, {"id": 7, "status": "pending", "scope": "read"})})
    r = run("request", "--reason", "Read your session 4328 please", "--timeout", "10")
    assert r.exit_code == act_as_cli.EXIT_STILL_PENDING, r.output
    assert "still pending" in r.output
    assert token_file(env.home).exists()  # kept, so `status --wait` can pick it up
    srv.routes[GET] = [approved(c)]
    s = run("status", "--wait")
    assert s.exit_code == 0 and "approved" in s.output and SECRET not in s.output
    assert len([x for x in srv.calls if x[0] == "POST"]) == 1


def test_no_wait_then_request_again_does_not_ask_twice(env):
    c = env.clock
    srv = env({POST: pending(c), GET: approved(c)})
    r = run("request", "--reason", "Read your session 4328 please", "--no-wait")
    assert r.exit_code == 0 and "Approve at:" in r.output
    r2 = run("request", "--reason", "Read your session 4328 please")
    assert r2.exit_code == 0 and "already waiting" in r2.output and "Approved." in r2.output
    assert len([x for x in srv.calls if x[0] == "POST"]) == 1


def test_old_canopy_web_404_says_so(env):
    env({})
    r = run("request", "--reason", "Read your session 4328 please")
    assert r.exit_code == act_as_cli.EXIT_NO_ACT_AS_API
    assert "doesn't support act-as yet" in r.output


def test_no_eligible_asker_403_is_verbatim(env):
    env({POST: (403, {"detail": "this turn has no verified human asker"})})
    r = run("request", "--reason", "Read your session 4328 please")
    assert r.exit_code == 1 and "this turn has no verified human asker" in r.output


def test_read_scope_refuses_writes_and_403_is_verbatim(env):
    c = env.clock
    srv = env({POST: pending(c), GET: approved(c),
               ("GET", "/api/admin/"): (403, {"detail": "act-as: route excluded for scope read"})})
    assert run("request", "--reason", "Read your session 4328 please").exit_code == 0
    n = len(srv.calls)
    w = run("call", "PATCH", "/api/sessions/4328/", "--json", '{"title": "x"}')
    assert w.exit_code == 1 and "read-only" in w.output and len(srv.calls) == n
    d = run("get", "/api/admin/")
    assert d.exit_code == 1 and "act-as: route excluded for scope read" in d.output


def test_write_scope_call_sends_body(env):
    c = env.clock
    srv = env({POST: pending(c), GET: approved(c, scope="write"),
               ("PATCH", "/api/sessions/4328/"): (200, {"ok": True})})
    assert run("request", "--reason", "Rename your session 4328 as asked",
               "--scope", "write").exit_code == 0
    r = run("call", "patch", "/api/sessions/4328/", "--json", '{"title": "x"}')
    assert r.exit_code == 0, r.output
    assert srv.calls[-1] == ("PATCH", "/api/sessions/4328/", f"Bearer {SECRET}", {"title": "x"})


def test_401_drops_the_credential(env):
    c = env.clock
    env({POST: pending(c), GET: approved(c),
         ("GET", "/api/x/"): (401, {"detail": "grant ended"})})
    assert run("request", "--reason", "Read your session 4328 please").exit_code == 0
    r = run("get", "/api/x/")
    assert r.exit_code == 1 and "grant ended" in r.output
    assert not token_file(env.home).exists()


@pytest.mark.parametrize("bad", ["https://evil.example/api/x/", "//evil.example/x", "api/x/"])
def test_credential_only_goes_to_canopy_web(env, bad):
    c = env.clock
    srv = env({POST: pending(c), GET: approved(c)})
    assert run("request", "--reason", "Read your session 4328 please").exit_code == 0
    n = len(srv.calls)
    r = run("get", bad)
    assert r.exit_code != 0 and len(srv.calls) == n


def test_workspace_pins_a_scoped_path(env):
    c = env.clock
    srv = env({POST: pending(c), GET: approved(c),
               ("GET", "/api/w/connect/projects/3/"): (200, {"id": 3})})
    assert run("request", "--reason", "Read your project 3 please").exit_code == 0
    assert run("get", "/api/projects/3/", "--workspace", "connect").exit_code == 0
    assert srv.calls[-1][1] == "/api/w/connect/projects/3/"


def test_approved_without_token_is_not_silently_usable(env):
    c = env.clock
    env({POST: pending(c), GET: approved(c, token=False)})
    r = run("request", "--reason", "Read your session 4328 please")
    assert r.exit_code == 1 and "already handed out" in r.output
    assert not token_file(env.home).exists()


def test_default_identity_never_reads_the_act_as_store():
    """act-as is explicit-only: the shared resolver and the MCP headers helper never pick it up."""
    for f in [ROOT / "src" / "orchestrator" / "canopy_web.py",
              ROOT / "src" / "orchestrator" / "turn_agent_token.py",
              *PLUGIN.rglob("canopy-web-mcp-headers*")]:
        text = f.read_text(encoding="utf-8")
        assert ".canopy/act-as" not in text and "act_as_cli" not in text, f


def test_agent_login_refuses_a_local_turn(monkeypatch):
    monkeypatch.setenv("CANOPY_LOCAL_TURN", "1")
    with pytest.raises(Exception, match="local turn"):
        act_as_cli.agent_login()


def test_agent_login_refuses_a_person_session(monkeypatch):
    monkeypatch.delenv("CANOPY_LOCAL_TURN", raising=False)
    monkeypatch.setattr(act_as_cli.canopy_web, "agent_context_slug", lambda *a, **k: "")
    with pytest.raises(Exception, match="belongs to no agent"):
        act_as_cli.agent_login()


# --- SessionEnd cleanup -------------------------------------------------------

def test_session_end_hook_deletes_this_sessions_and_expired_grants(tmp_path):
    store = tmp_path / ".canopy" / "act-as"
    store.mkdir(parents=True)
    future = _iso(dt.datetime.now(dt.timezone.utc).timestamp() + 900)
    past = _iso(dt.datetime.now(dt.timezone.utc).timestamp() - 5)
    (store / "turn-a.token").write_text(json.dumps({"claude_session_id": "s-1", "expires_at": future}))
    (store / "turn-b.token").write_text(json.dumps({"claude_session_id": "s-2", "expires_at": future}))
    (store / "turn-c.token").write_text(json.dumps({"claude_session_id": "s-3", "expires_at": past}))
    r = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "act_as_cleanup.py")],
                       input=json.dumps({"session_id": "s-1"}), text=True,
                       capture_output=True, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in store.iterdir()) == ["turn-b.token"]


def test_session_end_hook_is_registered():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    cmds = [h["command"] for entry in hooks["SessionEnd"] for h in entry["hooks"]]
    assert any("act_as_cleanup.py" in c for c in cmds)


# --- the deny rail ------------------------------------------------------------

STORE = "/.canopy/" + "act-as"  # built at runtime so the literal never sits on a scanned line


@pytest.fixture
def guard(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    loader = importlib.machinery.SourceFileLoader(
        "gating_guard_act_as", str(PLUGIN / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "ada", "channels": [], "deny": [], "approve": []}))
    return lambda tool, tool_input: mod.run(str(tmp_path), {"tool_name": tool, "tool_input": tool_input})


@pytest.mark.parametrize("cmd", [
    "cat ~" + STORE + "/turn-1.token",
    'curl -H "Authorization: Bearer $(jq -r .token ~' + STORE + '/t.token)" https://x/api/',
    "ls $HOME" + STORE,
    "python3 -c 'print(1)' < /Users/ada" + STORE + "/t.token",
])
def test_rail_blocks_hand_rolled_use(guard, cmd):
    code, _, err = guard("Bash", {"command": cmd})
    assert code == 2, cmd
    assert "canopy act-as call" in err


def test_rail_blocks_file_tools(guard):
    assert guard("Read", {"file_path": "/Users/ada" + STORE + "/turn-1.token"})[0] == 2


@pytest.mark.parametrize("cmd", [
    "canopy act-as get /api/sessions/4328/",
    "canopy act-as call POST /api/x/ --json '{}'",
    "canopy act-as stop",
    'grep -rn "' + STORE.lstrip("/") + '" src/',
    "cat plugins/canopy/skills/act-as-you/SKILL.md",
])
def test_rail_lets_the_cli_and_repo_greps_pass(guard, cmd):
    assert guard("Bash", {"command": cmd})[0] == 0, cmd


# --- the skill names the CLI it runs -----------------------------------------

def test_skill_names_every_verb():
    text = (PLUGIN / "skills" / "act-as-you" / "SKILL.md").read_text()
    for verb in ("request", "status", "stop", "get", "call"):
        assert f"canopy act-as {verb}" in text, verb
        assert verb in act_as_cli.act_as_group.commands, verb
    assert "as <person>" in text
