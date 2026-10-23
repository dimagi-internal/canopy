"""`canopy runner credential` shows a runner's Claude credentials and where to change
them — and takes no value: a runner's tokens and API keys live in a vault only its own
device key opens (canopy-web spec 2026-10-23-runner-claude-credentials-in-the-vault-design.md)."""
from __future__ import annotations

from click.testing import CliRunner

from orchestrator import canopy_web, runner_cli

RUNNER = {"id": "r1", "name": "ec2-box", "kind": "cloud", "status": "online", "workspace": "dimagi"}
STATUS = {
    "vaults": [{"vault_id": "v1", "name": "ec2-box Claude credentials", "kv": 1}],
    "credentials": [
        {"key": "a", "kind": "login", "account": "ace@example.org", "signed_in": True},
        {"key": "b", "kind": "token", "name": "hal", "in_vault": True},
        {"key": "c", "kind": "api_key", "name": "metered", "in_vault": False},
    ],
}


def test_it_lists_the_credentials_and_links_where_to_change_them(monkeypatch):
    calls = []
    monkeypatch.setattr(runner_cli, "_resolve", lambda name, ws="": RUNNER)
    monkeypatch.setattr(runner_cli, "_refuse_if_not_ours", lambda r, verb: None)
    monkeypatch.setattr(canopy_web, "resolve_base_url", lambda b: "https://canopy.example")
    monkeypatch.setattr(canopy_web, "call", lambda method, path, body=None, **kw: calls.append((method, path)) or STATUS)
    res = CliRunner().invoke(runner_cli.runner, ["credential", "ec2-box"])
    assert res.exit_code == 0, res.output
    assert calls == [("GET", "/api/harness/runners/r1/credential/status")], "it reads; it never writes"
    assert "ec2-box Claude credentials  (key version 1)" in res.output
    assert "token    hal" in res.output and "in the vault" in res.output
    assert "no value yet" in res.output
    assert "https://canopy.example/w/dimagi/runners/r1/credentials" in res.output
