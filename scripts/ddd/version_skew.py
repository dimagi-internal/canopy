"""Warn when the skill TEXT a session loaded and the RUNTIME it calls disagree.

Claude Code fixes a session's Skill registry at session start, so after a
mid-session plugin update (the fleet auto-updater does this) a judge subagent
still loads ``canopy:ddd-arc-eval`` from the OLD cache while every
``python -m scripts.ddd.*`` call runs the NEW runtime. Nothing errors; the
instructions and the code simply stop describing the same contract (0.2.528 run:
skills from 0.2.524, scripts from 0.2.528 — M7).

The Skill tool announces each skill's base directory
(``…/plugins/cache/canopy/canopy/<version>/skills/<name>``), so the version the
text came from is readable. This compares it with the runtime's ``VERSION``::

    python -m scripts.ddd.version_skew --skill-dir "<skill base directory>"

Exit 0 = same version (or undeterminable — nothing to say), 1 = skew (a
WARNING naming both versions and the fix: read the skill text from the runtime's
own plugin dir, ``<runtime>/../skills/<name>/SKILL.md``, or start a new session).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_VERSION_SEGMENT = re.compile(r"^\d+\.\d+\.\d+$")


def version_from_skill_dir(skill_dir: str | Path) -> str | None:
    """The plugin version a skill base directory belongs to, when the path says."""
    for part in reversed(Path(str(skill_dir)).parts):
        if _VERSION_SEGMENT.match(part):
            return part
    return None


def runtime_version(runtime_root: str | Path | None = None) -> str | None:
    """``VERSION`` of the runtime this module is running from (or *runtime_root*)."""
    root = Path(runtime_root).resolve() if runtime_root else Path(__file__).resolve().parents[2]
    # dev checkout: <repo>/VERSION
    try:
        text = (root / "VERSION").read_text().strip()
        if _VERSION_SEGMENT.match(text):
            return text
    except OSError:
        pass
    # installed bundle: <cache>/canopy/<version>/runtime -> the plugin's manifest
    try:
        import json

        manifest = json.loads((root.parent / ".claude-plugin" / "plugin.json").read_text())
        text = str(manifest.get("version") or "")
        if _VERSION_SEGMENT.match(text):
            return text
    except (OSError, ValueError):
        pass
    return version_from_skill_dir(root)


def check(skill_dir: str | Path, runtime_root: str | Path | None = None) -> dict:
    skill_v = version_from_skill_dir(skill_dir)
    runtime_v = runtime_version(runtime_root)
    skew = bool(skill_v and runtime_v and skill_v != runtime_v)
    message = None
    if skew:
        message = (
            f"WARNING: skill text is canopy {skill_v} (loaded at session start from "
            f"{skill_dir}) but the runtime is canopy {runtime_v}. The instructions and the "
            "scripts may describe different contracts. Read the skill from the runtime's "
            f"plugin dir instead (…/{runtime_v}/skills/<name>/SKILL.md), or start a new session."
        )
    return {"skill_version": skill_v, "runtime_version": runtime_v, "skew": skew, "message": message}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.version_skew")
    ap.add_argument("--skill-dir", required=True, help="the skill's base directory, as the Skill tool printed it")
    ap.add_argument("--runtime-root", default=None)
    args = ap.parse_args(argv)
    out = check(args.skill_dir, args.runtime_root)
    if out["skew"]:
        print(out["message"], file=sys.stderr)
        return 1
    print(f"version ok: skill {out['skill_version'] or '?'} / runtime {out['runtime_version'] or '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
