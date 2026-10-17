"""The one resolve client (canopy#946): `?refs=`, `X-Canopy-Turn-Id`, a DPoP proof when
this machine holds a registered device key — and EXACTLY the old behaviour when the server
ignores all three. canopy-web is faked at the transport boundary."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from orchestrator import canopy_web, cred_resolve, device_key

SECRET = "value-never-printed"


class FakeServer:
    """`mode`: "new" honours ?refs= (intersected with `declared`), "old" ignores it."""

    def __init__(self, values, *, mode="new", declared=None, status=200, body=None):
        self.values, self.mode, self.status, self.body = values, mode, status, body
        self.declared = set(values) if declared is None else set(declared)
        self.requests = []

    def __call__(self, method, url, headers, data):
        self.requests.append((method, url, dict(headers)))
        if self.status != 200:
            return self.status, json.dumps(self.body or {})
        q = parse_qs(urlsplit(url).query)
        values = dict(self.values)
        if self.mode == "new" and "refs" in q:
            want = set(q["refs"][0].split(","))
            values = {k: v for k, v in values.items() if k in want and k in self.declared}
        return 200, json.dumps({"values": values, "op_vault": "Agent-Eva", "op_sa_token": ""})


def _resolve(server, **kw):
    return cred_resolve.resolve("eva", token="pat", base_url="https://canopy.example",
                                transport=server, **kw)


def test_no_refs_is_todays_request():
    s = FakeServer({"A": SECRET, "B": "b"})
    body = _resolve(s, turn_id="")
    assert body["values"] == {"A": SECRET, "B": "b"}
    (method, url, headers), = s.requests
    assert url == "https://canopy.example/api/agents/eva/credentials/resolve"
    assert "DPoP" not in headers and cred_resolve.TURN_HEADER not in headers


def test_refs_are_sent_and_honoured():
    s = FakeServer({"A": SECRET, "B": "b", "C": "c"})
    body = _resolve(s, refs=["A", "B"], turn_id="")
    assert body["values"] == {"A": SECRET, "B": "b"}
    assert len(s.requests) == 1
    assert urlsplit(s.requests[0][1]).query == "refs=A,B"


def test_an_old_server_that_ignores_refs_is_filtered_here():
    s = FakeServer({"A": SECRET, "B": "b", "C": "c"}, mode="old")
    assert _resolve(s, refs=["A"], turn_id="")["values"] == {"A": SECRET}


def test_a_ref_the_server_drops_as_undeclared_is_fetched_without_refs():
    # A new server intersects ?refs= with runtime_sources; an agent that declares none
    # there (Eva) would get nothing back. One more read, without refs, then filter.
    s = FakeServer({"A": SECRET, "B": "b"}, declared=[])
    assert _resolve(s, refs=["A", "B"], turn_id="")["values"] == {"A": SECRET, "B": "b"}
    assert [urlsplit(u).query for _, u, _ in s.requests] == ["refs=A,B", ""]


def test_a_ref_that_does_not_exist_is_simply_absent():
    s = FakeServer({"A": SECRET})
    assert _resolve(s, refs=["A", "NOPE"], turn_id="")["values"] == {"A": SECRET}


def test_turn_id_header_when_a_turn_is_known(monkeypatch):
    s = FakeServer({})
    _resolve(s, turn_id="t-123")
    assert s.requests[0][2][cred_resolve.TURN_HEADER] == "t-123"
    monkeypatch.setenv("CANOPY_TURN_ID", "t-env")
    _resolve(s)
    assert s.requests[1][2][cred_resolve.TURN_HEADER] == "t-env"


def test_purpose_header_only_when_asked():
    s = FakeServer({})
    _resolve(s, turn_id="")
    assert cred_resolve.PURPOSE_HEADER not in s.requests[0][2]
    _resolve(s, turn_id="", purpose=cred_resolve.RUNNER_REFRESH)
    assert s.requests[1][2][cred_resolve.PURPOSE_HEADER] == "runner-refresh"


def test_dpop_header_signed_for_this_url_without_query(tmp_path, monkeypatch):
    key, _ = device_key.ensure(allow_software=True, system=lambda: "Linux")
    device_key.write_status({"jkt": key.jkt, "status": "active"})
    s = FakeServer({"A": SECRET}, declared=[])
    _resolve(s, refs=["A"], turn_id="")
    proofs = [h["DPoP"] for _, _, h in s.requests]
    assert len(proofs) == 2 and proofs[0] != proofs[1]          # fresh jti per request
    claims = json.loads(device_key.b64url_decode(proofs[0].split(".")[1]))
    assert claims["htu"] == "https://canopy.example/api/agents/eva/credentials/resolve"
    assert claims["htm"] == "GET"


def test_device_proof_required_says_how_to_get_approved():
    device_key.write_status({"jkt": "j", "status": "pending",
                             "approve_url": "https://canopy.example/approve/9"})
    s = FakeServer({}, status=401, body={"error": "device_proof_required"})
    with pytest.raises(cred_resolve.DeviceProofRequired) as e:
        _resolve(s, turn_id="")
    assert "https://canopy.example/approve/9" in str(e.value)
    assert isinstance(e.value, canopy_web.CanopyError)


def test_other_errors_pass_through_unchanged():
    s = FakeServer({}, status=403, body={"detail": "nope"})
    with pytest.raises(canopy_web.CanopyError) as e:
        _resolve(s, turn_id="")
    assert not isinstance(e.value, cred_resolve.DeviceProofRequired)
    assert "403" in str(e.value)
