"""A cloud runner's own Claude credentials, opened from the vault that holds them
(canopy-web spec 2026-10-23-runner-claude-credentials-in-the-vault-design.md).

canopy-web answers only with ciphertext in an agent's workspace-vault layer shape, so
the box opens it with the code that opens those. Real crypto: a value is read only if
this device's own key opens the vault key's copy made for it."""
from __future__ import annotations

import json
import os

from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import cred_resolve, device_key, vault_cli
from orchestrator import vault_zk as zk

ME_KEY = ec.generate_private_key(ec.SECP256R1())
ME_JWK = zk.jwk(ME_KEY.public_key())
RUNNER = "00000000-0000-4000-8000-000000000001"


def _ecdh() -> device_key.EcdhKey:
    def derive(point: bytes) -> bytes:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
        return ME_KEY.exchange(ec.ECDH(), peer)
    return device_key.EcdhKey("software", ME_JWK, derive)


def _layer(values, *, mine=True):
    vid = "11111111-2222-3333-4444-555555555555"
    binding, vk, kv = f"vault:{vid}", os.urandom(32), 1
    jkt = zk.jwk_thumbprint(ME_JWK)
    return {"vault_id": vid, "name": "ec2-box Claude credentials", "workspace": "dimagi",
            "binding_id": binding, "kv": kv,
            "wrap": zk.wrap_ecdh_es(vk, ME_JWK, zk.device_info(binding, kv, jkt)) if mine else None,
            "values": {n: zk.encrypt_value(vk, binding, n, kv, v) for n, v in values.items()} if mine else {}}


class Server:
    def __init__(self, layers, status=200):
        self.layers, self.status, self.calls = layers, status, []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, dict(headers)))
        if self.status != 200:
            return self.status, json.dumps({"error": "device_proof_required"})
        return 200, json.dumps({"vaults": self.layers})


def test_the_box_opens_its_own_credentials(monkeypatch):
    monkeypatch.setattr(device_key, "proof_headers", lambda m, u: {"X-Canopy-Aws-Proof": "p"})
    server = Server([_layer({"claude-token:t1": "sk-ant-oat01-real"})])
    out = cred_resolve.resolve_runner(RUNNER, token="runner-token", base_url="https://c.example",
                                      transport=server, ecdh=_ecdh())
    assert out["values"] == {"claude-token:t1": "sk-ant-oat01-real"}
    assert out["vault_layers"][0]["opened"] is True
    method, url, headers = server.calls[0]
    assert (method, url) == ("GET", f"https://c.example/api/harness/runners/{RUNNER}/credential/resolve")
    assert headers["X-Canopy-Aws-Proof"] == "p", "the device proof goes with the read"
    assert headers["Authorization"] == "Bearer runner-token"


def test_a_vault_this_box_holds_no_copy_of_is_named_not_fatal(monkeypatch):
    monkeypatch.setattr(device_key, "proof_headers", lambda m, u: {})
    out = cred_resolve.resolve_runner(RUNNER, token="t", base_url="https://c.example",
                                      transport=Server([_layer({"x": "y"}, mine=False)]), ecdh=_ecdh())
    assert out["values"] == {}
    assert out["vault_layers"][0]["opened"] is False


def test_a_refused_proof_says_so(monkeypatch):
    monkeypatch.setattr(device_key, "proof_headers", lambda m, u: {})
    try:
        cred_resolve.resolve_runner(RUNNER, token="t", base_url="https://c.example",
                                    transport=Server([], status=401), ecdh=_ecdh())
    except cred_resolve.DeviceProofRequired as e:
        assert "own approved device key" in str(e)
    else:
        raise AssertionError("a 401 device_proof_required must raise")


def test_the_command_reads_as_the_box_and_prints_one_json_line(monkeypatch):
    seen = {}

    def fake(runner_id, *, token, base_url):
        seen.update(runner=runner_id, token=token)
        return {"values": {"claude-token:t1": "v"}, "vault_layers": []}

    monkeypatch.setattr(cred_resolve, "resolve_runner", fake)
    monkeypatch.setenv("CANOPY_TOKEN", "the-box-token")
    res = CliRunner().invoke(vault_cli.vault_group, ["runner-credentials", "--runner", RUNNER, "--print-json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output.strip().splitlines()[-1])["values"] == {"claude-token:t1": "v"}
    assert seen == {"runner": RUNNER, "token": "the-box-token"}


def test_the_command_never_reads_with_a_persons_token(monkeypatch):
    """No CANOPY_TOKEN and no device key to buy a runner token: refuse, never fall
    back to the person's PAT (canopy-web would answer runner_only anyway)."""
    monkeypatch.delenv("CANOPY_TOKEN", raising=False)
    monkeypatch.setenv("CANOPY_WEB_PAT", "a-persons-pat")
    from orchestrator import runner_session
    monkeypatch.setattr(runner_session, "token", lambda base, transport=None: "")
    monkeypatch.setattr(cred_resolve, "resolve_runner",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not read")))
    res = CliRunner().invoke(vault_cli.vault_group, ["runner-credentials", "--runner", RUNNER, "--print-json"])
    assert res.exit_code != 0
    assert "no runner token" in res.output


# ── sealing: the box writes its own credentials into its vault ────────────────

class SealServer:
    """Answers the resolve with one layer this device holds a copy of, and records the PUT."""

    def __init__(self, *, mine=True):
        self.vid = "11111111-2222-3333-4444-555555555555"
        self.binding, self.vk, self.kv = f"vault:{self.vid}", os.urandom(32), 1
        jkt = zk.jwk_thumbprint(ME_JWK)
        self.layer = {"vault_id": self.vid, "name": "box vault", "workspace": "dimagi",
                      "binding_id": self.binding, "kv": self.kv, "values": {},
                      "wrap": zk.wrap_ecdh_es(self.vk, ME_JWK, zk.device_info(self.binding, self.kv, jkt))
                      if mine else None}
        self.calls = []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, dict(headers), json.loads(data) if data else None))
        if method == "GET":
            return 200, json.dumps({"vaults": [self.layer]})
        return 200, json.dumps({"names": [v["name"] for v in json.loads(data)["values"]]})


def test_the_box_seals_values_canopy_web_cannot_read(monkeypatch):
    monkeypatch.setattr(device_key, "proof_headers", lambda m, u: {"X-Canopy-Aws-Proof": f"proof-{m}"})
    server = SealServer()
    out = cred_resolve.seal_runner_values(RUNNER, {"claude-token:t1": "sk-ant-oat01-new"}, token="box",
                                          base_url="https://c.example", transport=server, ecdh=_ecdh())
    assert out == {"names": ["claude-token:t1"], "vault_id": server.vid}
    method, url, headers, body = server.calls[-1]
    assert (method, url) == ("PUT", f"https://c.example/api/harness/runners/{RUNNER}/vault/values")
    assert headers["X-Canopy-Aws-Proof"] == "proof-PUT", "a proof made for the write itself"
    [sent] = body["values"]
    assert "sk-ant-oat01-new" not in json.dumps(body), "only ciphertext leaves the box"
    # Bound to this vault, this name and this key version: it opens only as that.
    assert zk.decrypt_value(server.vk, server.binding, "claude-token:t1", 1, sent["value_zk"]) == "sk-ant-oat01-new"


def test_a_box_with_no_copy_of_the_key_cannot_seal(monkeypatch):
    monkeypatch.setattr(device_key, "proof_headers", lambda m, u: {})
    server = SealServer(mine=False)
    try:
        cred_resolve.seal_runner_values(RUNNER, {"claude-token:t1": "v"}, token="box",
                                        base_url="https://c.example", transport=server, ecdh=_ecdh())
    except cred_resolve.ZkUnlockError as e:
        assert "no copy of a vault key" in str(e)
    else:
        raise AssertionError("no key copy must refuse")
    assert [c[0] for c in server.calls] == ["GET"], "nothing is written"


def test_the_seal_command_reads_values_from_stdin(monkeypatch):
    seen = {}

    def fake(runner_id, values, *, token, base_url):
        seen.update(runner=runner_id, values=values, token=token)
        return {"names": sorted(values), "vault_id": "v"}

    monkeypatch.setattr(cred_resolve, "seal_runner_values", fake)
    monkeypatch.setenv("CANOPY_TOKEN", "the-box-token")
    res = CliRunner().invoke(vault_cli.vault_group, ["runner-seal", "--runner", RUNNER],
                             input=json.dumps({"claude-token:t1": "secret"}))
    assert res.exit_code == 0, res.output
    assert json.loads(res.output.strip().splitlines()[-1]) == {"names": ["claude-token:t1"], "vault_id": "v"}
    assert seen == {"runner": RUNNER, "values": {"claude-token:t1": "secret"}, "token": "the-box-token"}
    assert "secret" not in res.output
