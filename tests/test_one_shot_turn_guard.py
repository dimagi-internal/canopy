"""one_shot_turn_guard: a one-shot runner turn may leave nothing running in the background.

Evidence (2026-10-04, cloud-ec2-1, ACP): Ada's fleet-sync turn ended with background Bash
jobs outstanding and the sync stalled; ACE's fix turn backgrounded its test suite, ended,
and the fix was never committed. Nothing can resume a cloud-runner turn once it ends.
"""
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks/one_shot_turn_guard.py"
_SPEC = importlib.util.spec_from_file_location("one_shot_turn_guard", _PATH)
g = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(g)

_RUNNER_ENV_KEYS = ("CANOPY_ONE_SHOT_TURN", "CANOPY_REQUESTED_BY")


@pytest.fixture()
def interactive(monkeypatch):
    for k in _RUNNER_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture()
def runner_turn(monkeypatch, interactive):
    # What cloud_runner._github_turn_env puts in EVERY cloud turn's env — often blank.
    monkeypatch.setenv("CANOPY_REQUESTED_BY", "")


def _run(monkeypatch, capsys, tool, tool_input):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": tool, "tool_input": tool_input, "cwd": "/opt/agents/ada"})))
    code = g.main()
    return code, capsys.readouterr().err


BACKGROUND_SHAPES = [
    ("Bash", {"command": "uv run pytest -q", "run_in_background": True}),
    ("Agent", {"description": "collect", "prompt": "x", "run_in_background": True}),
    ("Task", {"description": "collect", "prompt": "x", "run_in_background": True}),
    ("Agent", {"description": "collect", "prompt": "x", "subagent_type": "fork"}),
    ("Agent", {"description": "collect", "prompt": "x", "isolation": "remote"}),
    ("Monitor", {"command": "until gh pr view 1; do sleep 2; done"}),
    ("ScheduleWakeup", {"delay": 300}),
    ("CronCreate", {"cron": "*/5 * * * *", "prompt": "check"}),
]


@pytest.mark.parametrize("tool,tool_input", BACKGROUND_SHAPES)
def test_background_work_is_denied_in_a_runner_turn(monkeypatch, capsys, runner_turn,
                                                    tool, tool_input):
    code, err = _run(monkeypatch, capsys, tool, tool_input)
    assert code == 2
    assert "FOREGROUND" in err and "under 10 minutes" in err
    assert "never end the turn with work outstanding" in err


@pytest.mark.parametrize("tool,tool_input", BACKGROUND_SHAPES)
def test_an_interactive_session_is_untouched(monkeypatch, capsys, interactive, tool, tool_input):
    assert _run(monkeypatch, capsys, tool, tool_input) == (0, "")


@pytest.mark.parametrize("tool,tool_input", [
    ("Bash", {"command": "uv run pytest -q", "timeout": 600000}),
    ("Bash", {"command": "ls", "run_in_background": False}),
    ("Agent", {"description": "review", "prompt": "x", "subagent_type": "general-purpose"}),
    ("Read", {"file_path": "/opt/agents/ada/CLAUDE.md"}),
])
def test_foreground_work_is_allowed_in_a_runner_turn(monkeypatch, capsys, runner_turn,
                                                     tool, tool_input):
    assert _run(monkeypatch, capsys, tool, tool_input) == (0, "")


def test_the_explicit_flag_wins_both_ways(monkeypatch, interactive):
    monkeypatch.setenv("CANOPY_ONE_SHOT_TURN", "1")
    assert g.one_shot_turn()
    monkeypatch.setenv("CANOPY_REQUESTED_BY", "a@x.org")
    monkeypatch.setenv("CANOPY_ONE_SHOT_TURN", "0")
    assert not g.one_shot_turn()


def test_the_laptop_runner_markers_alone_do_not_trigger_it():
    """A laptop turn runs in a persistent emdash session that a notification CAN wake;
    it carries the agent slug or a caller pointer, never CANOPY_REQUESTED_BY."""
    assert not g.one_shot_turn({"CANOPY_AGENT": "ada", "CANOPY_CALLER": "/tmp/c.json"})


def test_the_hook_runs_as_a_script_under_system_python(tmp_path):
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "sleep 900", "run_in_background": True}})
    env = {"PATH": "/usr/bin:/bin", "CANOPY_REQUESTED_BY": ""}
    r = subprocess.run([sys.executable, str(_PATH)], input=payload, capture_output=True,
                       text=True, env=env)
    assert r.returncode == 2 and "one-shot runner turn" in r.stderr
    r = subprocess.run([sys.executable, str(_PATH)], input=payload, capture_output=True,
                       text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0


def test_the_hook_is_registered_for_every_tool_it_judges():
    hooks = json.loads((_PATH.parent / "hooks.json").read_text())["hooks"]
    [entry] = [e for e in hooks["PreToolUse"]
               if any("one_shot_turn_guard.py" in h["command"] for h in e["hooks"])]
    matched = set(entry["matcher"].split("|"))
    judged = {t for t, _ in BACKGROUND_SHAPES}
    assert judged <= matched
    assert all(re.fullmatch(r"[A-Za-z]+", t) for t in matched)
