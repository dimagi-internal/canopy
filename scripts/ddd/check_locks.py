"""CI gate: every recipe has a lock, they agree, and no lock was hand-edited.

Deliberately OFFLINE. Re-fetching from canopy-web would make CI fail whenever
the site is down, and fail *legitimately* whenever someone approves a new
narrative version before re-pulling — turning a normal workflow into a red
build. What must hold locally is structural: the two files describe the same
scene set, and the lock carries story only.

The limit that buys: a lock can be BEHIND canopy-web and this stays green. That
is the accepted trade. What it cannot do is silently FORK — a stale lock is one
version behind and `pull` fixes it, which is categorically different from the
pre-L1 failure where three narratives existed in ten distinct local states.

    python -m scripts.ddd.check_locks docs/walkthroughs
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from scripts.ddd.spec_io import _LOCK_SCENE_FIELDS, _LOCK_TOP_FIELDS, lock_path

_RECIPE_ONLY_FIELDS = (
    "show", "url", "viewport", "full_page", "pace", "actions",
    "design_intent", "impressive_because", "concept_claim", "role",
)

# The other direction, and the one that cost a whole PR. ``compose`` merges the
# lock OVER the recipe for every lock-owned field, so a ``narrative:`` written
# into a recipe scene is overwritten and never reaches the renderer — silently,
# with an exit code of 0. An author edits the text, re-renders, measures no
# change, and has nothing to tell them why. Sourced from ``spec_io`` so the two
# can never drift.
_LOCK_OWNED_SCENE_FIELDS = _LOCK_SCENE_FIELDS
_LOCK_OWNED_TOP_FIELDS = _LOCK_TOP_FIELDS
_REQUIRED_LOCK_KEYS = ("slug", "version", "fetched_at", "name", "narrative", "scenes")


def check(base_dir) -> list[str]:
    """Return a list of human-readable problems. Empty means clean."""
    base = Path(base_dir)
    problems: list[str] = []

    for rpath in sorted(base.glob("*.recipe.yaml")):
        slug = rpath.name[: -len(".recipe.yaml")]
        lpath = lock_path(base, slug)
        if not lpath.exists():
            problems.append(
                f"{slug}: recipe has no narrative lock — run "
                f"`python -m scripts.ddd.narrative pull {slug} {base}`"
            )
            continue

        recipe = yaml.safe_load(rpath.read_text()) or {}
        try:
            lock = json.loads(lpath.read_text())
        except json.JSONDecodeError as e:
            problems.append(
                f"{slug}: lock is not valid JSON ({e}) — it has been hand-edited; re-pull it"
            )
            continue

        missing = [k for k in _REQUIRED_LOCK_KEYS if k not in lock]
        if missing:
            problems.append(
                f"{slug}: lock is missing generated key(s) {missing} — hand-edited or stale"
            )

        recipe_scenes_by_id = {
            (s.get("id") or "").strip(): s
            for s in (recipe.get("scenes") or [])
            if isinstance(s, dict)
        }
        recipe_ids = set(recipe_scenes_by_id)
        lock_scenes = lock.get("scenes") or []
        lock_ids = {(s.get("id") or "").strip() for s in lock_scenes}

        for sid in sorted(lock_ids - recipe_ids):
            problems.append(f"{slug}: lock scene {sid!r} has no recipe scene")
        for sid in sorted(recipe_ids - lock_ids):
            problems.append(f"{slug}: recipe scene {sid!r} is absent from the narrative")

        for s in lock_scenes:
            leaked = [f for f in _RECIPE_ONLY_FIELDS if f in s]
            if leaked:
                problems.append(
                    f"{slug}: lock scene {s.get('id')!r} carries recipe field(s) {leaked} "
                    f"— the lock has been hand-edited; re-pull it"
                )

        top_owned = [f for f in _LOCK_OWNED_TOP_FIELDS if f in recipe]
        if top_owned:
            problems.append(
                f"{slug}: recipe carries story field(s) {top_owned} at the top level "
                f"— compose overwrites them from the lock, so they never render. "
                f"Delete them; the story lives on canopy-web."
            )

        for sid in sorted(recipe_ids & lock_ids):
            rs = recipe_scenes_by_id.get(sid) or {}
            owned = [f for f in _LOCK_OWNED_SCENE_FIELDS if f in rs]
            if owned:
                problems.append(
                    f"{slug}: recipe scene {sid!r} carries story field(s) {owned} "
                    f"— compose overwrites them from the lock, so editing them here "
                    f"changes nothing that renders. Edit the story on canopy-web and "
                    f"`python -m scripts.ddd.narrative pull {slug} <dir>`, then delete "
                    f"the local copy."
                )

    return problems


def main(argv: list[str]) -> int:
    base = argv[0] if argv else "docs/walkthroughs"
    problems = check(base)
    for p in problems:
        print(p)
    if problems:
        print(
            f"\n{len(problems)} problem(s). The story lives on canopy-web and the lock "
            f"is generated: edit the story there and re-pull — never hand-edit the lock, "
            f"and never keep a local copy of the story in the recipe."
        )
        return 1
    print(f"{base}: locks clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
