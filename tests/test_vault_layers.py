"""Workspace vaults layered under an agent's own secrets, on the client (canopy-web spec
2026-10-22-vault-access-follows-canopy-access-design.md §4). Real crypto: a layer's value
is read only if this device's own key opens that layer's copy of ITS vault key."""
from __future__ import annotations

import os

from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import cred_resolve, device_key, vault_zk_cli
from orchestrator import vault_zk as zk

ME_KEY = ec.generate_private_key(ec.SECP256R1())
ME_JWK = zk.jwk(ME_KEY.public_key())


def _ecdh() -> device_key.EcdhKey:
    def derive(point: bytes) -> bytes:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
        return ME_KEY.exchange(ec.ECDH(), peer)
    return device_key.EcdhKey("software", ME_JWK, derive)


def _layer(name, values, *, mine=True, kv=1):
    vid = f"00000000-0000-0000-0000-00000000000{len(name)}"
    binding, vk = f"vault:{vid}", os.urandom(32)
    jkt = zk.jwk_thumbprint(ME_JWK)
    return {"vault_id": vid, "name": name, "workspace": "dimagi", "binding_id": binding, "kv": kv,
            "wrap": zk.wrap_ecdh_es(vk, ME_JWK, zk.device_info(binding, kv, jkt)) if mine else None,
            "values": {n: zk.encrypt_value(vk, binding, n, kv, v) for n, v in values.items()} if mine else {}}


def test_layers_merge_in_order_and_the_agent_wins():
    body = {"values": {"HQ_PASSWORD": "agent-own", "shared": "agent-says"},
            "vaults": [_layer("Org", {"gog-oauth-client": "org-client", "shared": "org-says"}),
                       _layer("Division", {"shared": "division-says", "OCS": "div-ocs"})]}
    out = cred_resolve.merge_vault_layers("ace", body, ecdh=_ecdh())
    assert out["values"] == {"gog-oauth-client": "org-client", "shared": "agent-says", "OCS": "div-ocs",
                             "HQ_PASSWORD": "agent-own"}
    assert "vaults" not in out
    assert [r["opened"] for r in out["vault_layers"]] == [True, True]


def test_a_layer_this_device_cannot_open_is_named_not_fatal():
    body = {"values": {"a": "1"}, "vaults": [_layer("Locked", {"x": "y"}, mine=False)]}
    out = cred_resolve.merge_vault_layers("ace", body, ecdh=_ecdh())
    assert out["values"] == {"a": "1"}
    assert out["vault_layers"] == [{"vault": "Locked", "opened": False,
                                    "why": "this device holds no copy of its key"}]


def test_a_layer_bound_to_another_vault_does_not_open():
    good = _layer("Org", {"x": "y"})
    stolen = dict(good, binding_id="vault:someone-else")   # the server lies about the binding
    out = cred_resolve.merge_vault_layers("ace", {"values": {}, "vaults": [stolen]}, ecdh=_ecdh())
    assert out["values"] == {} and out["vault_layers"][0]["opened"] is False


def test_a_workspace_vault_session_targets_its_own_routes():
    v = vault_zk_cli.ZkVault("vault abc", token="t", vault_id="abc")
    assert v.base == "/api/vaults/abc/zk"
    assert vault_zk_cli.ZkVault("ace", token="t").base == "/api/agents/ace/vault/zk"
