"""`canopy vault runner-proof`: a proof, for a HOST SITE, that this machine's approved
device key made one request. Pinned against the contract canopy-web's SDK publishes
(`canopy_sdk.runner_attestations.bound_url`): the two sides compute the same `htu`."""
from __future__ import annotations

import hashlib
import json

import pytest
from click.testing import CliRunner

from orchestrator import device_key as dk
from orchestrator import vault_cli

URL = "https://labs.example.org/mcp/admin/agent-requests/?x=1"
BODY = b'{"reason":"r","recipient_key":"k"}'


@pytest.fixture
def keydir(tmp_path, monkeypatch):
    monkeypatch.setenv(dk.DIR_ENV, str(tmp_path / "dk"))
    monkeypatch.setenv(dk.HELPER_ENV, "/nonexistent")


def test_the_bound_htu_is_the_endpoint_plus_a_digest_of_the_body():
    # canopy_sdk.runner_attestations.bound_url, written out: the two must never drift.
    digest = hashlib.sha256(BODY).hexdigest()
    assert dk.bound_htu(URL, BODY) == f"https://labs.example.org/mcp/admin/agent-requests/attest/{digest}"
    assert dk.bound_htu("https://labs.example.org/mcp/admin/agent-requests", BODY) == dk.bound_htu(URL, BODY)
    assert dk.bound_htu(URL, BODY + b" ") != dk.bound_htu(URL, BODY)


def test_a_registered_key_signs_the_bound_url(keydir):
    key, _ = dk.ensure(allow_software=True, system=lambda: "Linux")
    dk.write_status({"jkt": key.jkt, "status": "active"})
    name, value = vault_cli.run_runner_proof(URL, BODY)
    assert name == "X-Canopy-Runner-Proof"
    kind, _, jws = value.partition(" ")
    assert kind == "dpop"
    claims = json.loads(dk.b64url_decode(jws.split(".")[1]))
    assert claims["htm"] == "POST" and claims["htu"] == dk.bound_htu(URL, BODY)


def test_no_registered_key_says_why(keydir):
    with pytest.raises(vault_cli.VaultCliError, match="no enrolled device key"):
        vault_cli.run_runner_proof(URL, BODY)


def test_an_instance_role_proof_is_sent_as_aws_iam(keydir, monkeypatch):
    monkeypatch.setattr(dk, "proof_or_reason", lambda m, u, **k: ({"X-Canopy-Aws-Proof": "b64"}, ""))
    assert vault_cli.run_runner_proof(URL, BODY) == ("X-Canopy-Runner-Proof", "aws-iam b64")


def test_the_command_prints_one_header_line(keydir, tmp_path):
    key, _ = dk.ensure(allow_software=True, system=lambda: "Linux")
    dk.write_status({"jkt": key.jkt, "status": "active"})
    body = tmp_path / "body.json"
    body.write_bytes(BODY)
    res = CliRunner().invoke(vault_cli.vault_group, ["runner-proof", "--url", URL, "--body-file", str(body)])
    assert res.exit_code == 0, res.output
    assert res.output.startswith("X-Canopy-Runner-Proof: dpop ") and res.output.count("\n") == 1
