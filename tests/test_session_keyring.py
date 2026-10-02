"""A Mac laptop runner's turns must be able to open gog's keyring.

2026-10-02: a second operator's ACE laptop had `GOG_KEYRING_BACKEND=file` + a password
in ACE's plugin .env. The runner (launchd: PATH + PYTHONUNBUFFERED, no TTY) spawned a
caller turn that could neither read nor send mail, while every doctor — which loads the
.env itself — passed. These pin: the check models a TURN's view, it is a hard fail in
preflight / health, bootstrap repairs the box, and the cloud (Linux) path is untouched.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator import agent_bootstrap as ab
from orchestrator import agent_email as ae
from orchestrator import agent_health as ah


class Runner:
    """launchctl getenv answers from `launchd`; gog search succeeds (as it does from a
    shell that HAS the password — the misleading green this is about)."""

    def __init__(self, launchd=None):
        self.launchd = launchd or {}
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if argv[:2] == ["launchctl", "getenv"]:
            v = self.launchd.get(argv[2], "")
            return subprocess.CompletedProcess(argv, 0 if v else 1, v + "\n" if v else "", "")
        return subprocess.CompletedProcess(argv, 0, '{"threads": [], "accounts": []}', "")


def _gog(tmp_path, backend=None):
    gog = tmp_path / "gog"
    gog.mkdir(exist_ok=True)
    cfg = {"account_clients": {"ace@dimagi-ai.com": "canopy"}}
    if backend:
        cfg["keyring_backend"] = backend
    (gog / "config.json").write_text(json.dumps(cfg))
    (gog / "credentials-canopy.json").write_text('{"client_id": "x", "client_secret": "y"}')
    return gog


def _plugin_env(home: Path, text: str) -> Path:
    p = home / ".claude" / "plugins" / "data" / "ace-ace" / ".env"
    p.parent.mkdir(parents=True)
    p.write_text(text)
    return p


def _problems(tmp_path, *, backend=None, launchd=None, environ=None, platform="darwin"):
    return ae.session_keyring_problems(
        "ace", gog_dir=str(_gog(tmp_path, backend)), home=tmp_path / "home",
        environ=environ or {}, runner=Runner(launchd), platform=platform)


def test_file_backend_with_no_password_for_turns_is_a_problem(tmp_path):
    lines = _problems(tmp_path, backend="file")
    assert lines and "dead in every turn" in lines[0]
    assert any("gog auth keyring keychain" in x for x in lines)
    assert any("Never `launchctl setenv" in x for x in lines)


def test_keychain_backend_with_nothing_else_is_fine(tmp_path):
    assert _problems(tmp_path, backend="keychain") == []
    assert _problems(tmp_path) == []          # unset = auto = Keychain on a Mac


def test_a_plugin_env_selecting_file_is_a_split_brain_even_when_turns_are_fine(tmp_path):
    p = _plugin_env(tmp_path / "home", "A=1\nGOG_KEYRING_BACKEND=file\nGOG_KEYRING_PASSWORD=pw\n")
    lines = _problems(tmp_path)
    assert lines and "two keyrings" in lines[0] and str(p) in lines[0]
    assert not any("pw" == x.strip() for x in lines)            # never echoes the password


def test_this_shells_file_backend_is_named_too(tmp_path):
    lines = _problems(tmp_path, environ={"GOG_KEYRING_BACKEND": "file"})
    assert lines and "this shell" in lines[0]


def test_file_backend_is_fine_when_launchd_really_provides_the_password(tmp_path):
    assert _problems(tmp_path, backend="file",
                     launchd={"GOG_KEYRING_PASSWORD": "pw"}) == []


def test_the_cloud_runner_path_is_untouched(tmp_path):
    _plugin_env(tmp_path / "home", "GOG_KEYRING_BACKEND=file\n")
    assert _problems(tmp_path, backend="file", platform="linux") == []


# ── hard fail where it is consumed ──────────────────────────────────────────

def test_preflight_fails_on_a_mac_whose_turns_cannot_open_the_keyring(tmp_path, monkeypatch):
    """THE regression: gog search from this shell succeeds, so preflight said OK."""
    monkeypatch.setattr(ae, "_PLATFORM", "darwin")
    monkeypatch.setattr(ae, "_launchd_env", lambda name, runner=None: "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    gog = _gog(tmp_path, "file")
    ident = ae.EmailIdentity(slug="ace", account="ace@dimagi-ai.com", client="canopy",
                             repo=tmp_path)
    ok, lines = ae.preflight(ident, gog_dir=str(gog), runner=Runner())
    assert not ok
    assert "dead in every turn" in lines[0]


def test_health_is_not_ready_when_turns_cannot_open_the_keyring(tmp_path, monkeypatch):
    monkeypatch.setattr(ae, "_PLATFORM", "darwin")
    monkeypatch.setattr(ae, "_launchd_env", lambda name, runner=None: "")
    monkeypatch.setattr(ae, "gog_keyring_backend", lambda gog_dir=None: "file")
    monkeypatch.setattr(ah, "probe_board", lambda *a, **k: {
        "needs_you": [], "harness_turns": [], "turn_age_days": 0})
    rep = ah.health_report("ace", runner=Runner())
    assert "keyring_unreachable_in_turns" in rep["flags"] and not rep["ready"]
    assert rep["keyring"]


# ── bootstrap repairs the box ───────────────────────────────────────────────

def _boot(tmp_path, platform, runner):
    return ab.Bootstrapper(runner=runner, web_token=lambda slug: (None, ""),
                           gog_dir=str(tmp_path / "gog"), home=tmp_path / "home",
                           verify=lambda ident, gog_dir=None: (True, ["OK"]),
                           echo=lambda s: None, platform=platform)


def test_bootstrap_moves_a_mac_to_the_keychain_and_strips_the_env_files(tmp_path):
    _gog(tmp_path, "file")
    home = tmp_path / "home"
    plugin = _plugin_env(home, "A=1\nGOG_KEYRING_BACKEND=file\nexport GOG_KEYRING_PASSWORD=pw\nB=2\n")
    own = home / ".ace" / ".env"
    own.parent.mkdir(parents=True)
    own.write_text("GOG_KEYRING_BACKEND=file\nC=3\n")
    rep = ab.AgentReport(slug="ace")
    _boot(tmp_path, "darwin", Runner()).step_keyring("ace", rep)

    cfg = json.loads((tmp_path / "gog" / "config.json").read_text())
    assert cfg["keyring_backend"] == "keychain"
    assert cfg["account_clients"] == {"ace@dimagi-ai.com": "canopy"}     # kept
    assert plugin.read_text() == "A=1\nB=2\n"
    assert own.read_text() == "C=3\n"
    assert oct(plugin.stat().st_mode & 0o777) == "0o600"
    assert any("Keychain" in n for n in rep.notes)


def test_bootstrap_gog_calls_never_carry_the_keyring_vars_on_a_mac(tmp_path, monkeypatch):
    monkeypatch.setenv("GOG_KEYRING_BACKEND", "file")
    monkeypatch.setenv("GOG_KEYRING_PASSWORD", "pw")
    r = Runner()
    _boot(tmp_path, "darwin", r).gog_accounts()
    (_argv, kw), = [c for c in r.calls if c[0][0] == "gog"]
    assert not set(ae.KEYRING_VARS) & set(kw["env"])


def test_bootstrap_leaves_the_cloud_layout_alone(tmp_path, monkeypatch):
    _gog(tmp_path, "file")
    plugin = _plugin_env(tmp_path / "home", "GOG_KEYRING_BACKEND=file\n")
    monkeypatch.setenv("GOG_KEYRING_PASSWORD", "pw")
    r = Runner()
    b = _boot(tmp_path, "linux", r)
    b.step_keyring("ace", ab.AgentReport(slug="ace"))
    b.gog_accounts()
    assert json.loads((tmp_path / "gog" / "config.json").read_text())["keyring_backend"] == "file"
    assert plugin.read_text() == "GOG_KEYRING_BACKEND=file\n"
    assert "env" not in [c for c in r.calls if c[0][0] == "gog"][0][1]
