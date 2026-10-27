"""A resolve answer the way canopy-web sends one: every value sealed (``zks1``) to this
machine's ECDH key. canopy-web holds no value it can read, so a fake resolve that hands
out plaintext tests a server that no longer exists — and the client refuses it.

A test that fakes a resolve builds its ``values`` with :func:`sealed_values` (and puts
``agent_id`` in the body, as canopy-web does) and installs this machine's key with
:func:`install`; ``cred_resolve.resolve`` then opens them exactly as on a runner.
"""
from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import device_key
from orchestrator import vault_zk as zk

AGENT_ID = "4242"

_PRIV = ec.generate_private_key(ec.SECP256R1())
JWK = zk.jwk(_PRIV.public_key())
JKT = zk.jwk_thumbprint(JWK)


def ecdh() -> device_key.EcdhKey:
    """This (test) machine's ECDH key."""
    def derive(point: bytes) -> bytes:
        return _PRIV.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point))
    return device_key.EcdhKey("software", JWK, derive)


def seal(name: str, value: str, agent_id: str = AGENT_ID) -> str:
    return zk.seal_value(agent_id, name, 0, value, [("device", JKT, JWK)])


def sealed_values(values: dict, agent_id: str = AGENT_ID) -> dict:
    """``{name: value}`` as canopy-web stores and sends it: each value sealed to this machine
    (an empty or None value stays as it is)."""
    return {n: (seal(n, v, agent_id) if v not in (None, "") else v) for n, v in values.items()}


def install(monkeypatch) -> None:
    """Give this machine the key the values are sealed to."""
    monkeypatch.setattr(device_key, "load_ecdh", lambda *a, **k: ecdh())
