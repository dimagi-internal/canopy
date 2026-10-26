"""A pool lease's slot data: canopy-web delivers the secret half sealed to the agent's
readers; `canopy vault open-slot-data` opens it with this machine's device key, and passes
an older server's plaintext `secret` through (canopy-web capabilities.md § 6)."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import cred_resolve, device_key, vault_cli
from orchestrator import vault_zk as zk

AGENT_ID = "6f1c0d2e-agent"
PIN = "PIN-73914-sentinel"


def _ecdh(priv) -> device_key.EcdhKey:
    jwk = zk.jwk(priv.public_key())

    def derive(point: bytes) -> bytes:
        return priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point))
    return device_key.EcdhKey("software", jwk, derive)


def _sealed(me: device_key.EcdhKey, value: str, slot="worker-1", key="pin", cap="test-worker") -> str:
    return zk.seal_value(AGENT_ID, cred_resolve.slot_sealed_name(cap, slot, key), 0, value,
                         [("device", me.jkt, me.public_jwk)])


def _lease(me, **extra):
    return {"lease_id": "l1", "capability": "test-worker", "slot": "worker-1", "status": "ready",
            "slot_data": {"public": {"phone": "+1"}, "agent_id": AGENT_ID,
                          "sealed": {"pin": _sealed(me, PIN)}, **extra}}


def test_sealed_values_open_with_this_devices_key():
    me = _ecdh(ec.generate_private_key(ec.SECP256R1()))
    lease = _lease(me)
    out = cred_resolve.open_slot_data(lease["slot_data"], capability="test-worker", slot="worker-1", ecdh=me)
    assert out == {"public": {"phone": "+1"}, "secret": {"pin": PIN}}


def test_a_value_bound_to_another_slot_does_not_open():
    me = _ecdh(ec.generate_private_key(ec.SECP256R1()))
    lease = _lease(me)
    with pytest.raises(cred_resolve.ZkUnlockError) as e:
        cred_resolve.open_slot_data(lease["slot_data"], capability="test-worker", slot="worker-2", ecdh=me)
    assert PIN not in str(e.value)


def test_a_value_sealed_to_another_device_does_not_open():
    me, other = (_ecdh(ec.generate_private_key(ec.SECP256R1())) for _ in range(2))
    with pytest.raises(cred_resolve.ZkUnlockError):
        cred_resolve.open_slot_data(_lease(other)["slot_data"], capability="test-worker", slot="worker-1",
                                    ecdh=me)


def test_an_older_servers_plaintext_secret_passes_through_without_a_device_key():
    data = {"public": {"phone": "+1"}, "secret": {"pin": PIN}}
    assert cred_resolve.open_slot_data(data, capability="", slot="") == data


def test_the_cli_reads_a_lease_on_stdin_and_prints_the_opened_data(monkeypatch):
    me = _ecdh(ec.generate_private_key(ec.SECP256R1()))
    monkeypatch.setattr(device_key, "load_ecdh", lambda *a, **k: me)
    res = CliRunner().invoke(vault_cli.vault_group, ["open-slot-data"], input=json.dumps(_lease(me)))
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == {"public": {"phone": "+1"}, "secret": {"pin": PIN}}


def test_the_cli_refuses_a_lease_with_no_slot_data():
    res = CliRunner().invoke(vault_cli.vault_group, ["open-slot-data"],
                             input=json.dumps({"lease_id": "l1", "slot_data": None}))
    assert res.exit_code != 0 and "no slot_data" in res.output
