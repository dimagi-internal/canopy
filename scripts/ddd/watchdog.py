"""Heartbeat + timeout for every long DDD sub-step — a clear ``timed_out``, never a hang.

Why this module exists
----------------------
On the first live v1 run one fixer batch took **4 h 04 min** of wall clock for
~20 minutes of work: it stalled on a shared test database another session held,
and nothing in the loop noticed — there was no timeout on a fixer, a render, or
a seed (``loop-metrics.md`` B3). A loop that runs unattended needs every step
it waits on to end in one of four named states: ``ok``, ``failed``,
``timed_out``, or still ``running`` with a recent heartbeat.

Two shapes of step
------------------
* **Shell sub-steps** (render wrapper, seed, CI wait): wrap them —

      python -m scripts.ddd.watchdog run <run_id> --step render -- <cmd …>

  The child runs in its own process group; past its budget (or with a
  ``--heartbeat`` file gone stale) the whole group is killed and the step is
  recorded ``timed_out``. Exit code: the child's, or 124 on a timeout.

* **Agent sub-steps** (fixer subagents): the orchestrator records a start, the
  fixer touches its heartbeat file after every meaningful step, and the
  orchestrator polls ``check`` while it waits —

      python -m scripts.ddd.watchdog start <run_id> fixer:B3     # prints the heartbeat path
      python -m scripts.ddd.watchdog check <run_id> fixer:B3     # exit 3 = timed_out
      python -m scripts.ddd.watchdog finish <run_id> fixer:B3 --status ok

  ``check`` answers ``timed_out`` when the total budget is spent OR no heartbeat
  arrived for ``heartbeat_minutes``. On ``timed_out`` stop the agent, record it,
  and carry its findings into the next batch — do not wait on it.

Budgets come from ``.canopy/ddd/config.yaml`` ``timeouts:`` (see
:mod:`scripts.ddd.loop_config`): ``default_minutes`` (45), ``heartbeat_minutes``
(15), and ``<step>_minutes`` per step (``fixer_minutes``, ``render_minutes`` …;
``fixer:B3`` reads ``fixer_minutes``). State lives on ``RunState.steps``.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TIMED_OUT_EXIT = 124
STATUSES = ("running", "ok", "failed", "timed_out")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _parse_iso(raw: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None


def heartbeat_path(run_dir: str | Path, step: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in step)
    return Path(run_dir) / "heartbeats" / safe


# ---------------------------------------------------------------------------
# Pure decision
# ---------------------------------------------------------------------------


def evaluate(entry: dict, *, now: float, heartbeat_mtime: float | None = None) -> dict:
    """``running`` or ``timed_out`` (with the reason) for a recorded running step.

    A finished step (``ok``/``failed``/``timed_out``) is returned as recorded.
    """
    status = entry.get("status")
    if status != "running":
        return {"status": status or "unknown", "reason": entry.get("reason") or ""}
    started = _parse_iso(entry.get("started_at")) or now
    beats = [b for b in (_parse_iso(entry.get("last_beat")), heartbeat_mtime, started) if b]
    last = max(beats)
    total = float(entry.get("timeout_minutes") or 45.0) * 60
    idle = float(entry.get("heartbeat_minutes") or 15.0) * 60
    elapsed, quiet = now - started, now - last
    if elapsed > total:
        return {
            "status": "timed_out",
            "reason": f"ran {elapsed / 60:.0f} min, budget {total / 60:.0f} min",
            "elapsed_minutes": round(elapsed / 60, 1),
        }
    if quiet > idle:
        return {
            "status": "timed_out",
            "reason": f"no heartbeat for {quiet / 60:.0f} min (limit {idle / 60:.0f} min)",
            "elapsed_minutes": round(elapsed / 60, 1),
        }
    return {
        "status": "running",
        "reason": f"{elapsed / 60:.0f}/{total / 60:.0f} min, last heartbeat {quiet / 60:.0f} min ago",
        "elapsed_minutes": round(elapsed / 60, 1),
    }


# ---------------------------------------------------------------------------
# RunState bookkeeping
# ---------------------------------------------------------------------------


def start(state: Any, step: str, *, timeout_minutes: float, heartbeat_minutes: float, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    entry = {
        "status": "running",
        "started_at": _iso(now),
        "last_beat": _iso(now),
        "timeout_minutes": float(timeout_minutes),
        "heartbeat_minutes": float(heartbeat_minutes),
        "finished_at": None,
        "reason": "",
    }
    state.steps = {**(getattr(state, "steps", None) or {}), step: entry}
    return entry


def finish(state: Any, step: str, status: str, *, reason: str = "", now: float | None = None) -> dict:
    if status not in STATUSES or status == "running":
        raise ValueError(f"finish status must be ok | failed | timed_out, got {status!r}")
    now = time.time() if now is None else now
    entry = dict((getattr(state, "steps", None) or {}).get(step) or {})
    entry.update({"status": status, "finished_at": _iso(now), "reason": reason})
    state.steps = {**(getattr(state, "steps", None) or {}), step: entry}
    return entry


def check(state: Any, step: str, *, run_dir: str | Path | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    entry = (getattr(state, "steps", None) or {}).get(step)
    if not entry:
        return {"status": "unknown", "reason": f"no step {step!r} recorded"}
    mtime = None
    if run_dir is not None:
        hb = heartbeat_path(run_dir, step)
        if hb.exists():
            mtime = hb.stat().st_mtime
    return evaluate(entry, now=now, heartbeat_mtime=mtime)


# ---------------------------------------------------------------------------
# Wrapped shell step
# ---------------------------------------------------------------------------


def run_command(
    cmd: list[str],
    *,
    timeout_s: float,
    heartbeat_file: str | Path | None = None,
    heartbeat_s: float | None = None,
    poll_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    popen: Callable[..., Any] = subprocess.Popen,
) -> dict:
    """Run ``cmd``; kill its process group past ``timeout_s`` or a stale heartbeat."""
    start_mono = clock()
    start_wall = time.time()
    proc = popen(cmd, start_new_session=True)
    while True:
        code = proc.poll()
        if code is not None:
            return {
                "status": "ok" if code == 0 else "failed",
                "exit_code": code,
                "elapsed_s": round(clock() - start_mono, 1),
                "reason": "" if code == 0 else f"exited {code}",
            }
        elapsed = clock() - start_mono
        reason = None
        if elapsed > timeout_s:
            reason = f"ran {elapsed:.0f}s, budget {timeout_s:.0f}s"
        elif heartbeat_file and heartbeat_s:
            hb = Path(heartbeat_file)
            last = max(start_wall, hb.stat().st_mtime if hb.exists() else start_wall)
            if time.time() - last > heartbeat_s:
                reason = f"no heartbeat on {hb} for {heartbeat_s:.0f}s"
        if reason:
            _kill_group(proc)
            return {
                "status": "timed_out",
                "exit_code": TIMED_OUT_EXIT,
                "elapsed_s": round(elapsed, 1),
                "reason": reason,
            }
        time.sleep(poll_s)


def _kill_group(proc: Any) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd import loop_config
    from scripts.ddd.runstate import _resolve_ddd_dir, _run_dir_for, load, save

    argv = list(sys.argv[1:] if argv is None else argv)
    child: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, child = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.watchdog")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a shell step under a budget: run <run_id> --step S -- cmd …")
    r.add_argument("run_id")
    r.add_argument("--step", required=True)
    r.add_argument("--timeout-min", type=float, default=None)
    r.add_argument("--heartbeat", default=None, help="file whose mtime is the heartbeat")
    for name in ("start", "beat", "check"):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        p.add_argument("step")
        if name == "start":
            p.add_argument("--timeout-min", type=float, default=None)
    f = sub.add_parser("finish")
    f.add_argument("run_id")
    f.add_argument("step")
    f.add_argument("--status", required=True, choices=["ok", "failed", "timed_out"])
    f.add_argument("--reason", default="")
    args = ap.parse_args(argv)

    ddd_dir = _resolve_ddd_dir()
    cfg = loop_config.load(ddd_dir).timeouts
    run_dir = _run_dir_for(ddd_dir, args.run_id)
    state = load(args.run_id, ddd_dir=ddd_dir)
    step = args.step
    budget = getattr(args, "timeout_min", None) or cfg.for_step(step)

    if args.cmd == "run":
        if not child:
            print("watchdog run: give the command after `--`", file=sys.stderr)
            return 2
        start(state, step, timeout_minutes=budget, heartbeat_minutes=cfg.heartbeat_minutes)
        save(state, ddd_dir=ddd_dir)
        out = run_command(
            child,
            timeout_s=budget * 60,
            heartbeat_file=args.heartbeat,
            heartbeat_s=cfg.heartbeat_minutes * 60 if args.heartbeat else None,
        )
        state = load(args.run_id, ddd_dir=ddd_dir)
        finish(state, step, out["status"], reason=out["reason"])
        save(state, ddd_dir=ddd_dir)
        print(json.dumps({"step": step, **out}), file=sys.stderr)
        return int(out["exit_code"]) if out["status"] != "ok" else 0
    if args.cmd == "start":
        start(state, step, timeout_minutes=budget, heartbeat_minutes=cfg.heartbeat_minutes)
        hb = heartbeat_path(run_dir, step)
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.touch()
        save(state, ddd_dir=ddd_dir)
        print(json.dumps({"step": step, "heartbeat": str(hb), "timeout_minutes": budget,
                          "heartbeat_minutes": cfg.heartbeat_minutes}))
        return 0
    if args.cmd == "beat":
        hb = heartbeat_path(run_dir, step)
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.touch()
        return 0
    if args.cmd == "check":
        out = check(state, step, run_dir=run_dir)
        if out["status"] == "timed_out":
            finish(state, step, "timed_out", reason=out["reason"])
            save(state, ddd_dir=ddd_dir)
        print(json.dumps({"step": step, **out}))
        return 3 if out["status"] == "timed_out" else 0
    finish(state, step, args.status, reason=args.reason)
    save(state, ddd_dir=ddd_dir)
    print(json.dumps({"step": step, "status": args.status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
