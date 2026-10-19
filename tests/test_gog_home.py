"""Per-agent gog homes (runner isolation units, spec §6).

On a box whose runner names a unit secret ($CANOPY_GOG_SECRET_FILE), each agent's mail token
lives in its own gog home (~/.gog/<slug>/) under its own keyring password,
HKDF-SHA256(secret, info="gog:"+slug). There is no box-wide password, so a confined turn's
mail capability opens only its own agent's mail (Review Focus 3). Without the variable,
nothing changes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import gog_home as gh
from tests.test_agent_prepare import Box, _prep, _repo, _token

SECRET = bytes(range(32))


@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    f = tmp_path / "etc" / "canopy-runner" / "dimagi.gog-secret"
    f.parent.mkdir(parents=True)
    f.write_bytes(SECRET)
    os.chmod(f, 0o400)
    monkeypatch.setenv("CANOPY_GOG_SECRET_FILE", str(f))
    return f


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("CANOPY_GOG_SECRET_FILE", "GOG_HOME", "GOG_KEYRING_PASSWORD",
              "GOG_KEYRING_BACKEND", "CANOPY_AGENT", "CANOPY_AGENT_SLUG"):
        monkeypatch.delenv(k, raising=False)


# ── derivation ────────────────────────────────────────────────────────────────

def test_derivation_is_rfc5869_hkdf_sha256():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    want = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"gog:eva").derive(SECRET)
    assert gh.derive_password(SECRET, "eva") == want.hex()


def test_derivation_is_stable_and_differs_per_slug_and_secret():
    a = gh.derive_password(SECRET, "eva")
    assert a == gh.derive_password(SECRET, "eva") and len(a) == 64
    assert a != gh.derive_password(SECRET, "hal")
    assert a != gh.derive_password(b"\x01" * 32, "eva")


def test_known_vector():
    # Pinned (and cross-checked against `cryptography` above) so canopy-web's stdlib copy in
    # cloud_runner can assert the same bytes.
    assert gh.derive_password(bytes(range(32)), "eva") == \
        "ab2166c77a1c7ae28294765e11a5cac5758470a68352a5e872402d33388047fc"
    assert gh.derive_password(b"\x00" * 32, "ace") == \
        "7ee5d739a4ccab4ff159f9157a857e887702c7abb5617194923ba61cb84ebf59"


def test_gog_home_is_per_agent(tmp_path):
    assert gh.gog_home("eva", tmp_path) == tmp_path / ".gog" / "eva"
    with pytest.raises(gh.GogHomeError):
        gh.gog_home("../hal", tmp_path)


def test_agent_gog_env(secret_file, tmp_path):
    env = gh.agent_gog_env("eva", home=tmp_path)
    assert env == {"GOG_HOME": str(tmp_path / ".gog" / "eva"),
                   "GOG_KEYRING_PASSWORD": gh.derive_password(SECRET, "eva"),
                   "GOG_KEYRING_BACKEND": "file"}


# ── the confined turn's mail capability (Review Focus 3) ──────────────────────

def test_confined_mail_env_carries_only_this_agents_pair(secret_file, tmp_path):
    box = {
        "CANOPY_GOG_SECRET_FILE": str(secret_file),
        # what the runner process might also hold: another agent's pair, a box-wide one
        "GOG_HOME": str(tmp_path / ".gog" / "hal"),
        "GOG_KEYRING_PASSWORD": gh.derive_password(SECRET, "hal"),
        "GOG_KEYRING_BACKEND": "file",
    }
    env = gh.confined_mail_env("eva", box, home=tmp_path)
    assert set(env) == {"GOG_HOME", "GOG_KEYRING_PASSWORD", "GOG_KEYRING_BACKEND"}
    assert env["GOG_HOME"] == str(tmp_path / ".gog" / "eva")
    assert env["GOG_KEYRING_PASSWORD"] == gh.derive_password(SECRET, "eva")
    values = " ".join(env.values())
    assert "hal" not in values
    assert gh.derive_password(SECRET, "hal") not in values
    assert str(secret_file) not in values and "CANOPY_GOG_SECRET_FILE" not in env
    assert SECRET.hex() not in values


def test_confined_mail_env_with_an_unreadable_secret_carries_nothing(tmp_path):
    env = gh.confined_mail_env("eva", {"CANOPY_GOG_SECRET_FILE": str(tmp_path / "nope"),
                                       "GOG_KEYRING_PASSWORD": "box-wide"}, home=tmp_path)
    assert env == {}


def test_confined_mail_env_legacy_is_what_the_box_has():
    box = {"GOG_KEYRING_PASSWORD": "box-wide", "GOG_KEYRING_BACKEND": "file", "PATH": "/bin"}
    assert gh.confined_mail_env("eva", box) == {"GOG_KEYRING_PASSWORD": "box-wide",
                                                "GOG_KEYRING_BACKEND": "file"}


# ── legacy behaviour holds when unset ─────────────────────────────────────────

def test_unset_means_nothing_changes(tmp_path):
    assert gh.agent_gog_env("eva", home=tmp_path) == {}
    env = {"CANOPY_AGENT": "eva"}
    assert gh.adopt_session_agent_env(env) == {} and env == {"CANOPY_AGENT": "eva"}


def test_prepare_without_a_secret_runs_gog_as_before(tmp_path):
    _repo(tmp_path / "agents")
    box = Box(tmp_path, vault_items={
        "Agent-Ace/gog-token/credential": _token(),
        "Canopy-Shared/gog-oauth-client/credential": '{"installed": {}}'})
    _prep(tmp_path, box).prepare("ace")
    gog = [e for e in box.envs if e[0][0] == "gog"]
    assert gog and all("GOG_HOME" not in env for _, env in gog)
    assert (tmp_path / "gog" / "credentials-canopy.json").is_file()


# ── prepare imports into the agent's own home ─────────────────────────────────

def test_prepare_imports_into_the_agents_own_gog_home(secret_file, tmp_path):
    _repo(tmp_path / "agents")
    box = Box(tmp_path, vault_items={
        "Agent-Ace/gog-token/credential": _token(),
        "Canopy-Shared/gog-oauth-client/credential": '{"installed": {}}'})
    prep = _prep(tmp_path, box)
    home = Path(os.environ["HOME"])
    rep = prep.prepare("ace")
    imports = [env for argv, env in box.envs if argv[:4] == ["gog", "auth", "tokens", "import"]]
    assert imports, rep.notes
    for env in imports:
        assert env["GOG_HOME"] == str(home / ".gog" / "ace")
        assert env["GOG_KEYRING_PASSWORD"] == gh.derive_password(SECRET, "ace")
        assert env["GOG_KEYRING_BACKEND"] == "file"
    # The OAuth client landed in the agent's own home, not the box's shared gog dir.
    assert (home / ".gog" / "ace" / "credentials-canopy.json").is_file()
    assert not (tmp_path / "gog" / "credentials-canopy.json").exists()
    # And this process is back to how it was.
    assert "GOG_HOME" not in os.environ and "GOG_KEYRING_PASSWORD" not in os.environ
    assert prep.gog_dir == tmp_path / "gog"


def test_prepare_with_an_unreadable_secret_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_GOG_SECRET_FILE", str(tmp_path / "missing"))
    rep = _prep(tmp_path, Box(tmp_path)).prepare("ace", report=False)
    assert not rep.ok and any("CANOPY_GOG_SECRET_FILE" in n for n in rep.notes)


# ── every gog call canopy makes in an agent session ───────────────────────────

def test_an_agent_session_adopts_its_own_gog_home_at_import(secret_file, tmp_path):
    code = ("import os, json; import orchestrator.agent_email as a; "
            "print(json.dumps({'dir': a.GOG_CONFIG_DIR, 'home': os.environ.get('GOG_HOME'), "
            "'pw': os.environ.get('GOG_KEYRING_PASSWORD')}))")
    env = {**os.environ, "HOME": str(tmp_path), "CANOPY_AGENT": "eva",
           "CANOPY_GOG_SECRET_FILE": str(secret_file)}
    env.pop("GOG_HOME", None)
    out = json.loads(subprocess.run([sys.executable, "-c", code], env=env, check=True,
                                    capture_output=True, text=True).stdout)
    assert out["home"] == str(tmp_path / ".gog" / "eva")
    assert out["dir"] == str(tmp_path / ".gog" / "eva")
    assert out["pw"] == gh.derive_password(SECRET, "eva")


def test_a_runner_given_pair_is_never_overridden(secret_file, tmp_path):
    env = {"CANOPY_AGENT": "eva", "CANOPY_GOG_SECRET_FILE": str(secret_file),
           "GOG_HOME": "/given"}
    assert gh.adopt_session_agent_env(env) == {} and env["GOG_HOME"] == "/given"
