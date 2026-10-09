"""`canopy workspace` — /canopy:setup's workspace step (canopy#851). canopy-web + claude mocked."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import workspace_cli as ws

CATALOG = {"name": "canopy-family", "owner": {"name": "canopy"},
           "plugins": [{"name": "eva", "source": {"source": "archive", "url": "https://x/eva.zip"}},
                       {"name": "hal", "source": {"source": "archive", "url": "https://x/hal.zip"}}]}


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
            names = [{"name": "canopy-family"}] if self.synced else []
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


def test_connect_registers_with_autoupdate_and_the_archive_helper(_cfg):
    seen, claude = [], FakeClaude()
    ws._connect("family", runner=claude, transport=transport_for(seen=seen))
    assert seen == [("GET", "https://canopy.dimagi.com/w/family/marketplace.json", "Bearer pat_person")]
    settings = json.loads((_cfg / "settings.json").read_text())
    entry = settings["extraKnownMarketplaces"]["canopy-family"]
    assert entry["autoUpdate"] is True
    assert entry["source"]["url"] == "https://canopy.dimagi.com/w/family/marketplace.json"
    helper = entry["source"]["headersHelper"]
    assert helper.startswith("node ") and helper.endswith(" --archive")
    assert "marketplaces/canopy/plugins/canopy/scripts" in helper  # stable, not a versioned cache
    assert len(helper) <= 500 and "    " not in helper
    installs = [c for c in claude.calls if c[1:3] == ["plugin", "install"]]
    assert [c[3] for c in installs] == ["eva@canopy-family", "hal@canopy-family"]


def test_connect_preserves_other_settings_and_is_idempotent(_cfg):
    (_cfg / "settings.json").write_text(json.dumps({"theme": "dark",
                                                    "extraKnownMarketplaces": {"other": {"x": 1}}}))
    ws._connect("family", runner=FakeClaude(), transport=transport_for())
    data = json.loads((_cfg / "settings.json").read_text())
    assert data["theme"] == "dark" and "other" in data["extraKnownMarketplaces"]
    entry = data["extraKnownMarketplaces"]["canopy-family"]
    assert ws.write_user_settings("canopy-family", entry) is False


def test_dry_run_changes_nothing(_cfg):
    claude = FakeClaude()
    ws._connect("family", dry_run=True, runner=claude, transport=transport_for())
    assert not (_cfg / "settings.json").exists() and claude.calls == []


@pytest.mark.parametrize("status,needle", [(401, "pat-mint"), (404, "canopy-web#1376"), (500, "500")])
def test_catalog_failures_explain_themselves(status, needle):
    with pytest.raises(ws.WorkspaceSetupError) as e:
        ws._connect("family", runner=FakeClaude(), transport=transport_for(status, "nope"))
    assert needle in e.value.message


def test_no_token_points_at_the_sign_in(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.setattr(ws.canopy_web, "TOKEN_FILE", tmp_path / "none")
    with pytest.raises(ws.WorkspaceSetupError) as e:
        ws._connect("family", runner=FakeClaude(), transport=transport_for())
    assert "/canopy:canopy-web-pat-mint" in e.value.message


def test_failed_install_is_reported(_cfg):
    with pytest.raises(ws.WorkspaceSetupError) as e:
        ws._connect("family", runner=FakeClaude(fail_install={"hal"}), transport=transport_for())
    assert "hal" in e.value.message


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
