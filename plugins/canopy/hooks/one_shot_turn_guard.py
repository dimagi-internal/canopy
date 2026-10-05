#!/usr/bin/env python3
"""Rail: in a ONE-SHOT runner turn, nothing may be left running in the background.

A cloud-runner turn is one prompt. When the model ends its turn, `claude -p` exits
(or, under the ACP executor, the prompt request resolves and the runner closes the
agent — canopy-web `runner/ec2/cloud_runner.py::run_acp`, `agent.close()` in its
`finally`). A background task's completion notification then has nobody to deliver
to: the session is gone, and nothing re-opens it. Evidence, 2026-10-04 on
cloud-ec2-1 (ACP): Ada's fleet-sync turn started collection as background Bash jobs,
said "I'll pick this back up as soon as each one reports back" and ended 3 minutes in
— the sync stalled; ACE's fix turn ran its full test suite with run_in_background,
said "I'll wait for the task notification", and the fix was never committed.

**Scoped to where that is true.** A LAPTOP runner turn is different: the runner
types the prompt into a persistent, interactive emdash session
(canopy_runner/execute.py), which stays open after the turn and IS woken by a
background task's notification. So the rail fires only when the session's
environment says it was spawned by a one-shot runner:

  CANOPY_ONE_SHOT_TURN=1|0   explicit — wins either way (a runner that knows)
  CANOPY_REQUESTED_BY        set (even blank) on EVERY cloud-runner turn by
                             `_github_turn_env`, and by nothing else — not the
                             laptop runner, not an interactive human session

Interactive human sessions and laptop emdash turns are untouched.

Denied in a one-shot turn:
  Bash  with run_in_background: true
  Agent / Task  with run_in_background: true, a `fork` subagent, or remote
        isolation (all of which run detached and report back by notification)
  Monitor, ScheduleWakeup, CronCreate — waits that resume a session later

Stdlib only (system python3, like every canopy hook).
"""
import json
import os
import sys

FIX = ("run it in the FOREGROUND — split long runs into chunks under 10 minutes (the Bash "
       "timeout cap is 600000 ms) and loop; never end the turn with work outstanding")

#: Tools whose whole job is to resume the session later. Never useful in a one-shot turn.
_WAIT_TOOLS = {"Monitor", "ScheduleWakeup", "CronCreate"}
_SUBAGENT_TOOLS = {"Agent", "Task"}


def one_shot_turn(env=None) -> bool:
    """True when this session is an unattended runner turn that ends for good when the
    model stops — see the module docstring for why the laptop runner is excluded."""
    env = os.environ if env is None else env
    flag = (env.get("CANOPY_ONE_SHOT_TURN") or "").strip().lower()
    if flag:
        return flag not in ("0", "false", "no", "off")
    return "CANOPY_REQUESTED_BY" in env


def _truthy(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))


def decide(tool: str, tool_input: dict):
    """None to allow, else the reason to refuse — for a session already known one-shot."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash" and _truthy(ti.get("run_in_background")):
        return "a background Bash command"
    if tool in _SUBAGENT_TOOLS:
        if _truthy(ti.get("run_in_background")):
            return f"a background {tool} (run_in_background)"
        if str(ti.get("subagent_type") or "").strip().lower() == "fork":
            return f"a forked {tool} (a fork always runs in the background)"
        if str(ti.get("isolation") or "").strip().lower() == "remote":
            return f"a remote {tool} (remote isolation always runs in the background)"
    if tool in _WAIT_TOOLS:
        return f"{tool} (it resumes the session later)"
    return None


def main() -> int:
    if not one_shot_turn():
        return 0                            # interactive / laptop session: not ours to judge
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    what = decide(data.get("tool_name", ""), data.get("tool_input") or {})
    if what:
        print(f"canopy: blocked {what}. This is a one-shot runner turn: when you end your "
              f"turn the session ends, and a background task's notification can never "
              f"resume it — the work would be silently dropped. Instead, {FIX}.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
