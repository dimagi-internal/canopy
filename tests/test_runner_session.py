"""A runner's credential reads fall back to its own device-key token (canopy-web#1604).

2026-10-17: the laptop runner's owner PAT was revoked; the runner carried on with its
device key, but `canopy agent prepare` / `canopy cred` on it were refused. These pin
the fallback: only credential reads, only after a refusal, with a fresh proof.
"""
import datetime as dt
import json

import pytest

from orchestrator import canopy_web as cw
from orchestrator import device_key, runner_session

BASE = "https://x.test"
RESOLVE = "/api/agents/hal/credentials/resolve?refs=gog-token"


@pytest.fixture
def box(monkeypatch, tmp_path):
    """A paired runner whose device key buys `crt_box`, with no cached token."""
    monkeypatch.setattr(runner_session, "CACHE", tmp_path / "runner-session.json")
    monkeypatch.setattr(device_key, "runner_id_from_config", lambda path=None: "r-1")
    monkeypatch.delenv("CANOPY_DEVICE_KEY", raising=False)
    bought = []

    def buy(runner_id, base_url="", *, transport=None):
        bought.append(runner_id)
        exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        return {"access_token": "crt_box", "expires_at": exp.isoformat(), "runner": runner_id}

    import orchestrator.vault_cli as vault_cli
    monkeypatch.setattr(vault_cli, "run_runner_session", buy)
    proofs = iter(["proof-2", "proof-3", "proof-4"])
    monkeypatch.setattr(device_key, "proof_headers", lambda m, u, **kw: {"DPoP": next(proofs)})
    return bought


def _server(answers):
    """A transport answering by bearer: {token: (status, body)}; records each request."""
    seen = []

    def fake(method, url, headers, body):
        tok = headers["Authorization"].split(" ", 1)[1]
        seen.append({"token": tok, "dpop": headers.get("DPoP"), "url": url})
        status, payload = answers.get(tok, (401, '{"detail": "Authentication required"}'))
        return status, json.dumps(payload) if not isinstance(payload, str) else payload
    return fake, seen


def test_a_refused_pat_is_retried_as_the_runner_with_a_fresh_proof(box):
    fake, seen = _server({"crt_box": (200, {"values": {"gog-token": "x"}})})
    out = cw.call("GET", RESOLVE, base_url=BASE, token="revoked-pat", transport=fake,
                  headers={"DPoP": "proof-1"})
    assert out["values"] == {"gog-token": "x"}
    assert [s["token"] for s in seen] == ["revoked-pat", "crt_box"]
    assert [s["dpop"] for s in seen] == ["proof-1", "proof-2"]   # never a replay


def test_an_agents_own_pat_refused_403_is_retried_too(box):
    fake, seen = _server({"agent-pat": (403, {"title": "needs a runner"}),
                          "crt_box": (200, {"credential_source": "canopy-web"})})
    out = cw.call("GET", "/api/agents/hal/credentials/access", base_url=BASE,
                  token="agent-pat", transport=fake)
    assert out["credential_source"] == "canopy-web"


def test_no_pat_at_all_goes_straight_out_as_the_runner(box, monkeypatch):
    monkeypatch.setattr(cw, "resolve_token", lambda t: (_ for _ in ()).throw(RuntimeError("no PAT")))
    fake, seen = _server({"crt_box": (200, {"repo_url": "u"})})
    assert cw.call("GET", "/api/agents/hal/runtime", base_url=BASE, transport=fake) == {"repo_url": "u"}
    assert [s["token"] for s in seen] == ["crt_box"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/agents/hal/vault"),       # not a read a runner token may make
    ("GET", "/api/me"),
    ("POST", "/api/agents/hal/credentials/resolve"),
])
def test_anything_else_is_never_retried(box, method, path):
    fake, seen = _server({})
    with pytest.raises(cw.CanopyError, match="401"):
        cw.call(method, path, base_url=BASE, token="revoked-pat", transport=fake)
    assert [s["token"] for s in seen] == ["revoked-pat"] and box == []


@pytest.mark.parametrize("why", ["device_proof_required", "zk_wrap_missing"])
def test_a_proof_refusal_stands(box, why):
    fake, seen = _server({"pat": (401, {"error": why})})
    with pytest.raises(cw.CanopyError, match=why):
        cw.call("GET", RESOLVE, base_url=BASE, token="pat", transport=fake)
    assert len(seen) == 1


def test_a_working_pat_is_used_as_before(box):
    fake, seen = _server({"pat": (200, {"ok": True})})
    assert cw.call("GET", RESOLVE, base_url=BASE, token="pat", transport=fake) == {"ok": True}
    assert box == []


def test_the_token_is_cached_and_a_refused_one_forgotten(box):
    fake, _ = _server({"crt_box": (200, {})})
    cw.call("GET", RESOLVE, base_url=BASE, token="pat", transport=fake)
    cw.call("GET", RESOLVE, base_url=BASE, token="pat", transport=fake)
    assert box == ["r-1"]                                   # bought once
    assert oct(runner_session.CACHE.stat().st_mode & 0o777) == "0o600"
    fake, _ = _server({})                                   # now the runner token is refused too
    with pytest.raises(cw.CanopyError):
        cw.call("GET", RESOLVE, base_url=BASE, token="pat", transport=fake)
    assert not runner_session.CACHE.exists()


def test_no_runner_or_opted_out_means_no_fallback(box, monkeypatch):
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "0")
    assert runner_session.token(BASE) == ""
    monkeypatch.delenv("CANOPY_DEVICE_KEY")
    monkeypatch.setattr(device_key, "runner_id_from_config", lambda path=None: "")
    assert runner_session.token(BASE) == "" and box == []
