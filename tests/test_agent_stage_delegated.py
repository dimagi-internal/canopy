"""`canopy agent stage-delegated`: a laptop stages what an agent BORROWS (canopy-web#1291).

Pinned: the borrowed Salesforce credential lands at the path chrome-sales reads in
agent sessions (0600); a withdrawn loan removes the staged copy; the credential is
never printed.
"""
import json
import stat

from click.testing import CliRunner

from orchestrator import agent_cli, canopy_web

CREDS = json.dumps({"clientId": "PlatformCLI", "refreshToken": "SECRET-r1", "instanceUrl": "https://x"})


def _run(monkeypatch, tmp_path, creds):
    monkeypatch.setattr(agent_cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(canopy_web, "call", lambda *a, **k: {"salesforce_creds": creds})
    monkeypatch.setattr("orchestrator.agent_bootstrap._operator_token", lambda: "pat")
    return CliRunner().invoke(agent_cli.agent, ["stage-delegated", "--slug", "hal"])


def test_stages_the_borrowed_credential_privately(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, CREDS)
    assert r.exit_code == 0, r.output
    f = tmp_path / ".canopy" / "delegated" / "hal" / "chrome-sales" / ".sf-creds.json"
    assert json.loads(f.read_text())["refreshToken"] == "SECRET-r1"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert "SECRET" not in r.output


def test_a_withdrawn_loan_removes_the_staged_copy(monkeypatch, tmp_path):
    f = tmp_path / ".canopy" / "delegated" / "hal" / "chrome-sales" / ".sf-creds.json"
    f.parent.mkdir(parents=True)
    f.write_text(CREDS)
    r = _run(monkeypatch, tmp_path, "")
    assert r.exit_code == 0 and not f.exists()
    assert "removed the stale staged copy" in r.output


def test_skips_when_the_only_token_is_the_agents_own(monkeypatch, tmp_path):
    """canopy#945: asking /credentials/resolve with the agent's own PAT can only 403 —
    skip with a clear line, and leave a copy someone else staged alone."""
    (tmp_path / ".hal").mkdir()
    (tmp_path / ".hal" / ".env").write_text("CANOPY_WEB_PAT=hal-own\n")
    f = tmp_path / ".canopy" / "delegated" / "hal" / "chrome-sales" / ".sf-creds.json"
    f.parent.mkdir(parents=True)
    f.write_text(CREDS)
    monkeypatch.setattr(agent_cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(canopy_web, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setattr("orchestrator.agent_bootstrap._operator_token", lambda: "hal-own")
    r = CliRunner().invoke(agent_cli.agent, ["stage-delegated", "--slug", "hal"])
    assert r.exit_code == 0, r.output
    assert "skipped" in r.output and f.exists()
