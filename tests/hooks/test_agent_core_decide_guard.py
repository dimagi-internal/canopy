"""The fleet's decide-don't-offer rail: a native prompt Stop hook rendered from labelled
examples (`src/orchestrator/decide_guard_prompt.py`).

The regex engine this replaced was whack-a-mole (2026-10-04: three misses in one ada
session). Its parametrized closings now live as labelled examples in
`plugins/canopy/agent-core/decide_guard_examples.jsonl`; what is tested here is the
plumbing that keeps the hook honest — the render agrees with the scope switch, the prompt
keeps the rules the live probe showed it needs, the stamp REPLACES the regex loader rather
than adding a second judge, only a person (never an agent repo) can opt in, and the retired regex engine is a harmless no-op.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import decide_guard_prompt as dg
from orchestrator.decide_guard_cli import decide_guard_group

REPO = Path(__file__).resolve().parents[2]


def _our_stop_hooks(hooks: dict) -> list[dict]:
    return [h for e in hooks["hooks"].get("Stop", []) for h in e["hooks"]
            if h.get("statusMessage") == dg.STATUS_MESSAGE]


def test_hooks_json_is_in_sync_with_the_examples():
    """hooks.json is GENERATED. Edit the examples file, then `canopy decide-guard render`."""
    assert dg.in_sync(), "run `uv run canopy decide-guard render`"


def test_plugin_hooks_json_follows_the_scope_switch():
    """Plugin-wide wiring fires in EVERY canopy session; it ships only when PLUGIN_WIDE."""
    ours = _our_stop_hooks(json.loads(dg.HOOKS_JSON_PATH.read_text()))
    assert len(ours) == (1 if dg.PLUGIN_WIDE else 0)


def test_the_hook_entry_uses_a_full_model_id():
    (hook,) = dg.hook_entry(dg.render_prompt(dg.load_examples()))["hooks"]
    assert hook["type"] == "prompt"
    # Probed 2026-10-04: an alias ("haiku") is rejected as unrecognized_model.
    assert hook["model"].startswith("claude-") and hook["model"].count("-") >= 2
    assert hook["timeout"] == dg.TIMEOUT_S


def test_the_stop_hook_active_rule_comes_first():
    """Without it the live probe blocked 10 times in a row: a block re-triggers Stop."""
    prompt = dg.render_prompt(dg.load_examples())
    first_rule = prompt.index("stop_hook_active")
    assert first_rule < prompt.index("Block only if")
    assert first_rule < prompt.index("BLOCK:")
    assert prompt.count("$ARGUMENTS") == 1 and prompt.rstrip().endswith("$ARGUMENTS")


def test_examples_are_well_formed_and_cover_both_labels():
    rows = dg.load_examples()
    assert len({r["closing"] for r in rows}) == len(rows), "duplicate closing"
    assert sum(r["handback"] for r in rows) >= 15
    assert sum(not r["handback"] for r in rows) >= 15


@pytest.mark.parametrize("fragment", [
    "It's a small canopy PR if you want it.",
    "unless you'd rather keep the engine call",
    "would be a small follow-up.",
])
def test_the_2026_10_04_misses_are_labelled_handbacks(fragment):
    rows = [r for r in dg.load_examples() if fragment in r["closing"]]
    assert rows and all(r["handback"] is True for r in rows)


@pytest.mark.parametrize("fragment", [
    "Want me to send it?", "publish this digest to the board", "Should I notify the team?",
])
def test_outbound_asks_are_labelled_fine(fragment):
    """Outbound always waits for a human — asking there is correct."""
    rows = [r for r in dg.load_examples() if fragment in r["closing"]]
    assert rows and all(r["handback"] is False for r in rows)


def test_dollar_signs_in_examples_cannot_become_placeholders():
    out = dg.render_prompt([{"closing": "costs $ARGUMENTS $1", "handback": False, "why": "x",
                             "in_prompt": True}])
    assert len(re.findall(r"(?<!\\)\$ARGUMENTS", out)) == 1 and "\\$1" in out


def test_render_replaces_its_own_entry_and_keeps_other_hooks(tmp_path):
    other = {"hooks": [{"type": "command", "command": "echo hi"}]}
    hooks = {"hooks": {"Stop": [other], "PreToolUse": []}}
    once = dg.rendered_hooks(hooks, dg.load_examples())
    twice = dg.rendered_hooks(once, dg.load_examples())
    assert once == twice
    assert twice["hooks"]["Stop"][0] == other and len(twice["hooks"]["Stop"]) == 2


def _fake_checkout(tmp_path) -> Path:
    plugin = tmp_path / "plugins" / "canopy"
    (plugin / "agent-core").mkdir(parents=True)
    (plugin / "hooks").mkdir()
    (plugin / "agent-core" / dg.EXAMPLES_PATH.name).write_text(dg.EXAMPLES_PATH.read_text())
    (plugin / "hooks" / "hooks.json").write_text(dg.HOOKS_JSON_PATH.read_text())
    return plugin


def test_add_example_appends_and_rerenders(tmp_path, monkeypatch):
    monkeypatch.setattr(dg, "PLUGIN_WIDE", True)  # exercise the plugin-wide render
    plugin = _fake_checkout(tmp_path)
    result = CliRunner().invoke(decide_guard_group, [
        "add-example", "--repo", str(tmp_path),
        "--closing", "The retry wrapper would be easy to add later.",
        "--handback", "true", "--why", "Undone dev work parked as 'later'.", "--in-prompt",
    ])
    assert result.exit_code == 0, result.output
    examples = plugin / "agent-core" / dg.EXAMPLES_PATH.name
    hooks = plugin / "hooks" / "hooks.json"
    assert dg.load_examples(examples)[-1]["handback"] is True
    assert dg.in_sync(hooks, examples)
    assert "retry wrapper would be easy" in hooks.read_text()

    dup = CliRunner().invoke(decide_guard_group, [
        "add-example", "--repo", str(tmp_path),
        "--closing", "The retry wrapper would be easy to add later.",
        "--handback", "false", "--why", "x",
    ])
    assert dup.exit_code != 0 and "already exists" in dup.output


def test_check_fails_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(dg, "PLUGIN_WIDE", True)
    plugin = _fake_checkout(tmp_path)
    with open(plugin / "agent-core" / dg.EXAMPLES_PATH.name, "a") as fh:
        fh.write(json.dumps({"closing": "new", "handback": False, "why": "y",
                             "in_prompt": True}) + "\n")
    result = CliRunner().invoke(decide_guard_group, ["check", "--repo", str(tmp_path)])
    assert result.exit_code != 0 and "stale" in result.output


_AGENT_SETTINGS = {
    "env": {"CANOPY_AGENT": "eva"},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/gating_guard.py\""}]}],
        "Stop": [{"hooks": [
            {"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/decide_guard.py\""},
            {"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/gdoc_gate.py\" check"},
        ]}],
    },
}


def _agent_repo(tmp_path) -> Path:
    repo = tmp_path / "eva"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(json.dumps(_AGENT_SETTINGS, indent=2) + "\n")
    return repo


def test_stamp_replaces_the_loader_and_keeps_every_other_hook(tmp_path):
    """Agent-only scope: the prompt hook takes the loader's place — never both, or the
    session is judged twice — and the sibling Stop rail (gdoc_gate) survives."""
    settings = _agent_repo(tmp_path) / ".claude" / "settings.json"
    assert dg.stamp_settings(settings) is True
    data = json.loads(settings.read_text())
    stop_cmds = [h.get("command", "") for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert not any("decide_guard.py" in c for c in stop_cmds)
    assert any("gdoc_gate.py" in c for c in stop_cmds)
    assert len(_our_stop_hooks(data)) == 1
    assert data["hooks"]["PreToolUse"] == _AGENT_SETTINGS["hooks"]["PreToolUse"]
    assert data["env"] == _AGENT_SETTINGS["env"]
    assert dg.stamp_settings(settings) is False and dg.settings_in_sync(settings)


def test_an_agent_repo_cannot_carry_it(tmp_path):
    """Failure investigation 1 / turn bb892462: an agent repo's settings run decide-guard for
    EVERY person who runs that agent, so on Sarvesh's runner (Claude Code 2.1.220) it 400'd on
    every Stop and wedged ACE. Stamping an agent repo is refused, and writes nothing."""
    repo = _agent_repo(tmp_path)
    before = (repo / ".claude" / "settings.json").read_text()
    result = CliRunner().invoke(decide_guard_group, ["stamp", "--agent-repo", str(repo)])
    assert result.exit_code != 0 and "per-person" in result.output and "--user" in result.output
    assert (repo / ".claude" / "settings.json").read_text() == before


def test_unstamp_retires_an_agent_copy_and_keeps_every_other_hook(tmp_path):
    repo = _agent_repo(tmp_path)
    settings = repo / ".claude" / "settings.json"
    dg.stamp_settings(settings)  # the 2026-10-08..13 state: prompt hook in the agent repo
    assert CliRunner().invoke(decide_guard_group,
                              ["unstamp", "--agent-repo", str(repo), "--check"]).exit_code != 0
    ok = CliRunner().invoke(decide_guard_group, ["unstamp", "--agent-repo", str(repo)])
    assert ok.exit_code == 0 and "removed" in ok.output
    data = json.loads(settings.read_text())
    assert not dg.carries_decide_guard(settings) and not _our_stop_hooks(data)
    stop_cmds = [h.get("command", "") for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert stop_cmds == ["python3 \"$CLAUDE_PROJECT_DIR/hooks/gdoc_gate.py\" check"]
    assert data["hooks"]["PreToolUse"] == _AGENT_SETTINGS["hooks"]["PreToolUse"]
    assert CliRunner().invoke(decide_guard_group,
                              ["unstamp", "--agent-repo", str(repo), "--check"]).exit_code == 0


def test_unstamp_drops_an_emptied_stop_list(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"CANOPY_AGENT": "ada"}}) + "\n")
    dg.stamp_settings(settings)
    assert dg.unstamp_settings(settings) is True
    assert json.loads(settings.read_text()) == {"env": {"CANOPY_AGENT": "ada"}}
    assert dg.unstamp_settings(settings) is False


def test_production_deploy_asks_are_fine():
    """Fleet authority: deploys of other systems are not pre-approved (Jonathan, 2026-10-04)."""
    (row,) = [r for r in dg.load_examples() if "production deploy" in r["closing"]]
    assert row["handback"] is False


def test_the_retired_regex_engine_is_a_silent_no_op():
    """The prompt hook is plugin-wide, so an agent loader not yet removed must not judge the
    same Stop twice: the engine it execs reads nothing, prints nothing, exits 0."""
    import subprocess
    import sys

    stub = REPO / "plugins" / "canopy" / "agent-core" / "decide_guard.py"
    p = subprocess.run([sys.executable, str(stub)], input=json.dumps({
        "transcript_path": "/nonexistent", "session_id": "s1"}),
        capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout == ""


def test_the_prompt_stays_short_and_the_rest_is_held_out():
    """Jonathan, 2026-10-04: the 12K-char first version was "way way too much text" for a
    prompt sent on every Stop. Only `in_prompt` examples are rendered; the budget is enforced."""
    rows = dg.load_examples()
    prompt = dg.render_prompt(rows)
    assert len(prompt) <= dg.PROMPT_BUDGET
    held = [r for r in rows if not r.get("in_prompt")]
    assert len(held) >= 40 and not any(json.dumps(r["closing"])[1:-1] in prompt for r in held
                                        if len(r["closing"]) > 40)
    with pytest.raises(ValueError, match="budget"):
        dg.render_prompt([{"closing": "x" * 400, "handback": True, "why": "w", "in_prompt": True}] * 10)


def test_the_plugin_carries_no_decide_guard():
    """Opt-in since 2026-10-08: the plugin must not impose one person's Stop rail on every
    canopy user. Agents stamp it into their own settings; a person stamps it with --user."""
    assert dg.PLUGIN_WIDE is False
    hooks = json.loads(dg.HOOKS_JSON_PATH.read_text(encoding="utf-8"))
    assert not any(dg._is_ours(e) for e in hooks.get("hooks", {}).get("Stop", []))


def _home(tmp_path, monkeypatch, version):
    home = tmp_path / "home"
    monkeypatch.setattr(dg, "user_settings_path", lambda: home / ".claude" / "settings.json")
    monkeypatch.setattr(dg, "claude_code_version", lambda: version)
    return home / ".claude" / "settings.json"


def test_stamp_user_writes_your_own_settings_creating_them(tmp_path, monkeypatch):
    settings = _home(tmp_path, monkeypatch, dg.MIN_CLAUDE_CODE)
    stale = CliRunner().invoke(decide_guard_group, ["stamp", "--user", "--check"])
    assert stale.exit_code != 0
    ok = CliRunner().invoke(decide_guard_group, ["stamp", "--user"])
    assert ok.exit_code == 0 and "stamped" in ok.output
    stop = json.loads(settings.read_text())["hooks"]["Stop"]
    assert any(dg._is_ours(e) for e in stop)
    assert CliRunner().invoke(decide_guard_group, ["stamp", "--user", "--check"]).exit_code == 0
    off = CliRunner().invoke(decide_guard_group, ["unstamp", "--user"])
    assert off.exit_code == 0 and not dg.carries_decide_guard(settings)


@pytest.mark.parametrize("version", [(2, 1, 220), None])
def test_an_old_or_unreadable_claude_code_gets_no_judge(tmp_path, monkeypatch, version):
    """Turn bb892462: Claude Code 2.1.220's prompt-hook evaluator 400s on every Stop. An
    opt-in on such a machine is refused and writes nothing, so no judge call is ever made."""
    settings = _home(tmp_path, monkeypatch, version)
    result = CliRunner().invoke(decide_guard_group, ["stamp", "--user"])
    assert result.exit_code != 0 and "bb892462" in result.output
    assert not settings.exists()


def test_a_person_who_has_not_opted_in_gets_no_judge(tmp_path, monkeypatch):
    """Default OFF: with no --user stamp, neither the plugin nor any canopy-written file
    carries decide-guard, so a non-opted person's Stop makes no judge call."""
    settings = _home(tmp_path, monkeypatch, dg.MIN_CLAUDE_CODE)
    hooks = json.loads(dg.HOOKS_JSON_PATH.read_text(encoding="utf-8"))
    assert not dg._carrying(hooks)
    assert not settings.exists()
    assert CliRunner().invoke(decide_guard_group, ["unstamp", "--user", "--check"]).exit_code == 0


def test_stamp_needs_a_target():
    result = CliRunner().invoke(decide_guard_group, ["stamp"])
    assert result.exit_code != 0 and "--user" in result.output
