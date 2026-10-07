"""Scene-scoped capture — record a few scenes, keep the rest of the last render.

Why this module exists (canopy#785)
-----------------------------------
A DDD fix batch usually touches two or three scenes, but every pass re-recorded
all of them: a ``--scene`` render overwrote ``run-report.json`` and the manifest
with just the scenes it filmed, which broke every other scene's trace
fingerprint and the deck. So ``record_video.py --scenes 3,6`` captures scenes 3
and 6 into a scratch directory and this module folds them into what is already
on disk:

* the target scenes' files (``scene_<N>.png``, ``_before.png``,
  ``_page_text.json``, ``_regions.json``, ``_visual.json``) replace theirs; every
  other scene's files are left alone;
* the run report keeps the prior entries of every other scene (actions, timing,
  load waits, hooks) and takes the new ones for the targets;
* a reseeded take binds new ids, so each carried scene keeps the ``${var}``
  bindings it was filmed with in ``scene_variables`` — a fingerprint of a carried
  scene must un-substitute ITS ids, not this take's.

Which scenes must be REPLAYED to capture a target (:func:`replay_scenes`) is the
other half: a scene whose state an earlier scene builds (a reseeded narrative,
a scene with no ``url`` of its own, a ``${var}`` captured on camera earlier)
cannot be filmed alone.

Stdlib only — the merge is unit-tested without a browser.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Iterable

#: Per-scene files the recorder writes into ``--snapshots``.
SCENE_FILE_SUFFIXES = ("", "_before", "_page_text", "_regions", "_visual")
_SCENE_FILE = re.compile(r"^scene_(\d+)(_before|_page_text|_regions|_visual)?\.(png|json)$")
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_scenes(raw: str | None) -> list[int]:
    """``"3,6"`` / ``"2-4"`` / ``"2-4,7"`` -> sorted 1-based scene indices."""
    out: set[int] = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            out.update(range(min(lo, hi), max(lo, hi) + 1))
        else:
            out.add(int(part))
    if any(s < 1 for s in out):
        raise ValueError(f"scene indices are 1-based: {raw!r}")
    return sorted(out)


def _placeholders(obj: Any) -> set[str]:
    if isinstance(obj, str):
        return set(_PLACEHOLDER.findall(obj))
    if isinstance(obj, list):
        return set().union(*(_placeholders(x) for x in obj)) if obj else set()
    if isinstance(obj, dict):
        return set().union(*(_placeholders(v) for v in obj.values())) if obj else set()
    return set()


def _captured_vars(scene: dict) -> set[str]:
    return {
        str(a.get("capture_var") or a.get("var") or "")
        for a in scene.get("actions") or []
        if isinstance(a, dict) and (a.get("kind") == "capture")
    } - {""}


def replay_scenes(spec: dict, targets: Iterable[int]) -> tuple[list[int], str]:
    """``(scenes to run, why)`` so every target is filmed in the state it expects.

    Only the targets run when the narrative does not mutate state per take and
    every target opens its own url with no ``${var}`` an earlier scene captured
    on camera. Otherwise every scene from 1 to the last target runs, the
    non-targets off the record (flow pace, no files kept).
    """
    scenes = [s if isinstance(s, dict) else {} for s in spec.get("scenes") or []]
    want = sorted({int(t) for t in targets if 1 <= int(t) <= len(scenes)})
    if not want:
        return [], "no target scene exists in the spec"
    setup = spec.get("setup") if isinstance(spec.get("setup"), dict) else None
    upto = list(range(1, want[-1] + 1))
    if setup and str(setup.get("rerun") or "").strip() == "per_render":
        return upto, "setup reseeds every take (rerun: per_render): earlier scenes build the state"
    captured_earlier: set[str] = set()
    for i, scene in enumerate(scenes, start=1):
        if i in want:
            first = (scene.get("actions") or [None])[0]
            opens = bool(scene.get("url")) or (isinstance(first, dict) and first.get("kind") == "goto")
            if not opens:
                return upto, f"scene {i} has no url of its own: it continues from the scene before"
            used = _placeholders(scene) & captured_earlier
            if used:
                return upto, f"scene {i} uses {sorted(used)}, captured on camera by an earlier scene"
            if scene.get("before"):
                return upto, f"scene {i} has a before: hook that may depend on earlier state"
        captured_earlier |= _captured_vars(scene)
    return want, "every target opens its own url and needs no earlier scene's state"


def scene_files(snapshots: Path, scene: int) -> list[Path]:
    """The files the recorder wrote for one scene."""
    out = []
    for suffix in SCENE_FILE_SUFFIXES:
        for ext in ("png", "json"):
            p = snapshots / f"scene_{scene}{suffix}.{ext}"
            if p.exists():
                out.append(p)
    return out


def adopt_scene_files(scratch: Path, snapshots: Path, targets: Iterable[int]) -> list[str]:
    """Move the targets' files from ``scratch`` into ``snapshots``.

    A target's previous files are removed first, so a frame the new take did not
    write (a ``_before`` frame the scene no longer has) does not survive it.
    """
    snapshots.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for scene in sorted(set(targets)):
        for old in scene_files(snapshots, scene):
            old.unlink()
        for new in scene_files(scratch, scene):
            dest = snapshots / new.name
            shutil.move(str(new), str(dest))
            moved.append(dest.name)
    return moved


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        text = str(value).strip()
        return text if len(text) >= 2 else None
    return None


def bindings(report: dict | None) -> dict[str, str]:
    """``{name: value}`` a render bound (setup variables, then on-camera captures).

    Mirrors :func:`scripts.ddd.stable_ids.resolved_vars`, which reads these
    reports; kept here so the recorder stays free of DDD imports.
    """
    out: dict[str, str] = {}
    if not isinstance(report, dict):
        return out
    setup = report.get("setup") if isinstance(report.get("setup"), dict) else {}
    for name, value in (setup.get("variables") or {}).items():
        text = _scalar(value)
        if text is not None:
            out[str(name)] = text
    for action in report.get("actions") or []:
        if isinstance(action, dict) and action.get("capture_var"):
            text = _scalar(action.get("capture_value"))
            if text is not None:
                out[str(action["capture_var"])] = text
    return out


def _scene_of(entry: Any) -> int | None:
    if not isinstance(entry, dict):
        return None
    try:
        return int(entry.get("scene_index"))
    except (TypeError, ValueError):
        return None


def merge_report(prior: dict | None, new: dict, targets: Iterable[int]) -> dict:
    """The run report after a scoped take: ``new`` for the targets, ``prior`` elsewhere."""
    want = {int(t) for t in targets}
    prior = prior if isinstance(prior, dict) else {}
    merged: dict[str, Any] = dict(new)

    def pick(key: str) -> list:
        kept = [e for e in prior.get(key) or [] if _scene_of(e) not in want]
        fresh = [e for e in new.get(key) or [] if _scene_of(e) in want]
        return sorted(kept + fresh, key=lambda e: (_scene_of(e) or 0))

    for key in ("actions", "scenes", "load_waits", "scene_hooks"):
        if prior.get(key) or new.get(key):
            merged[key] = pick(key)
    actions = merged.get("actions") or []
    merged["total"] = len(actions)
    merged["ok"] = sum(1 for a in actions if isinstance(a, dict) and a.get("ok"))
    merged["failed"] = merged["total"] - merged["ok"]

    carried = sorted({s for s in (_scene_of(e) for e in prior.get("scenes") or []) if s is not None} - want)
    prior_vars = prior.get("scene_variables") if isinstance(prior.get("scene_variables"), dict) else {}
    take_vars = bindings(prior)
    scene_vars = {
        str(s): dict(prior_vars.get(str(s)) or take_vars) for s in carried if prior_vars.get(str(s)) or take_vars
    }
    if scene_vars:
        merged["scene_variables"] = scene_vars
    merged["captured_scenes"] = sorted(want)
    merged["carried_scenes"] = carried
    return merged


class ReportView:
    """Enough of :class:`RunReport` for ``build_manifest`` over a merged report dict."""

    def __init__(self, report: dict) -> None:
        self._timing = {
            s: e for e in report.get("scenes") or [] if (s := _scene_of(e)) is not None
        }

    def scene_timing_for(self, scene_index: int) -> dict:
        return self._timing.get(int(scene_index), {})


__all__ = [
    "ReportView",
    "adopt_scene_files",
    "bindings",
    "merge_report",
    "parse_scenes",
    "replay_scenes",
    "scene_files",
]
