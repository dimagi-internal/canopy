"""Pin one canopy version for a whole DDD run (M7 / M18).

Why this module exists
----------------------
The first live v1 run had THREE canopy versions in play at once: the plugin
auto-updated 0.2.528 -> 0.2.530 mid-run, the orchestrator kept the 0.2.528
runtime by typing its path by hand, and judge subagents loaded skill text from
0.2.524 because the session's Skill registry is fixed at session start. Nothing
errored; the instructions and the code simply stopped describing one contract.

A run now records, at start, the canopy version and the runtime root it began
with (``RunState.plugin_version`` / ``runtime_root``) and resolves the runtime
by that pinned path for the rest of the run. Any disagreement — the runtime a
command is actually running from, or the skill text a subagent loaded — is a
loud WARNING, written to ``RunState.version_warnings`` so the digest carries it.

    python -m scripts.ddd.pin ensure <run_id>                 # pin if unpinned (idempotent)
    python -m scripts.ddd.pin root <run_id>                   # the pinned runtime root
    python -m scripts.ddd.pin check <run_id> [--skill-dir D …]  # exit 1 on skew

Use the root for every ``scripts.ddd`` call of the run::

    DDD_REPO="$(cd "$DDD_REPO" && uv run python -m scripts.ddd.pin root <run_id>)"
    export CANOPY_RUNTIME_ROOT="$DDD_REPO"   # canopy-runtime.sh honours it first

A pinned root that no longer exists (the cache was pruned) is a warning and a
fallback to the current runtime — never a crash.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.ddd.version_skew import runtime_version, version_from_skill_dir


def current_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _valid_root(root: str | Path | None) -> bool:
    return bool(root) and (Path(str(root)) / "scripts" / "ddd").is_dir()


def warn(state: Any, message: str) -> None:
    existing = list(getattr(state, "version_warnings", None) or [])
    if message not in existing:
        state.version_warnings = existing + [message]
    print(f"WARNING: {message}", file=sys.stderr)


def ensure(state: Any, *, root: str | Path | None = None) -> dict:
    """Pin the run to ``root`` (default: this runtime) unless it is already pinned."""
    if getattr(state, "plugin_version", None) and getattr(state, "runtime_root", None):
        return {"plugin_version": state.plugin_version, "runtime_root": state.runtime_root, "fresh": False}
    root_path = Path(str(root)).resolve() if root else current_root()
    state.plugin_version = runtime_version(root_path)
    state.runtime_root = str(root_path)
    return {"plugin_version": state.plugin_version, "runtime_root": state.runtime_root, "fresh": True}


def resolve_root(state: Any) -> dict:
    """The runtime root to use for this run, with a warning if the pin is unusable."""
    pinned = getattr(state, "runtime_root", None)
    if pinned and _valid_root(pinned):
        return {"runtime_root": pinned, "pinned": True, "warning": None}
    cur = current_root()
    if not pinned:
        return {"runtime_root": str(cur), "pinned": False, "warning": None}
    return {
        "runtime_root": str(cur),
        "pinned": False,
        "warning": (
            f"pinned canopy runtime {pinned} (v{getattr(state, 'plugin_version', '?')}) no longer "
            f"exists; falling back to {cur} (v{runtime_version(cur)}) for the rest of the run"
        ),
    }


def skew(state: Any, *, runtime_root: str | Path | None = None, skill_dirs: list[str] | None = None) -> list[str]:
    """Every disagreement with the pinned version, as warning strings."""
    pinned = getattr(state, "plugin_version", None)
    if not pinned:
        return []
    out: list[str] = []
    running = runtime_version(runtime_root) if runtime_root else runtime_version(current_root())
    if running and running != pinned:
        out.append(
            f"version skew: run {getattr(state, 'run_id', '?')} is pinned to canopy {pinned} but "
            f"this command ran from runtime {running} — resolve the runtime with "
            "`scripts.ddd.pin root` (export CANOPY_RUNTIME_ROOT) for every call of the run"
        )
    for d in skill_dirs or []:
        v = version_from_skill_dir(d)
        if v and v != pinned:
            out.append(
                f"version skew: skill text loaded from canopy {v} ({d}) but run "
                f"{getattr(state, 'run_id', '?')} is pinned to {pinned} — have the subagent Read "
                f"<pinned runtime>/../skills/<name>/SKILL.md instead of loading it via the Skill tool"
            )
    return out


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd.runstate import load, save

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.pin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ensure", "root", "check"):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        if name == "check":
            p.add_argument("--skill-dir", action="append", default=[])
    args = ap.parse_args(argv)
    try:
        state = load(args.run_id)
    except (OSError, ValueError) as exc:
        print(f"pin {args.cmd}: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "ensure":
        out = ensure(state)
        save(state)
        print(json.dumps(out))
        return 0
    if args.cmd == "root":
        if not getattr(state, "runtime_root", None):
            ensure(state)
            save(state)
        out = resolve_root(state)
        if out["warning"]:
            warn(state, out["warning"])
            save(state)
        print(out["runtime_root"])
        return 0
    problems = skew(state, skill_dirs=args.skill_dir)
    for msg in problems:
        warn(state, msg)
    if problems:
        save(state)
        return 1
    print(f"version ok: run pinned to canopy {state.plugin_version or '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
