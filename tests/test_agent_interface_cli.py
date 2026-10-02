"""`canopy agent interface get|set` — the interface lives on canopy-web, not in the repo."""
import json

from click.testing import CliRunner

from orchestrator import agent_cli


class Fake:
    def __init__(self, body=None):
        self.body, self.sent = body or {}, None

    def get_interface(self):
        return self.body



def test_set_points_at_the_browser_instead_of_sending(tmp_path, monkeypatch):
    """canopy-web refuses an interface from a token (`@human_only`), so `set`
    must not send one: it says where to publish it, and fails so a script notices."""
    f = tmp_path / "anywhere.yaml"
    f.write_text("# kept\nfull: [contact@dimagi.com:verified]\n", encoding="utf-8")
    fake = Fake()
    monkeypatch.setattr(agent_cli, "_client", lambda slug, **k: fake)
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://canopy.example/canopy")
    r = CliRunner().invoke(agent_cli.agent, ["interface", "set", "--slug", "ace", "--file", str(f)])
    assert r.exit_code != 0
    assert "https://canopy.example/canopy/agents/ace/settings" in r.output
    assert fake.sent is None, "nothing may be sent with a token"


def test_get_prints_the_saved_yaml(monkeypatch):
    monkeypatch.setattr(agent_cli, "_client", lambda slug, **k: Fake({"source": "full: []\n"}))
    r = CliRunner().invoke(agent_cli.agent, ["interface", "get", "--slug", "ace"])
    assert r.output.strip() == "full: []"


def test_get_falls_back_to_the_parsed_form(monkeypatch):
    monkeypatch.setattr(agent_cli, "_client",
                        lambda slug, **k: Fake({"source": "", "interface": {"full": ["member"]}}))
    r = CliRunner().invoke(agent_cli.agent, ["interface", "get", "--slug", "ace"])
    assert json.loads(r.output) == {"full": ["member"]}


def test_there_is_no_repo_default(tmp_path, monkeypatch):
    """No --file means no guess at a repo path: the repo does not hold this."""
    monkeypatch.setattr(agent_cli, "_client", lambda slug, **k: Fake())
    r = CliRunner().invoke(agent_cli.agent, ["interface", "set", "--slug", "ace"])
    assert r.exit_code != 0 and "--file" in r.output
