"""Secrets off disk: `canopy agent prepare --runner` writes no secret values, and
`canopy cred env --out / --print-json` hands one turn its env (runner isolation units, §4).

Opt-in: `$CANOPY_TURN_ENV_ROOT` (the runner's tmpfs root for per-turn env files) or
`--no-secret-files`. Without either, prepare writes exactly what it always did — the live
fleet's runners read ~/.<slug>/.env until they opt in (tests/test_agent_prepare.py pins that).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from orchestrator import agent_prepare as ap
from orchestrator import cred_cli
from tests.test_agent_prepare import AGENT_KEY, Box, _creds, _prep, _repo


class PipeBox(Box):
    """The fake machine, plus `op inject` to stdout (no -o): the in-memory resolve."""

    def __call__(self, argv, **kw):
        if argv[:2] == ["op", "inject"] and "-o" not in argv:
            env = kw.get("env") or {}
            self.calls.append((list(argv), env.get("OP_SERVICE_ACCOUNT_TOKEN"), None, None))
            self.envs.append((list(argv), dict(env)))
            if env.get("OP_SERVICE_ACCOUNT_TOKEN") != AGENT_KEY or env.get("AGENT_VAULT") != "Agent-Ace":
                return SimpleNamespace(returncode=1, stdout="", stderr="could not resolve")
            return SimpleNamespace(returncode=0, stdout="CANOPY_WEB_PAT=x\nGDRIVE_ROOT=y\n",
                                   stderr="")
        if argv[0] == "bash":
            env = kw.get("env") or {}
            f = env.get("AGENT_ENV_FILE")
            self.setup_saw = (f, Path(f).read_text() if f and Path(f).is_file() else None,
                              oct(Path(f).stat().st_mode & 0o777) if f and Path(f).is_file() else None)
            # An agent installer that writes its own copies, as ACE's does.
            data = Path(env["CLAUDE_PLUGIN_DATA"])
            (data / ".env").write_text("CANOPY_WEB_PAT=x\n")
            (data / "gws-sa-key.json").write_text("{}")
        return super().__call__(argv, **kw)


def _secret_files(*roots: Path) -> list[str]:
    hits = []
    for root in roots:
        for p in root.rglob("*"):
            if p.is_file() and p.name in (".env", ".gws-sa-key.json") and \
                    "plugins/data" not in str(p):
                hits.append(str(p))
    return hits


@pytest.fixture
def turn_root(tmp_path, monkeypatch):
    root = tmp_path / "run" / "canopy" / "1001"
    monkeypatch.setenv("CANOPY_TURN_ENV_ROOT", str(root))
    return root


# ── prepare --runner ──────────────────────────────────────────────────────────

def test_prepare_runner_writes_no_secret_files(tmp_path, turn_root):
    _repo(tmp_path / "agents", setup=True)
    box = PipeBox(tmp_path)
    rep = _prep(tmp_path, box).prepare("ace")
    assert rep.env_ok is True and "not written" in rep.env
    assert _secret_files(tmp_path / "home", tmp_path / "agents") == []
    assert not (tmp_path / "home" / ".ace" / ".env").exists()
    # The installer got the values through a 0600 file under the tmpfs root, gone after.
    f, text, mode = box.setup_saw
    assert f.startswith(str(turn_root)) and text == "CANOPY_WEB_PAT=x\nGDRIVE_ROOT=y\n"
    assert mode == "0o600" and not Path(f).exists()
    assert not any(turn_root.iterdir())


def test_prepare_env_only_resolves_in_memory(tmp_path, turn_root):
    _repo(tmp_path / "agents")
    box = PipeBox(tmp_path)
    rep = _prep(tmp_path, box).prepare("ace", env_only=True)
    assert rep.env_ok is True and rep.env.startswith("OK (2 vars")
    assert [c[0][:2] for c in box.calls] == [["op", "inject"]]
    assert box.calls[0][1] == AGENT_KEY
    assert _secret_files(tmp_path / "home", tmp_path / "agents") == []


def test_prepare_canopy_web_backend_writes_nothing(tmp_path, turn_root):
    _repo(tmp_path / "agents")
    box = PipeBox(tmp_path)
    creds = _creds(credential_source="canopy-web", key="", values={"CANOPY_WEB_PAT": "w"})
    rep = _prep(tmp_path, box, creds=creds).prepare("ace", env_only=True)
    assert rep.env_ok is True and "canopy-web" in rep.env
    assert not (tmp_path / "home" / ".ace" / ".env").exists()


def test_prepare_resolve_failure_is_reported(tmp_path, turn_root):
    _repo(tmp_path / "agents")
    rep = _prep(tmp_path, PipeBox(tmp_path), creds=_creds(key="ops_WRONG")).prepare(
        "ace", env_only=True)
    assert rep.env_ok is False and any("op inject failed" in n for n in rep.notes)


def test_the_flag_opts_in_without_the_root(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_TURN_ENV_ROOT", raising=False)
    _repo(tmp_path / "agents")
    prep = _prep(tmp_path, PipeBox(tmp_path))
    prep.no_secret_files = True
    assert prep.prepare("ace", env_only=True).env_ok is True
    assert not (tmp_path / "home" / ".ace" / ".env").exists()


def test_without_opt_in_prepare_still_writes_the_legacy_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_TURN_ENV_ROOT", raising=False)
    monkeypatch.delenv("CANOPY_NO_SECRET_FILES", raising=False)
    _repo(tmp_path / "agents")
    rep = _prep(tmp_path, Box(tmp_path)).prepare("ace")
    assert rep.env_ok is True and (tmp_path / "home" / ".ace" / ".env").is_file()


# ── cred env --out / --print-json ─────────────────────────────────────────────

@pytest.fixture
def fake_values(monkeypatch):
    got = {}

    def runner_values(slug, *, agent_root, **kw):
        got["runner"] = (slug, agent_root)
        return {"CANOPY_WEB_PAT": "p", "OP_SERVICE_ACCOUNT_TOKEN": "k", "gog-token": "{\n}"}, "1Password"

    monkeypatch.setattr(cred_cli, "runner_env_values", runner_values)
    monkeypatch.setattr(cred_cli, "exec_env", lambda slug, **kw: ({"CANOPY_WEB_PAT": "h"}, "canopy-web"))
    return got


def test_cred_env_out_is_0600_under_the_root(tmp_path, turn_root, fake_values):
    out = turn_root / "turn-1" / "env"
    r = CliRunner().invoke(cred_cli.cred_group, ["env", "--slug", "ace", "--runner",
                                                 "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    text = out.read_text()
    assert "CANOPY_WEB_PAT=p\n" in text and "OP_SERVICE_ACCOUNT_TOKEN=k\n" in text
    assert "gog-token" not in text      # not an env var: never in the file
    assert fake_values["runner"][0] == "ace"


@pytest.mark.parametrize("where", ["outside", "dotdot", "symlink", "the-root-itself"])
def test_cred_env_out_refuses_outside_the_root(tmp_path, turn_root, fake_values, where):
    turn_root.mkdir(parents=True)
    out = {"outside": tmp_path / "home" / ".ace" / ".env",
           "dotdot": turn_root / ".." / "elsewhere" / "env",
           "the-root-itself": turn_root}[where] if where != "symlink" else None
    if where == "symlink":
        (tmp_path / "persist").mkdir()
        (turn_root / "link").symlink_to(tmp_path / "persist")
        out = turn_root / "link" / "env"
    r = CliRunner().invoke(cred_cli.cred_group, ["env", "--slug", "ace", "--runner",
                                                 "--out", str(out)])
    assert r.exit_code == cred_cli.EXIT_USAGE
    assert "outside" in r.output or "is a directory" in r.output
    assert "runner" not in fake_values      # refused before resolving anything
    assert not (tmp_path / "home" / ".ace" / ".env").exists()
    assert not (tmp_path / "persist" / "env").exists()


def test_cred_env_out_anywhere_without_a_root(tmp_path, monkeypatch, fake_values):
    monkeypatch.delenv("CANOPY_TURN_ENV_ROOT", raising=False)
    out = tmp_path / "x" / "env"
    r = CliRunner().invoke(cred_cli.cred_group, ["env", "--agent", "ace", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert out.read_text().endswith("CANOPY_WEB_PAT=h\n")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_cred_env_print_json_shape(tmp_path, turn_root, fake_values):
    r = CliRunner().invoke(cred_cli.cred_group, ["env", "--slug", "ace", "--runner",
                                                 "--print-json"])
    assert r.exit_code == 0, r.output
    body = json.loads(r.stdout.strip().splitlines()[-1])
    assert body == {"agent": "ace", "source": "1Password",
                    "values": {"CANOPY_WEB_PAT": "p", "OP_SERVICE_ACCOUNT_TOKEN": "k"}}
    assert not turn_root.exists()       # wrote nothing


def test_cred_env_runner_needs_a_destination(fake_values):
    r = CliRunner().invoke(cred_cli.cred_group, ["env", "--slug", "ace", "--runner"])
    assert r.exit_code == cred_cli.EXIT_USAGE


def test_runner_env_values_resolves_like_prepare(tmp_path, monkeypatch):
    """`cred env --runner` uses the runner's credentials and the agent's own key, in memory."""
    _repo(tmp_path / "agents")
    box = PipeBox(tmp_path)
    seen = {}

    def fetch(slug, **kw):
        seen.update(kw)
        return _creds()
    monkeypatch.setattr(ap, "fetch_runner_credentials", fetch)
    monkeypatch.setenv("CANOPY_TURN_ID", "turn-9")
    values, how = cred_cli.runner_env_values("ace", agent_root=tmp_path / "agents",
                                             base_url="https://canopy.test", token="rt",
                                             runner=box)
    assert values == {"CANOPY_WEB_PAT": "x", "GDRIVE_ROOT": "y"} and how == "1Password"
    assert seen["token"] == "rt" and seen["in_turn"] is True
    assert box.calls[0][1] == AGENT_KEY
    assert not (tmp_path / "home" / ".ace" / ".env").exists()
