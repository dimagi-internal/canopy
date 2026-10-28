"""A runner finishes an agent's Google sign-in: canopy-web relays the code, the box holds
the PKCE verifier and the client secret, and only ciphertext leaves the box. Real
crypto: the stored value opens only with the vault key, or the reader it was sealed to."""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import device_key, google_mint as gm, vault_cli
from orchestrator import vault_zk as zk

AGENT_ID = "42"
CLIENT_ID = "cid.apps.googleusercontent.com"
CLIENT_FILE = json.dumps({"web": {"client_id": CLIENT_ID, "client_secret": "GOCSPX-x"}})
ME_KEY = ec.generate_private_key(ec.SECP256R1())
ME_JWK = zk.jwk(ME_KEY.public_key())


def _ecdh() -> device_key.EcdhKey:
    def derive(point: bytes) -> bytes:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
        return ME_KEY.exchange(ec.ECDH(), peer)
    return device_key.EcdhKey("software", ME_JWK, derive)


def _id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.sig"


def _google(*, refresh="1//refresh", email="ace@dimagi-ai.com", scope=None):
    out = {"id_token": _id_token(email),
           "scope": scope or "openid email https://www.googleapis.com/auth/gmail.modify "
                             "https://www.googleapis.com/auth/drive"}
    if refresh:
        out["refresh_token"] = refresh
    return out


def _work(**over):
    jkt = zk.jwk_thumbprint(ME_JWK)
    base = {"code": "4/code", "code_verifier": "v" * 64, "client_id": CLIENT_ID,
            "client": "canopy-web", "redirect_uri": "https://canopy.example/api/oauth/google/callback",
            "expected_account": "ace@dimagi-ai.com", "agent_id": AGENT_ID, "kv": 0,
            "recipients": [{"kind": "device", "target": jkt, "public_jwk": ME_JWK}]}
    return {**base, **over}


def test_the_code_goes_to_google_with_the_verifier_only_the_box_holds():
    sent = {}

    def post(url, data):
        sent.update(url=url, **data)
        return _google()

    gm.finish(_work(), client_raw=CLIENT_FILE, vk=None, post=post)
    assert sent["url"] == gm.TOKEN_ENDPOINT
    assert sent["code_verifier"] == "v" * 64 and sent["client_secret"] == "GOCSPX-x"
    assert sent["redirect_uri"].endswith("/api/oauth/google/callback")


def test_without_the_vault_key_the_token_is_sealed_to_the_readers_canopy_web_named():
    out = gm.finish(_work(), client_raw=CLIENT_FILE, vk=None, post=lambda u, d: _google(),
                    now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert out["name"] == "gog-token" and out["account"] == "ace@dimagi-ai.com"
    assert out["value_zk"].startswith("zks1:") and out["kv"] == 0
    token = json.loads(zk.open_sealed(out["value_zk"], AGENT_ID, "gog-token", "device",
                                      zk.jwk_thumbprint(ME_JWK), ME_KEY))
    assert token == {"email": "ace@dimagi-ai.com", "client": "canopy-web",
                     "services": ["drive", "gmail"], "created_at": "2026-10-27T00:00:00Z",
                     "refresh_token": "1//refresh",
                     "scopes": sorted(["openid", "email", "https://www.googleapis.com/auth/gmail.modify",
                                       "https://www.googleapis.com/auth/drive"])}


def test_with_the_vault_key_the_token_is_stored_under_it():
    vk = os.urandom(32)
    out = gm.finish(_work(kv=3), client_raw=CLIENT_FILE, vk=vk, vk_kv=3, post=lambda u, d: _google())
    assert out["value_zk"].startswith("zk1:") and out["kv"] == 3
    assert json.loads(zk.decrypt_value(vk, AGENT_ID, "gog-token", 3, out["value_zk"]))["refresh_token"] == "1//refresh"


def test_a_reader_whose_thumbprint_is_not_its_key_is_left_out():
    other = zk.jwk(ec.generate_private_key(ec.SECP256R1()).public_key())
    work = _work(recipients=[{"kind": "device", "target": zk.jwk_thumbprint(ME_JWK), "public_jwk": other}])
    with pytest.raises(gm.MintError) as e:
        gm.finish(work, client_raw=CLIENT_FILE, vk=None, post=lambda u, d: _google())
    assert e.value.error == "zk-no-recipients"


@pytest.mark.parametrize("resp, error", [
    (_google(refresh=""), "no-refresh-token"),
    ({**_google(), "id_token": ""}, "no-email"),
    (_google(email="jj@gmail.com"), "wrong-account"),
])
def test_what_would_only_look_like_success_is_refused(resp, error):
    with pytest.raises(gm.MintError) as e:
        gm.finish(_work(), client_raw=CLIENT_FILE, vk=None, post=lambda u, d: resp)
    assert e.value.error == error
    assert "1//refresh" not in json.dumps(e.value.out())


def test_the_account_check_ignores_case():
    out = gm.finish(_work(expected_account="ACE@dimagi-ai.com"), client_raw=CLIENT_FILE, vk=None,
                    post=lambda u, d: _google())
    assert out["account"] == "ace@dimagi-ai.com"


def test_no_expected_account_takes_any():
    out = gm.finish(_work(expected_account=""), client_raw=CLIENT_FILE, vk=None,
                    post=lambda u, d: _google(email="family.agent@gmail.com"))
    assert out["account"] == "family.agent@gmail.com"


def test_a_missing_or_foreign_client_is_named():
    with pytest.raises(gm.MintError) as e:
        gm.finish(_work(), client_raw=None, vk=None, post=lambda u, d: _google())
    assert e.value.error == "no-client" and "gog-oauth-client-web" in e.value.detail
    foreign = json.dumps({"client_id": "other", "client_secret": "s"})
    with pytest.raises(gm.MintError) as e:
        gm.finish(_work(), client_raw=foreign, vk=None, post=lambda u, d: _google())
    assert e.value.error == "no-client"


def test_flat_gog_client_files_are_read_too():
    assert gm.client_secret(json.dumps({"client_id": CLIENT_ID, "client_secret": "s"}), CLIENT_ID) == "s"
    assert gm.client_secret(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": "i"}}),
                            CLIENT_ID) == "i"


def test_a_refused_exchange_says_why():
    def post(url, data):
        raise OSError("connection reset")
    with pytest.raises(gm.MintError) as e:
        gm.finish(_work(), client_raw=CLIENT_FILE, vk=None, post=post)
    assert e.value.error == "exchange-failed" and "connection reset" in e.value.detail


def test_services_follow_what_google_granted():
    assert gm.services_for(["https://www.googleapis.com/auth/presentations",
                            "https://www.googleapis.com/auth/calendar", "openid"]) == ["calendar", "slides"]


def _layer(values):
    vid = "11111111-2222-3333-4444-555555555555"
    binding, vk, kv = f"vault:{vid}", os.urandom(32), 1
    jkt = zk.jwk_thumbprint(ME_JWK)
    return {"vault_id": vid, "name": "Shared", "binding_id": binding, "kv": kv,
            "wrap": zk.wrap_ecdh_es(vk, ME_JWK, zk.device_info(binding, kv, jkt)),
            "values": {n: zk.encrypt_value(vk, binding, n, kv, v) for n, v in values.items()}}


def test_run_reads_the_client_and_the_vault_key_with_the_boxs_own_key(monkeypatch):
    """End to end on the box: the resolve hands the shared vault's client and this
    device's wrap of the agent's vault key; the token comes out as zk1."""
    monkeypatch.setattr(device_key, "proof_headers", lambda m, u: {})
    vk, kv = os.urandom(32), 2
    jkt = zk.jwk_thumbprint(ME_JWK)
    body = {"zk": True, "kv": kv, "agent_id": AGENT_ID, "values": {},
            "wrap": zk.wrap_ecdh_es(vk, ME_JWK, zk.device_info(AGENT_ID, kv, jkt)),
            "vaults": [_layer({"gog-oauth-client-web": CLIENT_FILE})]}
    calls = []

    def transport(method, url, headers, data):
        calls.append(url)
        return 200, json.dumps(body)

    monkeypatch.setattr(device_key, "load_ecdh", _ecdh)
    out = gm.run("ace", _work(kv=kv), token="rt", base_url="https://c.example", transport=transport,
                 ecdh=_ecdh(), post=lambda u, d: _google())
    assert calls == ["https://c.example/api/agents/ace/credentials/resolve?refs=gog-oauth-client-web"]
    assert out["kv"] == kv and json.loads(zk.decrypt_value(vk, AGENT_ID, "gog-token", kv, out["value_zk"]))["email"] \
        == "ace@dimagi-ai.com"


def test_the_command_prints_one_line_and_never_the_token(monkeypatch):
    monkeypatch.setattr(vault_cli, "_runner_and_token", lambda r, b: ("r1", "rt"))
    monkeypatch.setattr(gm, "run", lambda slug, work, **kw: gm.finish(
        work, client_raw=CLIENT_FILE, vk=None, post=lambda u, d: _google()))
    res = CliRunner().invoke(vault_cli.vault_group, ["google-mint", "--agent", "ace"], input=json.dumps(_work()))
    assert res.exit_code == 0, res.output
    out = json.loads(res.output.strip().splitlines()[-1])
    assert out["value_zk"].startswith("zks1:") and "1//refresh" not in res.output


def test_the_command_reports_a_refusal_as_json(monkeypatch):
    monkeypatch.setattr(vault_cli, "_runner_and_token", lambda r, b: ("r1", "rt"))
    monkeypatch.setattr(gm, "run", lambda slug, work, **kw: gm.finish(
        work, client_raw=CLIENT_FILE, vk=None, post=lambda u, d: _google(refresh="")))
    res = CliRunner().invoke(vault_cli.vault_group, ["google-mint", "--agent", "ace"], input=json.dumps(_work()))
    assert res.exit_code == 1
    assert json.loads(res.output.strip().splitlines()[-1])["error"] == "no-refresh-token"



# --- a person's own account, delegated to the agent ------------------------------------

def _delegated(**over):
    return _work(**{"delegated": True, "name": "google-delegated-gina@gmail.com",
                    "expected_account": "gina@gmail.com", "kv": 3, **over})


def test_a_delegated_account_is_sealed_under_its_own_name_never_under_the_vault_key():
    """Even on a box that holds the vault key: under it, every vault member could read her inbox."""
    vk = os.urandom(32)
    out = gm.finish(_delegated(), client_raw=CLIENT_FILE, vk=vk, vk_kv=3,
                    post=lambda u, d: _google(email="gina@gmail.com"))
    assert out["name"] == "google-delegated-gina@gmail.com" and out["account"] == "gina@gmail.com"
    assert out["value_zk"].startswith("zks1:") and out["kv"] == 3
    token = json.loads(zk.open_sealed(out["value_zk"], AGENT_ID, "google-delegated-gina@gmail.com", "device",
                                      zk.jwk_thumbprint(ME_JWK), ME_KEY))
    assert token["email"] == "gina@gmail.com" and token["refresh_token"] == "1//refresh"


def test_a_delegated_sign_in_without_its_entry_name_is_refused():
    with pytest.raises(gm.MintError) as e:
        gm.finish(_delegated(name="gog-token"), client_raw=CLIENT_FILE, vk=None,
                  post=lambda u, d: _google(email="gina@gmail.com"))
    assert e.value.error == "bad-request"


def test_a_delegated_sign_in_from_another_account_is_refused():
    with pytest.raises(gm.MintError) as e:
        gm.finish(_delegated(), client_raw=CLIENT_FILE, vk=None, post=lambda u, d: _google(email="jj@gmail.com"))
    assert e.value.error == "wrong-account"
