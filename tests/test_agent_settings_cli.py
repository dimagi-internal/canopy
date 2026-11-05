"""`canopy agent read-mode` and `canopy agent sites`: the settings live on canopy-web, not in the repo."""
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from orchestrator import agent_cli
from orchestrator.agent_client import AgentClient


class Fake:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, *args))
            return {"called": name}
        return call


@pytest.fixture()
def fake(monkeypatch):
    client = Fake()
    monkeypatch.setattr(agent_cli, "_client", lambda slug, **k: client)
    return client


def _run(*args):
    return CliRunner().invoke(agent_cli.agent, list(args))


def test_read_mode_get_and_reset(fake):
    assert json.loads(_run("read-mode", "get", "--slug", "ace").output) == {"called": "get_read_mode"}
    assert _run("read-mode", "reset", "--slug", "ace").exit_code == 0
    assert fake.calls == [("get_read_mode",), ("reset_read_mode",)]


def test_read_mode_set_sends_the_files_fields(tmp_path, fake):
    f = tmp_path / "anywhere.yaml"
    f.write_text("description: Ask ACE.\ntools: [Read]\n", encoding="utf-8")
    r = _run("read-mode", "set", "--slug", "ace", "--file", str(f), "--dry-run")
    assert r.exit_code == 0, r.output
    assert fake.calls == [("put_read_mode", {"description": "Ask ACE.", "tools": ["Read"], "dry_run": True})]


def test_a_new_site_needs_no_file(fake):
    assert _run("sites", "set", "--slug", "ace", "--site", "connect-labs").exit_code == 0
    assert fake.calls == [("put_connected_site", "connect-labs", {"dry_run": False})]


def test_sites_set_list_and_remove(tmp_path, fake):
    f = tmp_path / "row.json"
    f.write_text(json.dumps({"sensitive": True}), encoding="utf-8")
    assert _run("sites", "set", "--slug", "ace", "--site", "s", "--file", str(f)).exit_code == 0
    assert _run("sites", "list", "--slug", "ace").exit_code == 0
    assert _run("sites", "remove", "--slug", "ace", "--site", "s").exit_code == 0
    assert fake.calls == [("put_connected_site", "s", {"sensitive": True, "dry_run": False}),
                          ("list_connected_sites",), ("delete_connected_site", "s")]


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path, fake):
    f = tmp_path / "list.yaml"
    f.write_text("- Read\n", encoding="utf-8")
    r = _run("read-mode", "set", "--slug", "ace", "--file", str(f))
    assert r.exit_code != 0 and "mapping" in r.output
    assert fake.calls == []


def test_the_client_calls_the_routes_canopy_web_publishes():
    sent = []
    client = AgentClient.__new__(AgentClient)
    client.identity = SimpleNamespace(slug="ace")
    client._call = lambda method, path, body=None: sent.append((method, path, body)) or {}
    client.get_read_mode()
    client.put_read_mode({"tools": []})
    client.reset_read_mode()
    client.list_connected_sites()
    client.put_connected_site("s", {"sensitive": True})
    client.delete_connected_site("s")
    assert sent == [("GET", "/api/agents/ace/read-mode", None), ("PUT", "/api/agents/ace/read-mode", {"tools": []}),
                    ("DELETE", "/api/agents/ace/read-mode", None), ("GET", "/api/agents/ace/connected-sites", None),
                    ("PUT", "/api/agents/ace/connected-sites/s", {"sensitive": True}),
                    ("DELETE", "/api/agents/ace/connected-sites/s", None)]
