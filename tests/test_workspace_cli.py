"""`canopy agents connect` (+ `canopy workspace`) — /canopy:setup's catalog step
(canopy#851, one catalog per person since 2026-10-14). canopy-web + claude mocked."""
from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from orchestrator import workspace_cli as ws

URL = "https://canopy.dimagi.com/agents/marketplace.json"
CATALOG = {"name": "canopy-agents", "owner": {"name": "canopy"},
           "plugins": [{"name": "ace", "description": "Runs Connect opportunities",
                        "source": {"source": "github", "repo": "dimagi-internal/ace"}},
                       {"name": "eva", "source": {"source": "github", "repo": "dimagi-internal/eva"}}]}
LEGACY = {"source": {"source": "url", "url": "https://canopy.dimagi.com/w/family/marketplace.json",
                     "headersHelper": "node x --archive"}, "autoUpdate": True}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    cfg = tmp_path / "claude"
    helper = cfg / "plugins" / "marketplaces" / "canopy" / ws.HELPER_REL
    helper.parent.mkdir(parents=True)
    helper.write_text("// helper")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("CANOPY_WEB_PAT", "pat_person")
    return cfg


def transport_for(status=200, body=CATALOG, seen=None):
    def t(method, url, headers, data):
        if seen is not None:
            seen.append((method, url, headers.get("Authorization")))
        return status, json.dumps(body) if not isinstance(body, str) else body
    return t


class FakeClaude:
    """Mimics Claude Code: a settings marketplace appears only after a session start."""

    def __init__(self, fail_install=()):
        self.calls, self.synced, self.fail_install = [], False, set(fail_install)

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        args = argv[1:]
        if args[:3] == ["plugin", "marketplace", "list"]:
            names = [{"name": "canopy-agents"}] if self.synced else []
            return subprocess.CompletedProcess(argv, 0, json.dumps(names), "")
        if args[:2] == ["-p", "/help"]:
            assert kw.get("stdin") == subprocess.DEVNULL
            self.synced = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[:3] == ["plugin", "marketplace", "update"]:
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        if args[:2] == ["plugin", "install"]:
            bad = args[2].split("@")[0] in self.fail_install
            assert "--yes" in args
            return subprocess.CompletedProcess(argv, 1 if bad else 0, "", "boom" if bad else "")
        raise AssertionError(argv)


def _settings(cfg):
    return json.loads((cfg / "settings.json").read_text())


def test_url_is_one_constant():
    assert ws.marketplace_url() == "https://canopy.dimagi.com" + ws.AGENTS_MARKETPLACE_PATH == URL
    assert ws.AGENTS_MARKETPLACE_NAME == "canopy-agents"


def test_connect_registers_one_catalog_installs_nothing_and_lists_agents(_cfg, capsys):
    seen, claude = [], FakeClaude()
    ws._connect(runner=claude, transport=transport_for(seen=seen))
    assert seen == [("GET", URL, "Bearer pat_person")]
    entry = _settings(_cfg)["extraKnownMarketplaces"]["canopy-agents"]
    assert entry["autoUpdate"] is True and entry["source"]["url"] == URL
    helper = entry["source"]["headersHelper"]
    assert helper.startswith("node ") and helper.endswith(" --archive")
    assert "marketplaces/canopy/plugins/canopy/scripts" in helper  # stable, not a versioned cache
    assert len(helper) <= 500 and "    " not in helper
    assert not [c for c in claude.calls if c[1:3] == ["plugin", "install"]]
    out = capsys.readouterr().out
    for needle in ("/plugin install ace@canopy-agents", "then /ace:setup",
                   "/plugin install eva@canopy-agents", "then /eva:setup",
                   "github: dimagi-internal/ace", "GitHub read access"):
        assert needle in out


def test_connect_removes_retired_per_workspace_entries_and_says_so(_cfg, capsys):
    (_cfg / "settings.json").write_text(json.dumps({
        "theme": "dark",
        "extraKnownMarketplaces": {"canopy-family": LEGACY, "other": {"source": {"url": "https://x/y.json"}}}}))
    ws._connect(runner=FakeClaude(), transport=transport_for())
    data = _settings(_cfg)
    assert data["theme"] == "dark"
    assert set(data["extraKnownMarketplaces"]) == {"other", "canopy-agents"}
    assert "removed retired per-workspace marketplace 'canopy-family'" in capsys.readouterr().out


def test_connect_is_idempotent(_cfg, capsys):
    ws._connect(runner=FakeClaude(), transport=transport_for())
    before = (_cfg / "settings.json").read_text()
    capsys.readouterr()
    ws._connect(runner=FakeClaude(), transport=transport_for())
    assert (_cfg / "settings.json").read_text() == before
    assert "already registered 'canopy-agents'" in capsys.readouterr().out


def test_install_all_installs_every_agent(_cfg):
    claude = FakeClaude()
    ws._connect(install_all=True, runner=claude, transport=transport_for())
    installs = [c for c in claude.calls if c[1:3] == ["plugin", "install"]]
    assert [c[3] for c in installs] == ["ace@canopy-agents", "eva@canopy-agents"]


def test_failed_install_names_github_access(_cfg):
    with pytest.raises(ws.WorkspaceSetupError) as e:
        ws._connect(install_all=True, runner=FakeClaude(fail_install={"eva"}), transport=transport_for())
    assert "eva" in e.value.message and "GitHub" in e.value.message


def test_dry_run_changes_nothing(_cfg, capsys):
    (_cfg / "settings.json").write_text(json.dumps({"extraKnownMarketplaces": {"canopy-family": LEGACY}}))
    claude = FakeClaude()
    ws._connect(dry_run=True, runner=claude, transport=transport_for())
    assert "canopy-agents" not in _settings(_cfg)["extraKnownMarketplaces"] and claude.calls == []
    out = capsys.readouterr().out
    assert "would remove retired per-workspace marketplace 'canopy-family'" in out
    assert "/plugin install ace@canopy-agents" in out


def test_empty_catalog_says_so(_cfg, capsys):
    ws._connect(runner=FakeClaude(), transport=transport_for(body={"name": "canopy-agents", "plugins": []}))
    assert "No agents to install yet" in capsys.readouterr().out


def test_catalog_with_another_name_is_refused(_cfg):
    with pytest.raises(ws.WorkspaceSetupError) as e:
        ws._connect(runner=FakeClaude(), transport=transport_for(body={"name": "canopy-x", "plugins": []}))
    assert "canopy-x" in e.value.message


@pytest.mark.parametrize("status,needle", [(401, "pat-mint"), (404, "agent catalog"),
                                           (410, "agent catalog"), (500, "500")])
def test_catalog_failures_explain_themselves(status, needle):
    with pytest.raises(ws.WorkspaceSetupError) as e:
        ws._connect(runner=FakeClaude(), transport=transport_for(status, "nope"))
    assert needle in e.value.message


def test_no_token_points_at_the_sign_in(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.setattr(ws.canopy_web, "TOKEN_FILE", tmp_path / "none")
    with pytest.raises(ws.WorkspaceSetupError) as e:
        ws._connect(runner=FakeClaude(), transport=transport_for())
    assert "/canopy:canopy-web-pat-mint" in e.value.message


def test_cli_agents_connect_and_retired_workspace_connect(monkeypatch):
    got = []
    monkeypatch.setattr(ws, "_connect", lambda **kw: got.append(kw))
    assert CliRunner().invoke(ws.agents_group, ["connect"]).exit_code == 0
    assert CliRunner().invoke(ws.agents_group, ["connect", "--install-all"]).exit_code == 0
    assert [kw["install_all"] for kw in got] == [False, True]
    for argv in (["connect", "family"], ["connect", "--all"], ["connect"]):
        r = CliRunner().invoke(ws.workspace_group, argv)
        assert r.exit_code != 0 and "canopy agents connect" in r.output
    assert len(got) == 2


def test_list_workspaces(monkeypatch):
    monkeypatch.setattr(ws.canopy_web, "call",
                        lambda m, p, **kw: [{"slug": "family", "name": "Family"}, {"name": "x"}])
    r = CliRunner().invoke(ws.workspace_group, ["list", "--json"])
    assert json.loads(r.output) == [{"slug": "family", "name": "Family"}]


def test_op_status_never_fails_and_names_the_fix():
    ok, line = ws.op_status(which=lambda n: None)
    assert not ok and "op signin" in line and "install" in line
    ok, line = ws.op_status(which=lambda n: "/usr/bin/op",
                            runner=lambda a, **k: subprocess.CompletedProcess(a, 0, "[]", ""))
    assert not ok and "op signin" in line
    ok, line = ws.op_status(which=lambda n: "/usr/bin/op",
                            runner=lambda a, **k: subprocess.CompletedProcess(a, 0, '[{"url":"x"}]', ""))
    assert ok
    r = CliRunner().invoke(ws.workspace_group, ["op-status"])
    assert r.exit_code == 0
