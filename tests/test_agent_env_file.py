"""`$CANOPY_AGENT_ENV_FILE` is the first place every reader of an agent's env looks.

A runner that keeps secrets off disk writes each turn's env to a tmpfs file it removes when
the turn ends, and names it in CANOPY_AGENT_ENV_FILE (runner isolation units, spec §4). Each
reader below must read that file first, and fall back to the legacy ~/.<slug>/.env when the
variable is unset, names a missing file, or belongs to another agent's turn. Writers never
write the turn file and never write a persistent copy beside it.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import agent_env_file, canopy_web, cred_cli
from orchestrator.agent_email import agent_env_files, agent_env_value

ROOT = Path(__file__).resolve().parents[1]
HEADERS = ROOT / "plugins/canopy/scripts/canopy-web-mcp-headers.js"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".eva").mkdir(parents=True)
    (h / ".eva" / ".env").write_text("CANOPY_WEB_PAT=legacy_pat\nCANOPY_WEB_WORKSPACE=legacy-ws\n")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    for var in ("CANOPY_AGENT_ENV_FILE", "CANOPY_AGENT", "CANOPY_AGENT_SLUG", "CANOPY_TURN_ID"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def turn_file(tmp_path, monkeypatch):
    f = tmp_path / "run" / "canopy" / "1001" / "turn-1" / "env"
    f.parent.mkdir(parents=True)
    f.write_text("CANOPY_WEB_PAT=turn_pat\nexport CANOPY_WEB_WORKSPACE=turn-ws\n")
    os.chmod(f, 0o600)
    monkeypatch.setenv("CANOPY_AGENT_ENV_FILE", str(f))
    return f


# ── the helper ────────────────────────────────────────────────────────────────

def test_helper_prefers_the_turn_file(home, turn_file):
    assert agent_env_file.agent_env_path("eva") == turn_file


def test_helper_falls_back_when_unset(home):
    assert agent_env_file.agent_env_path("eva") == home / ".eva" / ".env"


def test_helper_falls_back_when_the_named_file_is_missing(home, turn_file):
    turn_file.unlink()
    assert agent_env_file.agent_env_path("eva") == home / ".eva" / ".env"


def test_helper_never_hands_one_agents_turn_file_to_another(home, turn_file, monkeypatch):
    monkeypatch.setenv("CANOPY_AGENT", "hal")
    assert agent_env_file.agent_env_path("eva") == home / ".eva" / ".env"
    assert agent_env_file.agent_env_path("hal") == turn_file


# ── canopy_web: the PAT and the workspace ─────────────────────────────────────

def test_slug_env_pat_reads_the_turn_file_first(home, turn_file, monkeypatch):
    monkeypatch.setenv("CANOPY_AGENT", "eva")
    assert canopy_web._slug_env_pat("eva") == "turn_pat"
    assert canopy_web._slug_env_value("eva", "CANOPY_WEB_WORKSPACE") == "turn-ws"


def test_slug_env_pat_legacy(home):
    assert canopy_web._slug_env_pat("eva") == "legacy_pat"
    assert canopy_web._slug_env_value("eva", "CANOPY_WEB_WORKSPACE") == "legacy-ws"


# ── agent_email.agent_env_files ───────────────────────────────────────────────

def test_agent_env_files_turn_file_first(home, turn_file):
    files = agent_env_files("eva", home)
    assert files[0] == turn_file and home / ".eva" / ".env" in files
    assert agent_env_value("eva", "CANOPY_WEB_WORKSPACE", home) == "turn-ws"


def test_agent_env_files_legacy(home):
    assert agent_env_files("eva", home) == [home / ".eva" / ".env"]


# ── cred check / cred env / cred sync ─────────────────────────────────────────

def test_cred_env_path_reads_the_turn_file(home, turn_file):
    assert cred_cli.env_path("eva") == turn_file
    assert cred_cli.legacy_env_path("eva") == home / ".eva" / ".env"


def test_cred_env_legacy(home):
    assert cred_cli.env_path("eva") == home / ".eva" / ".env"


def test_ensure_env_returns_the_turn_file_and_writes_nothing(home, turn_file, monkeypatch):
    (home / ".eva" / ".env").unlink()
    monkeypatch.setattr(cred_cli, "decide", lambda slug, **kw: cred_cli.Verdict(
        slug, True, "agent-turn", via="turn"))
    path, how = cred_cli.ensure_env("eva")
    assert path == turn_file and "CANOPY_AGENT_ENV_FILE" in how
    assert not (home / ".eva" / ".env").exists()


def test_cred_sync_skips_a_turn_with_its_own_env_file(home, turn_file, monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_x")

    def boom(*a, **k):
        raise AssertionError("op must not run")
    assert "CANOPY_AGENT_ENV_FILE" in cred_cli.sync_env("eva", runner=boom)


def test_cred_check_local_env_still_counts_on_a_laptop(home, monkeypatch):
    os.chmod(home / ".eva" / ".env", 0o600)
    monkeypatch.setattr(cred_cli, "session_identity",
                        lambda: cred_cli.Session("human", "eva"))
    v = cred_cli.decide("eva")
    assert v.allowed and v.via == "local-env"


# ── agent_doctor ──────────────────────────────────────────────────────────────

def test_doctor_env_home_turn_first(home, turn_file, tmp_path):
    from orchestrator.agent_doctor import env_home
    repo = tmp_path / "eva"
    repo.mkdir()
    assert env_home(repo, home) in (turn_file, home / ".eva" / ".env")
    assert env_home(repo, home) == turn_file


# ── the SessionStart hook ─────────────────────────────────────────────────────

def test_session_start_hook_does_not_sync_when_the_turn_has_its_env_file(tmp_path, monkeypatch):
    from tests.test_agent_op_env_hook import _run
    monkeypatch.setenv("CANOPY_AGENT_ENV_FILE", str(tmp_path / "env"))
    r, _ef, log = _run(tmp_path, have_key=True)
    assert r.returncode == 0
    assert "cred sync" not in (log.read_text() if log.exists() else "")


def test_session_start_hook_still_syncs_without_it(tmp_path, monkeypatch):
    from tests.test_agent_op_env_hook import _run
    monkeypatch.delenv("CANOPY_AGENT_ENV_FILE", raising=False)
    r, _ef, log = _run(tmp_path, have_key=True)
    assert r.returncode == 0 and "cred sync" in log.read_text()


# ── the MCP headersHelper (node) ──────────────────────────────────────────────

def _headers(home, cwd, env):
    e = {"PATH": os.environ["PATH"], "HOME": str(home), **env}
    r = subprocess.run(["node", str(HEADERS)], cwd=str(cwd), env=e, capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout).get("Authorization", "")


def test_headers_helper_reads_the_turn_file_first(home, turn_file, tmp_path):
    out = _headers(home, tmp_path, {"CANOPY_AGENT": "eva", "CANOPY_AGENT_ENV_FILE": str(turn_file)})
    assert out == "Bearer turn_pat"


def test_headers_helper_legacy(home, tmp_path):
    assert _headers(home, tmp_path, {"CANOPY_AGENT": "eva"}) == "Bearer legacy_pat"


def test_headers_helper_ignores_another_agents_turn_file(home, turn_file, tmp_path):
    out = _headers(home, tmp_path, {"CANOPY_AGENT": "eva", "CANOPY_AGENT_SLUG": "hal",
                                    "CANOPY_AGENT_ENV_FILE": str(turn_file)})
    # The turn is hal's, so its file is not eva's PAT; eva's legacy file answers.
    assert out == "Bearer legacy_pat"
