"""sleep_cap_guard: no foreground shell wait may sleep past the 5-minute prompt-cache window.

Evidence (ACE runs 2026-10-15/16): phase agents waited on children with `sleep 300..595`
and `perl -e 'select(undef,undef,undef,590)'`, waking 7-9 times per wait into a cold
cache — each wake re-writing a 450-720k context.
"""
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks/sleep_cap_guard.py"
_SPEC = importlib.util.spec_from_file_location("sleep_cap_guard", _PATH)
g = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(g)

LONG = 600_000  # the Bash tool's max timeout — what a long waiter asks for


def _bash(cmd, timeout=LONG, **kw):
    return {"command": cmd, "timeout": timeout, **kw}


DENIED = [
    "sleep 300",
    "sleep 595 && gh pr view 12 --json state",
    "sleep 5m",
    "sleep 4m 40s",
    "sleep $((60*5))",
    "perl -e 'select(undef,undef,undef,590)'",
    "perl -e 'sleep 400'",
    "python3 -c 'import time; time.sleep(420)'",
    "for i in $(seq 1 115); do sleep 5; done",
    "for i in `seq 1 60`; do gh pr checks 3 && break; sleep 10; done",
    "for i in {1..30}; do sleep 20; done",
    "for ((i=0; i<40; i++)); do sleep 10; done",
    "for i in 1 2 3 4 5 6; do sleep 60; done",
    "until gh pr view 9 --json state | grep -q MERGED; do sleep 30; done",
    "while true; do curl -s x; sleep 15; done",
    "timeout 600 sleep 600",
    "timeout 590 bash -c 'until test -f done.flag; do sleep 5; done'",
    "bash -c 'sleep 400'",
    "cd /x && sleep 280 2>&1 | tail -1",
    "for i in $(seq 1 115)\ndo\n  sleep 5\ndone",
]


@pytest.mark.parametrize("cmd", DENIED)
def test_long_foreground_waits_are_denied(cmd):
    reason = g.decide("Bash", _bash(cmd), env={})
    assert reason and "prompt cache" in reason
    assert "run_in_background" in reason and "gh pr checks" in reason and "270" in reason


ALLOWED = [
    "sleep 30",
    "sleep 270",
    "sleep 120 && gh pr checks 4",
    "for i in $(seq 1 20); do sleep 10; done",          # 200 s
    "timeout 200 bash -c 'until test -f x; do sleep 5; done'",
    "npm test",                                          # long, but not a wait
    "uv run pytest -q",
    "timeout 590 npm test",
    "sleep 600 &",                                       # backgrounded inside the shell
    "echo 'sleep 900'",                                  # quoted text, not a command
    "grep -rn 'sleep 600' docs/",
    "sleep $DELAY",                                      # unknowable: let it through
    "gh pr checks 5 --watch",                            # not a sleep
    "cat > notes.md <<'EOF'\nnever run sleep 600 here\nsleep 900\nEOF\ngit add notes.md",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_short_waits_and_non_waits_are_allowed(cmd):
    assert g.decide("Bash", _bash(cmd), env={}) is None


def test_the_bash_timeout_bounds_the_wait():
    # An unbounded poll under the default 120 s timeout is killed long before 5 minutes.
    poll = "until gh pr view 9 --json state | grep -q MERGED; do sleep 30; done"
    assert g.decide("Bash", {"command": poll}, env={}) is None
    assert g.decide("Bash", _bash(poll, timeout=250_000), env={}) is None
    assert g.decide("Bash", _bash(poll, timeout=300_000), env={})
    # ...and so does BASH_DEFAULT_TIMEOUT_MS when the call names none
    assert g.decide("Bash", {"command": poll}, env={"BASH_DEFAULT_TIMEOUT_MS": "600000"})


def test_background_commands_are_allowed():
    assert g.decide("Bash", _bash("sleep 900", run_in_background=True), env={}) is None


def test_powershell_start_sleep():
    assert g.decide("PowerShell", _bash("Start-Sleep -Seconds 400"), env={})
    assert g.decide("PowerShell", _bash("Start-Sleep -Milliseconds 400000"), env={})
    assert g.decide("PowerShell", _bash("Start-Sleep 60"), env={}) is None


def test_other_tools_are_untouched():
    assert g.decide("Monitor", {"command": "sleep 900"}, env={}) is None
    assert g.decide("Read", {"file_path": "/x"}, env={}) is None


def test_the_cap_is_configurable_and_can_be_switched_off():
    assert g.decide("Bash", _bash("sleep 200"), env={"CANOPY_SLEEP_CAP_SECONDS": "120"})
    assert g.decide("Bash", _bash("sleep 900"), env={"CANOPY_SLEEP_CAP_SECONDS": "off"}) is None
    assert g.decide("Bash", _bash("sleep 900"), env={"CANOPY_SLEEP_CAP_SECONDS": "0"}) is None


def test_a_one_shot_turn_is_not_told_to_background():
    """one_shot_turn_guard denies background work there — don't send the agent into it."""
    reason = g.decide("Bash", _bash("sleep 500"), env={"CANOPY_REQUESTED_BY": ""})
    assert reason and "run_in_background" not in reason and "270" in reason


@pytest.mark.parametrize("cmd,seconds", [
    ("for i in $(seq 1 115); do sleep 5; done", 575),
    ("for i in $(seq 0 5 100); do sleep 1; done", 21),
    ("sleep 1m 30s; sleep 10", 100),
    ("timeout 60 sleep 600", 60),
    ("perl -e 'select(undef,undef,undef,0.5)'", 0.5),
])
def test_wait_seconds_counts_the_way_the_wait_runs(cmd, seconds):
    assert g.wait_seconds(cmd) == pytest.approx(seconds)


def test_garbage_fails_open(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert g.main() == 0
    assert g.decide("Bash", _bash("sleep 'unterminated"), env={}) is None


def test_the_hook_runs_as_a_script_under_system_python():
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "sleep 400", "timeout": LONG}})
    r = subprocess.run([sys.executable, str(_PATH)], input=payload, capture_output=True,
                       text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 2 and "prompt cache" in r.stderr
    ok = json.dumps({"tool_name": "Bash", "tool_input": {"command": "sleep 10"}})
    r = subprocess.run([sys.executable, str(_PATH)], input=ok, capture_output=True,
                       text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0


def test_the_hook_is_registered_for_the_shell_tools():
    hooks = json.loads((_PATH.parent / "hooks.json").read_text())["hooks"]
    [entry] = [e for e in hooks["PreToolUse"]
               if any("sleep_cap_guard.py" in h["command"] for h in e["hooks"])]
    assert set(entry["matcher"].split("|")) == g.SHELL_TOOLS
    assert all(re.fullmatch(r"[A-Za-z]+", t) for t in entry["matcher"].split("|"))
    [hook] = entry["hooks"]
    assert "python.sh" in hook["command"] and "--guard" not in hook["command"]
