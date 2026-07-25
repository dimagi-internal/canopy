# tests/test_cli_harvest_intent_audit.py
"""CLI tests for `canopy harvest intent-audit` (SP3 Task 3)."""
import json

from click.testing import CliRunner

from orchestrator.cli import main


def _session(tmp_path):
    j = tmp_path / "s.jsonl"
    j.write_text(
        '{"type":"user","message":{"content":"approve the broad option"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"shipped the narrow one"}]}}\n'
    )
    return j


def _findings():
    good = {
        "title": "approved broad, shipped narrow", "friction_type": "intent_miss",
        "fix_kind": "skill_edit", "target": "skills/x",
        "recommendation": "honor the approved scope",
        "evidence": {
            "source_ref": "you: 'approve the broad option'", "was_read": True,
            "already_fixed_check": {"ran": True, "result": "live on main"},
            "confidence": "high",
            "confidence_basis": "verbatim quote diverges from the shipped narrow filter",
        },
    }
    bad = {"title": "vibes", "friction_type": "intent_miss", "evidence": "I feel you wanted more"}
    return good, bad


def test_intent_audit_cli_qualified_and_dropped(tmp_path, monkeypatch):
    from orchestrator import harvest

    j = _session(tmp_path)
    good, bad = _findings()
    monkeypatch.setattr(harvest, "_run_intent_llm", lambda *a, **k: ([good, bad], None))

    r = CliRunner().invoke(main, ["harvest", "intent-audit", str(j)])

    assert r.exit_code == 0, r.output
    assert "Qualified (1)" in r.output
    assert "Dropped (1)" in r.output
    assert "approved broad, shipped narrow" in r.output


def test_intent_audit_cli_json(tmp_path, monkeypatch):
    from orchestrator import harvest

    j = _session(tmp_path)
    good, bad = _findings()
    monkeypatch.setattr(harvest, "_run_intent_llm", lambda *a, **k: ([good, bad], None))

    r = CliRunner().invoke(main, ["harvest", "intent-audit", str(j), "--json-output"])

    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert len(out["qualified"]) == 1
    assert len(out["dropped"]) == 1


def test_intent_audit_cli_no_llm(tmp_path):
    j = _session(tmp_path)

    r = CliRunner().invoke(main, ["harvest", "intent-audit", str(j), "--no-llm"])

    assert r.exit_code == 0, r.output
    assert "Qualified (0)" in r.output
