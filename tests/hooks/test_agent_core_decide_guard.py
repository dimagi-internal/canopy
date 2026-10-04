"""The fleet's decide-don't-offer rail: a native prompt Stop hook rendered from labelled
examples (`src/orchestrator/decide_guard_prompt.py`).

The regex engine this replaced was whack-a-mole (2026-10-04: three misses in one ada
session). Its parametrized closings now live as labelled examples in
`plugins/canopy/agent-core/decide_guard_examples.jsonl`; what is tested here is the
plumbing that keeps the hook honest — the render is in sync, the prompt keeps the rules
the live probe showed it needs, and the retired engine is a harmless no-op.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import decide_guard_prompt as dg
from orchestrator.decide_guard_cli import decide_guard_group

REPO = Path(__file__).resolve().parents[2]
STUB = REPO / "plugins" / "canopy" / "agent-core" / "decide_guard.py"


def _our_stop_hooks(hooks: dict) -> list[dict]:
    return [h for e in hooks["hooks"].get("Stop", []) for h in e["hooks"]
            if h.get("statusMessage") == dg.STATUS_MESSAGE]


def test_hooks_json_is_in_sync_with_the_examples():
    """hooks.json is GENERATED. Edit the examples file, then `canopy decide-guard render`."""
    assert dg.in_sync(), "run `uv run canopy decide-guard render`"


def test_exactly_one_prompt_hook_on_stop_with_a_full_model_id():
    (hook,) = _our_stop_hooks(json.loads(dg.HOOKS_JSON_PATH.read_text()))
    assert hook["type"] == "prompt"
    # Probed 2026-10-04: an alias ("haiku") is rejected as unrecognized_model.
    assert hook["model"].startswith("claude-") and hook["model"].count("-") >= 2
    assert hook["timeout"] == dg.TIMEOUT_S


def test_the_stop_hook_active_rule_comes_first():
    """Without it the live probe blocked 10 times in a row: a block re-triggers Stop."""
    prompt = dg.render_prompt(dg.load_examples())
    first_rule = prompt.index("stop_hook_active")
    assert first_rule < prompt.index("DEV ACTIONS")
    assert first_rule < prompt.index("HANDBACK:")
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
    (row,) = [r for r in dg.load_examples() if fragment in r["closing"]]
    assert row["handback"] is True


@pytest.mark.parametrize("fragment", [
    "Want me to send it?", "publish this digest to the board", "Should I notify the team?",
])
def test_outbound_asks_are_labelled_fine(fragment):
    """Outbound always waits for a human — asking there is correct."""
    (row,) = [r for r in dg.load_examples() if fragment in r["closing"]]
    assert row["handback"] is False


def test_dollar_signs_in_examples_cannot_become_placeholders():
    out = dg.render_prompt([{"closing": "costs $ARGUMENTS $1", "handback": False, "why": "x"}])
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


def test_add_example_appends_and_rerenders(tmp_path):
    plugin = _fake_checkout(tmp_path)
    result = CliRunner().invoke(decide_guard_group, [
        "add-example", "--repo", str(tmp_path),
        "--closing", "The retry wrapper would be easy to add later.",
        "--handback", "true", "--why", "Undone dev work parked as 'later'.",
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


def test_check_fails_when_stale(tmp_path):
    plugin = _fake_checkout(tmp_path)
    with open(plugin / "agent-core" / dg.EXAMPLES_PATH.name, "a") as fh:
        fh.write(json.dumps({"closing": "new", "handback": False, "why": "y"}) + "\n")
    result = CliRunner().invoke(decide_guard_group, ["check", "--repo", str(tmp_path)])
    assert result.exit_code != 0 and "stale" in result.output


def test_the_retired_engine_is_a_silent_no_op():
    """Agent repos whose loader still execs this file must stay harmless."""
    p = subprocess.run([sys.executable, str(STUB)], input=json.dumps({
        "transcript_path": "/nonexistent", "session_id": "s1"}),
        capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout == ""
