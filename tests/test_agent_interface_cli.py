"""`canopy agent interface` publishes config/interface.yaml as-is; canopy-web validates."""
from click.testing import CliRunner

from orchestrator import agent_cli


def test_publishes_the_repo_file(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "interface.yaml").write_text(
        "capabilities:\n  ask:\n    callers: [member]\ncallers_default: none\n", encoding="utf-8")
    sent = {}

    class Fake:
        def put_interface(self, doc):
            sent["doc"] = doc
            return {"interface": doc}

    monkeypatch.setattr(agent_cli, "_client", lambda slug, **k: Fake())
    r = CliRunner().invoke(agent_cli.agent, ["interface", "--slug", "ace", "--repo", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert sent["doc"] == {"capabilities": {"ask": {"callers": ["member"]}}, "callers_default": "none"}


def test_a_missing_file_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cli, "_client", lambda slug, **k: None)
    r = CliRunner().invoke(agent_cli.agent, ["interface", "--slug", "ace", "--repo", str(tmp_path)])
    assert r.exit_code != 0 and "interface.yaml" in r.output
