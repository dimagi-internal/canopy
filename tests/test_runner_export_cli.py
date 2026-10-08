"""`canopy runner export` — take a session's conversation to your own Claude."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from orchestrator import canopy_web, runner_cli
from orchestrator.agent_client import CanopyError

MD = "# widget\n\n## You · 2026-10-08\n\nfix it\n"


def _wire(monkeypatch, response):
    calls = []
    monkeypatch.setattr(runner_cli, "_resolve_session",
                        lambda needle, ws="": {"id": "s1", "title": "widget"})

    def call(method, path, body=None, **kw):
        calls.append((method, path))
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(canopy_web, "call", call)
    return calls


def test_writes_the_conversation_and_prints_how_to_pick_it_up(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = _wire(monkeypatch, {"markdown": MD, "message_count": 1})

    res = CliRunner().invoke(runner_cli.runner, ["export", "widget"])

    assert res.exit_code == 0, res.output
    dest = tmp_path / ".canopy" / "exports" / "s1.md"
    assert dest.read_text() == MD
    assert calls == [("GET", "/api/canopy-sessions/s1/export")]
    assert f'claude "Read {dest}' in res.output


def test_not_the_starter_is_a_clear_refusal(monkeypatch, tmp_path):
    _wire(monkeypatch, CanopyError(
        "GET /api/canopy-sessions/s1/export -> 403: only the person who started this "
        "session can export it"))
    res = CliRunner().invoke(runner_cli.runner, ["export", "widget",
                                                 "--out", str(tmp_path / "x.md")])
    assert res.exit_code != 0 and "only the person who started" in res.output
    assert not (tmp_path / "x.md").exists()
