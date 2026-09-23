"""`canopy agent mode --caller`: THIS turn's mode wins over the agent-wide switch.

canopy-web decides the mode per turn when it is claimed (a routing rule such as
"email from beth -> auto") and writes it into the caller envelope. Every case
where the envelope cannot say falls back to the agent-wide read — the behaviour
before per-turn modes — so an older runner or canopy-web changes nothing.
"""
import json

from click.testing import CliRunner

from orchestrator import agent_cli


def _envelope(tmp_path, turn_mode):
    p = tmp_path / "caller.json"
    env = {"version": 1, "turn_id": "t-1"}
    if turn_mode is not None:
        env["turn_mode"] = turn_mode
    p.write_text(json.dumps(env))
    return str(p)


def _run(monkeypatch, args, agent_wide="manual"):
    calls = []

    class Fake:
        def turn_mode(self):
            calls.append("agent")
            return agent_wide

    monkeypatch.setattr(agent_cli, "_client", lambda slug, **_: Fake())
    r = CliRunner().invoke(agent_cli.agent, ["mode", "--slug", "eva", *args])
    assert r.exit_code == 0, r.output
    return json.loads(r.output), calls


def test_the_turns_mode_wins_and_canopy_web_is_not_asked(tmp_path, monkeypatch):
    path = _envelope(tmp_path, {"mode": "auto", "basis": "rule email/beth@dimagi.com"})
    out, calls = _run(monkeypatch, ["--caller", path], agent_wide="manual")
    assert out == {"slug": "eva", "turn_mode": "auto",
                   "basis": "rule email/beth@dimagi.com", "source": "turn"}
    assert calls == []


def test_a_withheld_auto_reads_manual(tmp_path, monkeypatch):
    path = _envelope(tmp_path, {"mode": "manual",
                                "basis": "rule email/beth@dimagi.com: auto withheld, message not verified"})
    out, _ = _run(monkeypatch, ["--caller", path], agent_wide="auto")
    assert out["turn_mode"] == "manual"
    assert "withheld" in out["basis"]


def test_no_caller_reads_the_agent_wide_switch(monkeypatch):
    out, calls = _run(monkeypatch, [], agent_wide="auto")
    assert out == {"slug": "eva", "turn_mode": "auto", "basis": "agent", "source": "agent"}
    assert calls == ["agent"]


def test_an_envelope_from_an_older_canopy_web_falls_back(tmp_path, monkeypatch):
    out, calls = _run(monkeypatch, ["--caller", _envelope(tmp_path, None)])
    assert out["source"] == "agent" and calls == ["agent"]


def test_a_missing_or_garbled_envelope_falls_back(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    for path in (str(tmp_path / "absent.json"), str(bad)):
        out, _ = _run(monkeypatch, ["--caller", path])
        assert out["source"] == "agent"


def test_an_unknown_mode_is_not_trusted(tmp_path, monkeypatch):
    """Nothing silently resolves TO auto: an unrecognised value falls back."""
    out, _ = _run(monkeypatch, ["--caller", _envelope(tmp_path, {"mode": "yolo"})])
    assert out["source"] == "agent"
