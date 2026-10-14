"""`canopy agent env` — bind an install to ONE agent instance, fill its .env from that
instance's vault (Jonathan, 2026-10-14). canopy-web and `op` are faked."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import agent_instance as ai

ROOT = Path(__file__).resolve().parents[1]


def _inst(agent, ws="connect", vault="V", plugin="ace"):
    return ai.Instance(plugin=plugin, agent=agent, workspace=ws, vault=vault)


class FakeOp:
    def __init__(self, rc=0, stderr="", missing=False):
        self.rc, self.stderr, self.missing = rc, stderr, missing
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kw):
        if self.missing:
            raise FileNotFoundError("op")
        self.calls.append((list(argv), kw.get("env") or {}))
        if self.rc == 0:
            Path(argv[argv.index("-o") + 1]).write_text("A=1\nB=2\n")
        return subprocess.CompletedProcess(argv, self.rc, "", self.stderr)


@pytest.fixture()
def tpl(tmp_path):
    p = tmp_path / ".env.tpl"
    p.write_text("A=op://${AGENT_VAULT}/a/credential\nB=op://$AGENT_VAULT/b/credential\n")
    return p


def _run(tmp_path, tpl, instances, *, op=None, fetch=None, **kw):
    op = op or FakeOp()
    res = ai.run("ace", template=tpl, out=tmp_path / "home" / ".ace" / ".env", token="pat",
                 list_instances=lambda p: instances,
                 fetch_vault=fetch or (lambda s: pytest.fail("fetched without need")),
                 runner=op, base_env={"PATH": "/bin"}, **kw)
    return res, op


# ── instance resolution ──────────────────────────────────────────────────────

def test_one_instance_is_used_and_named(tmp_path, tpl):
    res, op = _run(tmp_path, tpl, [_inst("ace", vault="Ace-Vault")])
    assert res.instance.agent == "ace" and "the only instance" in res.why
    assert res.vault == "Ace-Vault" and res.vault_source == "canopy-web"
    argv, env = op.calls[0]
    assert argv[:5] == ["op", "inject", "-f", "-i", str(tpl)]
    assert env["AGENT_VAULT"] == "Ace-Vault" and env["AGENT_SLUG"] == "ace"
    assert (tmp_path / "home" / ".ace" / ".env").read_text() == "A=1\nB=2\n"
    assert res.lines == 2


def test_many_instances_stop_with_a_rerun_line_each(tmp_path, tpl):
    with pytest.raises(ai.InstanceError) as e:
        _run(tmp_path, tpl, [_inst("ace"), _inst("ace-sandbox", ws="dimagi")])
    msg = e.value.message
    assert "2 instances" in msg
    assert "canopy agent env --plugin ace --agent ace --template" in msg
    assert "canopy agent env --plugin ace --agent ace-sandbox --template" in msg
    assert not (tmp_path / "home" / ".ace" / ai.BINDING_NAME).exists()


def test_agent_flag_picks_one_of_many(tmp_path, tpl):
    res, _ = _run(tmp_path, tpl, [_inst("ace"), _inst("ace-sandbox", vault="SB")],
                  agent="ace-sandbox")
    assert res.instance.agent == "ace-sandbox" and res.vault == "SB"


def test_agent_flag_naming_an_invisible_instance_errors(tmp_path, tpl):
    with pytest.raises(ai.InstanceError, match="'nope' is not an instance"):
        _run(tmp_path, tpl, [_inst("ace")], agent="nope")


def test_no_instance_stops(tmp_path, tpl):
    with pytest.raises(ai.InstanceError, match="no instance of plugin 'ace' is visible"):
        _run(tmp_path, tpl, [])


def test_the_bound_instance_is_kept_among_many(tmp_path, tpl):
    _run(tmp_path, tpl, [_inst("ace-sandbox", vault="SB")])            # bind once
    res, _ = _run(tmp_path, tpl, [_inst("ace"), _inst("ace-sandbox", vault="SB")])
    assert res.instance.agent == "ace-sandbox" and "bound to" in res.why


def test_catalog_and_agents_list_parsing():
    catalog = {"plugins": [
        {"name": "hal", "metadata": {"instances": [{"agent": "hal", "workspace": "dimagi",
                                                    "vault": "Hal-V"}]}},
        {"name": "ace", "metadata": {"instances": [{"agent": "ace", "workspace": "connect"}]}},
    ]}
    assert ai.instances_from_catalog(catalog, "hal") == [
        ai.Instance("hal", "hal", "dimagi", "Hal-V")]
    # an older catalog carries no vault → None (fetch the agent record), not ""
    assert ai.instances_from_catalog(catalog, "ace")[0].vault is None
    assert ai.instances_from_catalog(catalog, "eva") is None
    rows = {"items": [{"slug": "eva", "workspace": "dimagi", "op_vault": "",
                       "plugin": {"repo": "dimagi-internal/eva", "name": "eva"}},
                      {"slug": "x", "plugin": None}]}
    assert ai.instances_from_agents(rows, "eva") == [ai.Instance("eva", "eva", "dimagi", "")]


# ── vault ────────────────────────────────────────────────────────────────────

def test_vault_flag_wins_over_the_record(tmp_path, tpl):
    res, op = _run(tmp_path, tpl, [_inst("ace", vault="Recorded")], vault="Mine")
    assert res.vault == "Mine" and res.vault_source == "--vault"
    assert op.calls[0][1]["AGENT_VAULT"] == "Mine"


def test_vault_fetched_from_the_agent_record_when_catalog_lacks_it(tmp_path, tpl):
    res, _ = _run(tmp_path, tpl, [_inst("ace", vault=None)], fetch=lambda s: "From-Detail")
    assert res.vault == "From-Detail"


def test_missing_vault_errors_and_never_guesses(tmp_path, tpl):
    with pytest.raises(ai.InstanceError) as e:
        _run(tmp_path, tpl, [_inst("ace", vault="")])
    assert ai.NO_VAULT in e.value.message
    with pytest.raises(ai.InstanceError, match="no 1Password vault recorded"):
        _run(tmp_path, tpl, [_inst("ace", vault=None)], fetch=lambda s: "")


# ── binding ──────────────────────────────────────────────────────────────────

def test_binding_recorded_and_rebind_refused_without_force(tmp_path, tpl):
    _run(tmp_path, tpl, [_inst("ace", vault="A")])
    b = json.loads((tmp_path / "home" / ".ace" / ai.BINDING_NAME).read_text())
    assert {k: b[k] for k in ("plugin", "agent", "workspace", "vault")} == \
        {"plugin": "ace", "agent": "ace", "workspace": "connect", "vault": "A"}
    assert b["bound_at"]
    op = FakeOp()
    with pytest.raises(ai.InstanceError, match="refusing to switch.*--force"):
        _run(tmp_path, tpl, [_inst("ace-sandbox", vault="SB")], op=op)
    assert op.calls == []          # nothing injected on a refused re-bind
    res, _ = _run(tmp_path, tpl, [_inst("ace-sandbox", vault="SB")], force=True)
    assert any("re-binding" in n for n in res.notes)
    assert json.loads((tmp_path / "home" / ".ace" / ai.BINDING_NAME).read_text())["agent"] == \
        "ace-sandbox"


def test_print_resolves_without_writing(tmp_path, tpl):
    op = FakeOp()
    res, _ = _run(tmp_path, tpl, [_inst("ace", vault="A")], op=op, print_only=True)
    assert res.vault == "A" and op.calls == []
    assert not (tmp_path / "home" / ".ace").exists()


# ── op failures ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("op", [
    FakeOp(missing=True),
    FakeOp(rc=1, stderr="[ERROR] You are not currently signed in."),
    FakeOp(rc=1, stderr='[ERROR] "Ace-Vault" isn\'t a vault in this account'),
])
def test_op_failures_name_the_vault_account_and_owner(tmp_path, tpl, op):
    with pytest.raises(ai.InstanceError) as e:
        _run(tmp_path, tpl, [_inst("ace", vault="Ace-Vault")], op=op)
    msg = e.value.message
    assert "'Ace-Vault'" in msg and "dimagi.1password.com" in msg
    assert "ask the agent's owner for access" in msg
    assert not (tmp_path / "home" / ".ace" / ".env").exists()
    assert not (tmp_path / "home" / ".ace" / ai.BINDING_NAME).exists()


def test_literal_vault_template_warns(tmp_path):
    t = tmp_path / ".env.tpl"
    t.write_text("A=op://Agent-Ace/a/credential\n")
    res, _ = _run(tmp_path, t, [_inst("ace", vault="Other")])
    assert any("Agent-Ace" in n and "${AGENT_VAULT}" in n for n in res.notes)


def test_instance_vault_binding_then_record_then_error(tmp_path):
    home = tmp_path
    assert ai.instance_vault("ace", home=home, fetch=lambda s: "Rec") == "Rec"
    (home / ".ace").mkdir()
    (home / ".ace" / ai.BINDING_NAME).write_text(json.dumps({"agent": "ace", "vault": "Bound"}))
    assert ai.instance_vault("ace", home=home, fetch=lambda s: pytest.fail("no fetch")) == "Bound"
    with pytest.raises(ai.InstanceError, match="no 1Password vault recorded"):
        ai.instance_vault("hal", home=home, fetch=lambda s: "")


def test_fetch_agent_vault_reads_the_record_then_the_vault_status(monkeypatch):
    calls = []

    def fake(method, path, **kw):
        calls.append(path)
        if path == "/api/agents/new/":
            return {"slug": "new", "op_vault": "From-Record"}
        if path == "/api/agents/old/":
            return {"slug": "old"}                     # a canopy-web without op_vault yet
        if path == "/api/agents/old/vault":
            return {"vault": "From-Status", "key_set": True}
        raise AssertionError(path)
    monkeypatch.setattr(ai.canopy_web, "call", fake)
    assert ai.fetch_agent_vault("new") == "From-Record"
    assert ai.fetch_agent_vault("old") == "From-Status"
    assert "/api/agents/new/vault" not in calls


def test_expand_vars_like_op():
    assert ai.expand_vars("op://${AGENT_VAULT}/x op://$AGENT_VAULT/y op://$NOPE/z",
                          {"AGENT_VAULT": "V"}) == "op://V/x op://V/y op://$NOPE/z"


# ── the CLI ──────────────────────────────────────────────────────────────────

def test_cli_one_instance_and_print(tmp_path, tpl, monkeypatch):
    from orchestrator import agent_cli, workspace_cli
    monkeypatch.setattr(workspace_cli, "person_token", lambda: "pat")
    monkeypatch.setattr(ai, "fetch_instances", lambda p, token: [_inst("ace", vault="Ace-V")])
    op = FakeOp()
    monkeypatch.setattr(ai.subprocess, "run", op)
    out = tmp_path / "h" / ".env"
    r = CliRunner().invoke(agent_cli.agent, ["env", "--plugin", "ace", "--template", str(tpl),
                                             "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "instance: ace (workspace connect)" in r.output and "vault:    Ace-V" in r.output
    assert op.calls[0][1]["AGENT_VAULT"] == "Ace-V"
    r = CliRunner().invoke(agent_cli.agent, ["env", "--plugin", "ace", "--out", str(out), "--print"])
    assert r.exit_code == 0 and json.loads(r.output)["vault"] == "Ace-V"


# ── no convention left ───────────────────────────────────────────────────────

def test_no_agent_slug_vault_derivation_left_in_canopy():
    """The vault belongs to the instance (canopy-web `Agent.op_vault`); nothing derives
    `Agent-<Slug>` from a slug any more."""
    pat = re.compile(r"""f["']Agent-|["']Agent-["']\s*\+|Agent-\{""")
    hits = []
    for base in ("src", "packages", "plugins/canopy/hooks", "plugins/canopy/agent-core"):
        for p in (ROOT / base).rglob("*.py"):
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "vault-name derivation found:\n" + "\n".join(hits)
