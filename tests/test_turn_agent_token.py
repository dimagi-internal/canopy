"""An agent's runner turn prefers the login canopy-web issued for THAT turn over its stored
canopy-pat, and falls back to the stored PAT on any miss (turn-scoped agent token, phase 1;
canopy-web docs/superpowers/specs/2026-10-21-turn-scoped-agent-token-design.md)."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from orchestrator import canopy_web as cw
from orchestrator import device_key, runner_session, turn_agent_token

TID = "3f2b8c1e-0000-4000-8000-000000000009"
BASE = "https://x.test"
URL = f"{BASE}/api/harness/runners/r-1/turns/{TID}/agent-token"
SESSION_URL = URL + "?scope=session"


@pytest.fixture
def box(monkeypatch):
    """This machine runs runner r-1, whose device key buys `crt_box`; the session runs turn TID."""
    monkeypatch.setenv("CANOPY_WEB_API_URL", BASE)
    monkeypatch.setenv("CANOPY_TURN_ID", TID)
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.delenv("CANOPY_TURN_AGENT_TOKEN", raising=False)
    monkeypatch.setattr(device_key, "runner_id_from_config", lambda path=None: "r-1")
    monkeypatch.setattr(runner_session, "token", lambda base, transport=None: "crt_box")
    seen = []

    def serve(status, body, session=None):
        """`session`: what a `?scope=session` ask gets instead (default: the same answer)."""
        def fake(method, url, headers, data):
            seen.append({"method": method, "url": url, "auth": headers.get("Authorization")})
            st, b = session if (session is not None and url.endswith("?scope=session")) else (status, body)
            return st, json.dumps(b) if b is not None else ""
        monkeypatch.setattr(cw, "urllib_transport", fake)

    serve(404, {"detail": "turn not found"})
    return serve, seen


GRANT = {"token": "turn-tok", "agent_slug": "hal", "turn_id": TID,
         "expires_at": "2099-01-01T00:00:00Z"}


def test_a_runner_turn_gets_its_own_login_asked_as_the_box(box):
    serve, seen = box
    serve(200, GRANT, session=(404, {"detail": "no live session"}))
    assert turn_agent_token.resolve("hal") == "turn-tok"
    # The session's login is asked first; with none, the turn's.
    assert seen == [{"method": "POST", "url": SESSION_URL, "auth": "Bearer crt_box"},
                    {"method": "POST", "url": URL, "auth": "Bearer crt_box"}]
    cached = turn_agent_token.CACHE_DIR / f"{TID}.json"
    assert stat.S_IMODE(cached.stat().st_mode) == 0o600
    assert turn_agent_token.resolve("hal") == "turn-tok"   # cached: no second ask
    assert len(seen) == 2


@pytest.mark.parametrize("status,body,asks", [
    (404, {"detail": "turn not found"}, 2),      # finished, another box's, or an older canopy-web
    (409, {"detail": "confined"}, 2),            # a caller's turn
    (200, {**GRANT, "agent_slug": "eva"}, 1),     # a login for some other agent: answered, refused
])
def test_a_refusal_falls_back_and_is_remembered(box, status, body, asks):
    serve, seen = box
    serve(status, body)
    assert turn_agent_token.resolve("hal") == ""
    assert turn_agent_token.resolve("hal") == ""
    assert len(seen) == asks   # session (then turn) — once; then remembered


SESSION_GRANT = {**GRANT, "token": "session-tok", "session_id": "5e55-1"}


def test_a_laptop_session_gets_the_login_that_lasts_it(box):
    serve, seen = box
    serve(200, GRANT, session=(200, SESSION_GRANT))
    assert turn_agent_token.resolve("hal") == "session-tok"
    assert [x["url"] for x in seen] == [SESSION_URL]          # never the turn login
    assert turn_agent_token.resolve("hal") == "session-tok"   # cached for its lifetime
    assert len(seen) == 1


def test_a_turn_login_is_kept_only_briefly(box):
    serve, seen = box
    serve(200, GRANT, session=(404, {}))
    t0 = 1_000_000.0
    assert turn_agent_token.resolve("hal", now=lambda: t0) == "turn-tok"
    later = t0 + turn_agent_token.TURN_LOGIN_CACHE_SECONDS + 1
    serve(200, GRANT, session=(200, SESSION_GRANT))
    assert turn_agent_token.resolve("hal", now=lambda: later) == "session-tok"


def test_session_only_never_takes_a_turn_login(box):
    serve, seen = box
    serve(200, GRANT, session=(404, {}))
    assert turn_agent_token.resolve("hal", session_only=True) == ""
    assert [x["url"] for x in seen] == [SESSION_URL]


def test_no_turn_or_no_runner_means_no_ask(box, monkeypatch):
    serve, seen = box
    serve(200, GRANT)
    monkeypatch.delenv("CANOPY_TURN_ID")
    assert turn_agent_token.resolve("hal") == ""
    monkeypatch.setenv("CANOPY_TURN_ID", TID)
    monkeypatch.setattr(device_key, "runner_id_from_config", lambda path=None: "")
    monkeypatch.setattr(device_key, "read_status", lambda: {})
    assert turn_agent_token.resolve("hal") == ""
    assert seen == []


def test_opt_out(box, monkeypatch):
    serve, seen = box
    serve(200, GRANT)
    monkeypatch.setenv("CANOPY_TURN_AGENT_TOKEN", "0")
    assert turn_agent_token.resolve("hal") == ""
    assert seen == []


def test_resolve_token_prefers_the_turn_login_over_the_stored_pat(box, monkeypatch):
    serve, _ = box
    monkeypatch.setenv("CANOPY_AGENT", "hal")
    monkeypatch.setattr(cw, "_slug_env_pat", lambda slug: "stored-pat")
    serve(200, GRANT)
    assert cw.resolve_token(None) == "turn-tok"


def test_resolve_token_falls_back_to_the_stored_pat(box, monkeypatch):
    serve, _ = box
    monkeypatch.setenv("CANOPY_AGENT", "hal")
    monkeypatch.setattr(cw, "_slug_env_pat", lambda slug: "stored-pat")
    serve(404, {"detail": "turn not found"})
    assert cw.resolve_token(None) == "stored-pat"


def test_an_env_pat_still_wins_it_is_how_the_cloud_runner_pins_the_turn(box, monkeypatch):
    serve, seen = box
    monkeypatch.setenv("CANOPY_AGENT", "hal")
    monkeypatch.setenv("CANOPY_WEB_PAT", "cloud-turn-tok")
    serve(200, GRANT)
    assert cw.resolve_token(None) == "cloud-turn-tok"
    assert seen == []


def test_a_human_session_never_asks(box, monkeypatch):
    serve, seen = box
    monkeypatch.setattr(cw, "agent_context_slug", lambda start=None: "")
    monkeypatch.setattr(cw, "_operator_token", lambda: "operator")
    serve(200, GRANT)
    assert cw.resolve_token(None) == "operator"
    assert seen == []


# ---- the MCP headers helper ---------------------------------------------------------

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/canopy/scripts/canopy-web-mcp-headers.js"


def _fake_cli(home, prints=""):
    """The canopy CLI the helper asks for a session login: this home's, never the real one."""
    bin_ = home / ".local" / "bin"
    bin_.mkdir(parents=True, exist_ok=True)
    cli = bin_ / "canopy"
    cli.write_text(f"#!/bin/sh\nprintf '%s' '{prints}'\n")
    cli.chmod(0o755)


def _helper(home, env):
    if not (home / ".local" / "bin" / "canopy").exists():
        _fake_cli(home)
    e = {"PATH": os.environ["PATH"], "HOME": str(home), **env}
    r = subprocess.run(["node", str(SCRIPT)], cwd=str(home), env=e, capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout).get("Authorization", "")


def test_the_mcp_helper_prefers_the_cloud_runners_turn_file(tmp_path):
    home = tmp_path / "home"
    (home / ".hal").mkdir(parents=True)
    (home / ".hal" / ".env").write_text("CANOPY_WEB_PAT=stored-pat\n")
    env = {"CANOPY_AGENT": "hal", "CANOPY_TURN_ID": TID}
    assert _helper(home, env) == "Bearer stored-pat"        # no turn file: as before
    d = home / ".canopy" / "agent-token" / "turn"
    d.mkdir(parents=True)
    (d / f"{TID}.token").write_text("turn-tok\n")
    assert _helper(home, env) == "Bearer turn-tok"
    assert _helper(home, {**env, "CANOPY_TURN_ID": "../x"}) == "Bearer stored-pat"


def test_the_mcp_helper_asks_for_the_laptop_sessions_login(tmp_path):
    home = tmp_path / "home"
    (home / ".hal").mkdir(parents=True)
    (home / ".hal" / ".env").write_text("CANOPY_WEB_PAT=stored-pat\n")
    env = {"CANOPY_AGENT": "hal"}
    assert _helper(home, env) == "Bearer stored-pat"         # the CLI had none: as before
    _fake_cli(home, prints="session-tok")
    assert _helper(home, env) == "Bearer session-tok"
    assert _helper(home, {**env, "CANOPY_TURN_AGENT_TOKEN": "0"}) == "Bearer stored-pat"


def test_session_token_refuses_to_print_to_a_terminal(monkeypatch):
    from click.testing import CliRunner

    from orchestrator.agent_cli import agent

    monkeypatch.setattr(turn_agent_token, "resolve", lambda slug, session_only=False: "session-tok")
    r = CliRunner().invoke(agent, ["session-token", "--slug", "hal"])
    assert r.exit_code == 0 and r.output.strip() == "session-tok"   # piped (CliRunner is not a tty)
