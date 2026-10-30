"""`canopy agent env` — bind an install to ONE agent instance, fill its .env from that
instance's Canopy vault (Jonathan, 2026-10-14). canopy-web is faked."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import agent_instance as ai


def _inst(agent, ws="connect", plugin="ace"):
    return ai.Instance(plugin=plugin, agent=agent, workspace=ws)


class FakeVault:
    """canopy_vault_env stand-in: records the call, writes a two-var env."""

    def __init__(self):
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, slug, out):
        self.calls.append((slug, Path(out)))
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("A=1\nB=2\n")
        return 2


def _run(tmp_path, instances, *, vault=None, **kw):
    vault = vault or FakeVault()
    res = ai.run("ace", out=tmp_path / "home" / ".ace" / ".env", token="pat",
                 list_instances=lambda p: instances, from_canopy_vault=vault, **kw)
    return res, vault


# ── instance resolution ──────────────────────────────────────────────────────

def test_one_instance_is_used_and_named(tmp_path):
    res, vault = _run(tmp_path, [_inst("ace")])
    assert res.instance.agent == "ace" and "the only instance" in res.why
    assert vault.calls == [("ace", tmp_path / "home" / ".ace" / ".env")]
    assert (tmp_path / "home" / ".ace" / ".env").read_text() == "A=1\nB=2\n"
    assert res.lines == 2


def test_many_instances_stop_with_a_rerun_line_each(tmp_path):
    with pytest.raises(ai.InstanceError) as e:
        _run(tmp_path, [_inst("ace"), _inst("ace-sandbox", ws="dimagi")])
    msg = e.value.message
    assert "2 instances" in msg
    assert "canopy agent env --plugin ace --agent ace" in msg
    assert "canopy agent env --plugin ace --agent ace-sandbox" in msg
    assert not (tmp_path / "home" / ".ace" / ai.BINDING_NAME).exists()


def test_agent_flag_picks_one_of_many(tmp_path):
    res, vault = _run(tmp_path, [_inst("ace"), _inst("ace-sandbox")], agent="ace-sandbox")
    assert res.instance.agent == "ace-sandbox" and vault.calls[0][0] == "ace-sandbox"


def test_agent_flag_naming_an_invisible_instance_errors(tmp_path):
    with pytest.raises(ai.InstanceError, match="'nope' is not an instance"):
        _run(tmp_path, [_inst("ace")], agent="nope")


def test_no_instance_stops(tmp_path):
    with pytest.raises(ai.InstanceError, match="no instance of plugin 'ace' is visible"):
        _run(tmp_path, [])


def test_the_bound_instance_is_kept_among_many(tmp_path):
    _run(tmp_path, [_inst("ace-sandbox")])            # bind once
    res, _ = _run(tmp_path, [_inst("ace"), _inst("ace-sandbox")])
    assert res.instance.agent == "ace-sandbox" and "bound to" in res.why


def test_catalog_and_agents_list_parsing_tolerates_old_server_fields():
    """An older canopy-web still sends a per-instance `vault` / `op_vault`: ignored."""
    catalog = {"plugins": [
        {"name": "hal", "metadata": {"instances": [{"agent": "hal", "workspace": "dimagi",
                                                    "vault": "Hal-V"}]}},
        {"name": "ace", "metadata": {"instances": [{"agent": "ace", "workspace": "connect"}]}},
    ]}
    assert ai.instances_from_catalog(catalog, "hal") == [ai.Instance("hal", "hal", "dimagi")]
    assert ai.instances_from_catalog(catalog, "ace") == [ai.Instance("ace", "ace", "connect")]
    assert ai.instances_from_catalog(catalog, "eva") is None
    rows = {"items": [{"slug": "eva", "workspace": "dimagi", "op_vault": "Eva-V",
                       "plugin": {"repo": "dimagi-internal/eva", "name": "eva"}},
                      {"slug": "x", "plugin": None}]}
    assert ai.instances_from_agents(rows, "eva") == [ai.Instance("eva", "eva", "dimagi")]


# ── binding ──────────────────────────────────────────────────────────────────

def test_binding_recorded_and_rebind_refused_without_force(tmp_path):
    _run(tmp_path, [_inst("ace")])
    b = json.loads((tmp_path / "home" / ".ace" / ai.BINDING_NAME).read_text())
    assert {k: b[k] for k in ("plugin", "agent", "workspace")} == \
        {"plugin": "ace", "agent": "ace", "workspace": "connect"}
    assert b["bound_at"] and "vault" not in b
    vault = FakeVault()
    with pytest.raises(ai.InstanceError, match="refusing to switch.*--force"):
        _run(tmp_path, [_inst("ace-sandbox")], vault=vault)
    assert vault.calls == []          # nothing written on a refused re-bind
    res, _ = _run(tmp_path, [_inst("ace-sandbox")], force=True)
    assert any("re-binding" in n for n in res.notes)
    assert json.loads((tmp_path / "home" / ".ace" / ai.BINDING_NAME).read_text())["agent"] == \
        "ace-sandbox"


def test_print_resolves_without_writing(tmp_path):
    res, vault = _run(tmp_path, [_inst("ace")], print_only=True)
    assert res.instance.agent == "ace" and vault.calls == []
    assert not (tmp_path / "home" / ".ace").exists()


# ── the Canopy vault ─────────────────────────────────────────────────────────

def test_canopy_vault_refusal_points_at_agent_use(tmp_path, monkeypatch):
    from orchestrator import cred_cli

    def refuse(slug, refs=None):
        raise cred_cli.CredError("could not resolve 'ace's credentials from canopy-web: 403")

    monkeypatch.setattr(cred_cli, "fetch_canopy_web_values", refuse)
    with pytest.raises(ai.InstanceError) as e:
        ai.canopy_vault_env("ace", tmp_path / ".env")
    assert "canopy agent use ace" in e.value.message


# ── the CLI ──────────────────────────────────────────────────────────────────

def test_cli_fills_from_the_canopy_vault_and_prints(tmp_path, monkeypatch):
    from orchestrator import agent_cli, workspace_cli
    monkeypatch.setattr(workspace_cli, "person_token", lambda: "pat")
    monkeypatch.setattr(ai, "fetch_instances", lambda p, token: [_inst("ace")])
    monkeypatch.setattr(ai, "canopy_vault_env", lambda slug, out: 3)
    out = tmp_path / "h" / ".env"
    r = CliRunner().invoke(agent_cli.agent, ["env", "--plugin", "ace", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "instance: ace (workspace connect)" in r.output
    assert "(3 vars) from the Canopy vault" in r.output
    r = CliRunner().invoke(agent_cli.agent, ["env", "--plugin", "ace", "--out", str(out), "--print"])
    assert r.exit_code == 0 and json.loads(r.output)["agent"] == "ace"


def test_cli_still_accepts_the_legacy_template_and_vault_flags(tmp_path, monkeypatch):
    """Agent repos' setup scripts written before the Canopy vault was the only source pass
    --template/--vault; they are ignored, not rejected."""
    from orchestrator import agent_cli, workspace_cli
    monkeypatch.setattr(workspace_cli, "person_token", lambda: "pat")
    monkeypatch.setattr(ai, "fetch_instances", lambda p, token: [_inst("ace")])
    monkeypatch.setattr(ai, "canopy_vault_env", lambda slug, out: 1)
    r = CliRunner().invoke(agent_cli.agent, ["env", "--plugin", "ace", "--template", ".env.tpl",
                                             "--vault", "Anything", "--out", str(tmp_path / ".env")])
    assert r.exit_code == 0, r.output
