"""Tests for scripts/ddd/auth.py — the shared canopy-web auth/URL helpers.

No network. scripts.ddd.auth now RE-EXPORTS the canonical resolvers from
orchestrator.canopy_web, so the on-disk fallback is patched at its real source —
``orchestrator.canopy_web.TOKEN_FILE`` — not the re-exported alias.
"""
from __future__ import annotations

from pathlib import Path

import json

import pytest

from scripts.ddd.auth import (
    DEFAULT_API,
    TOKEN_FILE,
    resolve_base_url,
    resolve_token,
)


# ---------------------------------------------------------------------------
# resolve_base_url
# ---------------------------------------------------------------------------


def test_resolve_base_url_strips_trailing_slash():
    assert resolve_base_url("http://x/") == "http://x"


def test_resolve_base_url_explicit_wins(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_API_URL", "http://env-host/")
    assert resolve_base_url("http://explicit/") == "http://explicit"


def test_resolve_base_url_env_wins_over_default(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_API_URL", "http://env-host/")
    assert resolve_base_url(None) == "http://env-host"


def test_resolve_base_url_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("CANOPY_WEB_API_URL", raising=False)
    assert resolve_base_url(None) == DEFAULT_API


# ---------------------------------------------------------------------------
# resolve_token — precedence: explicit arg > env > agent .env > TOKEN_FILE > raise
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _neutral_cwd(tmp_path, monkeypatch):
    """resolve_token now walks UP from cwd looking for an agent repo, so these
    cases must not depend on where pytest happens to be invoked from."""
    monkeypatch.chdir(tmp_path)


def _make_agent_repo(root, slug, home, pat=None):
    """A minimal agent repo (plugin.json = identity) + its provisioned ~/.<slug>/.env."""
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    if pat is not None:
        d = home / f".{slug}"
        d.mkdir(parents=True, exist_ok=True)
        (d / ".env").write_text(f"SOME_OTHER=1\nCANOPY_WEB_PAT={pat}\n")
    return root


def test_agent_env_pat_beats_global_token_file(monkeypatch, tmp_path):
    """The regression: an agent must act as ITSELF, not as whoever owns TOKEN_FILE.
    That file exists on every operator laptop, which is what masked this."""
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    _make_agent_repo(repo, "hal", home, pat="hal-own-pat")
    tok = tmp_path / "token"
    tok.write_text("operator-token")
    monkeypatch.setattr("orchestrator.canopy_web.TOKEN_FILE", tok)
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home))
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.chdir(repo)
    assert resolve_token(None) == "hal-own-pat"


def test_explicit_env_still_wins_over_agent_env(monkeypatch, tmp_path):
    """A runner pinning the identity for a turn must not be overridden by the repo."""
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    _make_agent_repo(repo, "hal", home, pat="hal-own-pat")
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home))
    monkeypatch.setenv("CANOPY_WEB_PAT", "runner-pinned")
    monkeypatch.chdir(repo)
    assert resolve_token(None) == "runner-pinned"


def test_agent_repo_without_env_falls_back_to_token_file(monkeypatch, tmp_path):
    """An unprovisioned agent keeps working exactly as before."""
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    _make_agent_repo(repo, "hal", home, pat=None)
    tok = tmp_path / "token"
    tok.write_text("operator-token")
    monkeypatch.setattr("orchestrator.canopy_web.TOKEN_FILE", tok)
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home))
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.chdir(repo)
    assert resolve_token(None) == "operator-token"


def test_resolves_from_a_subdirectory_of_the_agent_repo(monkeypatch, tmp_path):
    """Turns do not always run at the repo root."""
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    _make_agent_repo(repo, "hal", home, pat="hal-own-pat")
    deep = repo / "bin" / "nested"
    deep.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home))
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.chdir(deep)
    assert resolve_token(None) == "hal-own-pat"



def test_resolve_token_explicit_wins(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_PAT", "env-token")
    assert resolve_token("explicit-token") == "explicit-token"


def test_resolve_token_env_wins_over_file(monkeypatch, tmp_path):
    tok = tmp_path / "token"
    tok.write_text("file-token")
    monkeypatch.setattr("orchestrator.canopy_web.TOKEN_FILE", tok)
    monkeypatch.setenv("CANOPY_WEB_PAT", "env-token")
    assert resolve_token(None) == "env-token"


def test_resolve_token_file_fallback(monkeypatch, tmp_path):
    tok = tmp_path / "token"
    tok.write_text("  file-token  \n")
    monkeypatch.setattr("orchestrator.canopy_web.TOKEN_FILE", tok)
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    assert resolve_token(None) == "file-token"


def test_resolve_token_raises_when_none(monkeypatch, tmp_path):
    missing = tmp_path / "nope"
    monkeypatch.setattr("orchestrator.canopy_web.TOKEN_FILE", missing)
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    with pytest.raises(RuntimeError):
        resolve_token(None)
