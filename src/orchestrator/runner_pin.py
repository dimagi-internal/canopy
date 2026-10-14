"""Which runner a leader's fan-out lands on — `--runner caller|none|<name or id>`.

A huddle's rounds and a thread's messages are sent by a LEADER (a moderator) from one of its
own turns. Left to default routing, each member's turn follows that member's own runner order,
so one huddle can scatter across a cloud box and whichever laptops are online (2026-10-14: ada
led from cloud-ec2-2 and ace's three rounds landed on Jonathan's laptop, because ace follows
its workspace's default order and the laptop is first in it). The fix is not a routing rule
per agent: the leader says where its fan-out runs, and the default is where the leader is.

    caller  the runner the calling turn is on (the default). Found from the turn's own record:
            `provenance.resolve_parent()` gives this session's turn id on every runner kind
            (cloud: `$CANOPY_TURN_ID`; laptop: the caller hook's by-task record), and
            canopy-web says which runner claimed it. A session that is NOT a runner turn —
            a person at their own laptop — resolves to that machine's runner
            (`~/.canopy/runner.json`), so "run a huddle on my current runner" needs no flag.
    none    default routing: every member follows its own runner order. Explicit opt-in.
    <ref>   one named box (exact name or uuid, `agent_dispatch.resolve_runner`) — e.g. to
            debug a huddle on your laptop while the leader runs in the cloud.

Every resolved pin is vetted against every agent it will carry (`check_runner_pin`) BEFORE
anything is sent: a box that cannot drive one of them is a refusal, not a turn that sits
queued or lands on a box without the agent's repo.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from orchestrator import canopy_web
from orchestrator.agent_client import CanopyError

CALLER = "caller"
NONE = "none"
TURN_PATH = "/api/harness/turns/{id}"
RUNNER_CONFIG_ENV = "CANOPY_RUNNER_CONFIG"
DEFAULT_RUNNER_CONFIG = Path.home() / ".canopy" / "runner.json"


class PinError(ValueError):
    """The pin cannot be resolved or would not work; the message says what to pass instead."""


def _runners() -> list[dict]:
    from orchestrator.agent_dispatch import RUNNERS_PATH
    try:
        rows = canopy_web.call("GET", RUNNERS_PATH) or []
    except (CanopyError, RuntimeError) as e:
        raise PinError(f"could not list runners to resolve the pin: {e}")
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    return rows


def _machine_runner_id() -> str:
    path = Path(os.environ.get(RUNNER_CONFIG_ENV) or DEFAULT_RUNNER_CONFIG)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str((doc or {}).get("runner_id") or "").strip() if isinstance(doc, dict) else ""


def caller_runner_ref() -> tuple[str, str]:
    """(runner name-or-id, how it was found) for the runner this process runs under."""
    from orchestrator.provenance import resolve_parent
    turn_id = resolve_parent().get("turn_id") or ""
    if turn_id:
        try:
            turn = canopy_web.call("GET", TURN_PATH.format(id=turn_id)) or {}
        except (CanopyError, RuntimeError) as e:
            # Never fall back to the machine here: this IS a runner turn, and guessing
            # another box is exactly the scatter the pin exists to stop.
            raise PinError(f"cannot read this session's turn {turn_id} to find its runner: {e}. "
                           "Pass --runner <name> (or --runner none) explicitly.")
        name = str(turn.get("claimed_by_name") or "").strip()
        if not name:
            raise PinError(f"this session's turn {turn_id} names no runner that claimed it. "
                           "Pass --runner <name> (or --runner none) explicitly.")
        return name, f"the runner of this session's turn {turn_id}"
    rid = _machine_runner_id()
    if rid:
        return rid, "this machine's runner (~/.canopy/runner.json)"
    raise PinError("--runner caller: this session is not a runner turn and this machine has no "
                   "runner (~/.canopy/runner.json). Pass --runner <name> to pin one box, or "
                   "--runner none for default routing.")


def resolve_pin(ref: str, agents: list[str]) -> dict:
    """The pin for a fan-out to `agents`: {"id", "name", "basis", "warnings"}.

    `id` None = default routing (`--runner none`). Raises PinError when the ref cannot be
    resolved, or the box can never take (or is not taking) one of the agents' turns."""
    from orchestrator.agent_dispatch import DispatchError, check_runner_pin, resolve_runner
    ref = (ref or CALLER).strip()
    if ref.lower() == NONE:
        return {"id": None, "name": "", "basis": "none — every member follows its own runner order",
                "warnings": []}
    if ref.lower() == CALLER:
        needle, basis = caller_runner_ref()
    else:
        needle, basis = ref, "named with --runner"
    try:
        runner = resolve_runner(_runners(), needle)
    except DispatchError as e:
        raise PinError(str(e))
    refusals: list[str] = []
    warnings: list[str] = []
    for slug in agents:
        problems, not_ready, warns = check_runner_pin(runner, slug)
        refusals += problems + not_ready
        warnings += [w for w in warns if w not in warnings]
    if refusals:
        raise PinError("refusing to pin to " + str(runner.get("name") or runner.get("id")) + ": "
                       + "; ".join(dict.fromkeys(refusals))
                       + ". Pass --runner <another box> or --runner none.")
    return {"id": str(runner.get("id")), "name": str(runner.get("name") or ""), "basis": basis,
            "warnings": warnings}


def flag(pin: dict | None) -> str:
    """The `--runner` value that reproduces this pin in a follow-on command."""
    if not pin or not pin.get("id"):
        return NONE
    return str(pin.get("name") or pin["id"])
