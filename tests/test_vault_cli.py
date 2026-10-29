"""`canopy vault` (canopy#946): an agent's credentials on canopy-web, and this machine's device key.

What is pinned:
- status lists each credential canopy-web holds for the agent (set / missing, last read) and
  this machine's device key, and no value ever reaches stdout/stderr.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner

from orchestrator import canopy_web, vault_cli

SECRETS = {"canopy-pat": "cpat_SECRET_A", "kindora-service-key": "kindora_SECRET_B"}


class FakeWeb:
    def __init__(self, *, is_admin=True, stored=None):
        self.is_admin = is_admin
        self.stored = dict(stored or {})
        self.calls = []

    def __call__(self, method, url, headers, data):
        parts = urlsplit(url)
        path, q = parts.path, parse_qs(parts.query)
        body = json.loads(data) if data else None
        self.calls.append((method, path, body))
        base = "/api/agents/eva"
        if method == "GET" and path == f"{base}/credentials/status":
            return 200, json.dumps([{"name": n, "declared": False, "set": True,
                                     "updated_at": "2026-10-16T00:00:00Z",
                                     "last_read_at": "2026-10-16T01:00:00Z"}
                                    for n in sorted(self.stored)])
        if method == "GET" and path == f"{base}/credentials/access":
            return 200, json.dumps({"agent": "eva", "may_resolve": True, "via": "admin",
                                    "reason": ""})
        if method == "GET" and path == f"{base}/credentials/resolve":
            want = q.get("refs", [""])[0].split(",") if "refs" in q else None
            vals = {k: v for k, v in self.stored.items() if want is None or k in want}
            return 200, json.dumps({"values": vals})
        return 404, "{}"


@pytest.fixture
def web(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(canopy_web, "urllib_transport", w)
    monkeypatch.setattr(vault_cli, "_token", lambda: "person-pat")
    return w


def _no_secret(text: str) -> None:
    for v in SECRETS.values():
        assert v not in text


# ── status ────────────────────────────────────────────────────────────────────

def test_status_reports_presence_and_last_read_never_values(web):
    web.stored = {"CANOPY_WEB_PAT": SECRETS["canopy-pat"], "KINDORA_SERVICE_KEY": SECRETS["kindora-service-key"]}
    st = vault_cli.run_status("eva", token="person-pat")
    rows = {r["name"]: r for r in st["refs"]}
    assert set(rows) == {"CANOPY_WEB_PAT", "KINDORA_SERVICE_KEY"}
    assert rows["CANOPY_WEB_PAT"]["present"] and rows["CANOPY_WEB_PAT"]["last_read"] == "2026-10-16T01:00:00Z"
    _no_secret(vault_cli.render_status(st) + json.dumps(st, default=str))
    assert not any(p.endswith("/credentials/resolve") for _, p, _ in web.calls)


def test_status_shows_a_pending_device_key_approve_url(web):
    from orchestrator import device_key
    device_key.write_status({"jkt": "J" * 43, "status": "pending", "kind": "secure-enclave",
                             "approve_url": "https://canopy.example/approve/3", "runner": "r"})
    st = vault_cli.run_status("eva", token="person-pat")
    assert "https://canopy.example/approve/3" in vault_cli.render_status(st)


# ── device ────────────────────────────────────────────────────────────────────

def test_device_enroll_cli(monkeypatch, tmp_path):
    from orchestrator import device_key
    from orchestrator.cli import main
    monkeypatch.setattr(vault_cli, "_token", lambda: "runner-pat")
    calls = []

    def transport(method, url, headers, data):
        body = json.loads(data) if data else None
        calls.append((method, urlsplit(url).path, body))
        return 201, json.dumps({"id": 5, "jkt": device_key.jwk_thumbprint(body["public_jwk"]),
                                "status": "pending", "approve_url": "https://c/approve/5"})

    monkeypatch.setattr(canopy_web, "urllib_transport", transport)
    r = CliRunner().invoke(main, ["vault", "device", "--enroll", "--runner", "r-9",
                                  "--software", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output.strip().splitlines()[-1])
    assert out["status"] == "pending" and out["approve_url"] == "https://c/approve/5"
    assert calls[0][:2] == ("POST", "/api/runner-device-keys/") and calls[0][2]["runner"] == "r-9"
    shown = CliRunner().invoke(main, ["vault", "device"])
    assert "pending" in shown.output and "https://c/approve/5" in shown.output


def test_device_kind_and_software_must_agree(monkeypatch):
    from orchestrator.cli import main
    monkeypatch.setattr(vault_cli, "_token", lambda: "runner-pat")
    r = CliRunner().invoke(main, ["vault", "device", "--enroll", "--runner", "r-9",
                                  "--software", "--kind", "tpm"])
    assert r.exit_code != 0 and "disagree" in r.output


def test_device_kind_tpm_off_windows_is_a_clear_error(monkeypatch):
    from orchestrator.cli import main
    monkeypatch.setattr(vault_cli, "_token", lambda: "runner-pat")
    r = CliRunner().invoke(main, ["vault", "device", "--enroll", "--runner", "r-9",
                                  "--kind", "tpm"])
    assert r.exit_code != 0 and "no TPM here" in r.output


# ── binding ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("state,want", [("on", True), ("off", False)])
def test_binding_patches_device_binding(monkeypatch, state, want):
    from orchestrator.cli import main
    monkeypatch.setattr(vault_cli, "_token", lambda: "owner-pat")
    calls = []

    def transport(method, url, headers, data):
        calls.append((method, urlsplit(url).path, json.loads(data) if data else None))
        return 200, json.dumps({"require_device_binding": want})

    monkeypatch.setattr(canopy_web, "urllib_transport", transport)
    r = CliRunner().invoke(main, ["vault", "binding", "--agent", "eva", state])
    assert r.exit_code == 0, r.output
    assert calls == [("PATCH", "/api/agents/eva/device-binding",
                      {"require_device_binding": want})]
    assert f"require_device_binding={state}" in r.output


def test_binding_refuses_a_contradicting_answer(monkeypatch):
    monkeypatch.setattr(canopy_web, "urllib_transport",
                        lambda *a: (200, json.dumps({"require_device_binding": False})))
    with pytest.raises(vault_cli.VaultCliError, match="expected True"):
        vault_cli.run_binding("eva", True, token="t")


# ── runner-session: a runner signs in with its device key, never a PAT ────────────

def test_runner_session_sends_only_the_device_proof(monkeypatch):
    """canopy-web #1594's contract: the runner id in the body, the proof for THIS
    url, no Authorization header at all."""
    from orchestrator import device_key

    monkeypatch.setattr(device_key, "proof_headers",
                        lambda method, url: {"DPoP": f"proof-for:{method}:{url}"})
    sent = {}

    def transport(method, url, headers, body):
        sent.update(method=method, url=url, headers=headers, body=json.loads(body))
        return 201, json.dumps({"access_token": "crt_x", "runner": "r1",
                                "expires_at": "2099-01-01T00:00:00Z"})

    out = vault_cli.run_runner_session("r1", "https://canopy.test", transport=transport)
    assert out["access_token"] == "crt_x"
    assert sent["url"] == "https://canopy.test/api/runner-sessions/"
    assert sent["body"] == {"runner": "r1"}
    assert sent["headers"]["DPoP"] == "proof-for:POST:https://canopy.test/api/runner-sessions/"
    assert "Authorization" not in sent["headers"]


def test_runner_session_without_a_key_or_approval_is_a_clear_error(monkeypatch):
    from orchestrator import device_key

    monkeypatch.setattr(device_key, "proof_headers", lambda method, url: {})
    with pytest.raises(vault_cli.VaultCliError, match="no registered device key"):
        vault_cli.run_runner_session("r1", "https://canopy.test")

    monkeypatch.setattr(device_key, "proof_headers", lambda method, url: {"DPoP": "p"})
    with pytest.raises(vault_cli.VaultCliError, match="approved"):
        vault_cli.run_runner_session("r1", "https://canopy.test",
                                     transport=lambda *a: (401, '{"title": "Device proof refused"}'))


def test_runner_session_cli_prints_the_token_json(monkeypatch):
    monkeypatch.setattr(vault_cli, "run_runner_session",
                        lambda runner_id, base_url: {"access_token": "crt_x", "runner": runner_id})
    res = CliRunner().invoke(vault_cli.vault_group, ["runner-session", "--runner", "r9"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == {"access_token": "crt_x", "runner": "r9"}
