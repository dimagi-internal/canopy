"""Capture scope — re-film only what a fix batch could have changed (canopy#785).

The problem, measured
---------------------
Every DDD pass re-recorded every scene as video. connect-labs
``supply-sophie-sheets-2026-10-06-001`` made 11 full recordings (9 scenes, a
~112 s mp4 each) and ACE Spark run ``-002`` 32, while the judges between
checkpoints read only each scene's still frame and page text, and most fixes
were product copy or styling on two or three scenes (Spark: ~169 PRODUCT, ~10
SCRIPTING, ~12 narration). The full narrated video is made once, by the Video
phase, after convergence.

The rule
--------
:func:`plan` picks one of three capture modes for the pass about to render:

``full``
    Every scene, with video. Every checkpoint and every pass that will judge in
    full, the first capture of a run, a change of render target, a change to the
    spec outside its scenes (setup, auth, viewport, base_url, ...), a changed
    scene count, or a batch whose findings name no scene. Also whenever this
    module cannot tell what changed — a stale frame is worse than a re-film.
``scenes``
    Stills only (``--capture-scenes``, ``--no-video``) of the scenes whose
    RECIPE changed (url, actions, viewport, persona, pace, ``before:``), plus the
    scenes a product batch edited (``state.batch_plan``) and a pending recipe
    re-judge's scenes. Every other scene keeps its last capture (the recorder
    merges, :mod:`scripts.walkthrough._lib.scene_merge`).
``none``
    Nothing to film: the batch changed only narration / why-brief (the judges
    read the new words beside the old frame). The judge scope still re-judges
    the scenes whose spec changed.

A product batch can change a scene it did not name (a shared template). So a
``scenes`` pass after a PRODUCT batch marks the scenes it carried as
``carried_unverified``: ``judge_scope plan`` lists them, and the pass cannot
decide anything (``target.decision_needs_checkpoint``) — the next checkpoint
re-films and re-judges everything, exactly as a held scene works today.

``capture-plan.json`` in the run dir keeps the plan of every pass, and
``capture-ledger.json`` the recipe hash each scene's current frame was filmed
from — so a scene carried for several passes is still compared with the recipe
it was actually filmed with.

    python -m scripts.ddd.capture_scope plan <run_id> --spec <recipe>   # prints the plan
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLAN_FILE = "capture-plan.json"
LEDGER_FILE = "capture-ledger.json"

FULL = "full"
SCENES = "scenes"
NONE = "none"

#: Scene fields the recorder films from. A change to any other scene field
#: (narrative, show, features, concept_claim, title, provenance, ...) is words
#: the judges read from the spec, not something on screen.
RECORDER_FIELDS = (
    "url",
    "actions",
    "viewport",
    "full_page",
    "pace",
    "persona",
    "before",
    "video_hold_seconds",
)
#: Top-level spec fields that only the deck / judges read; any OTHER top-level
#: change (setup, auth, base_url, viewport, personas, recorder config ...) can
#: change every frame.
WORDS_ONLY_TOP = ("name", "title", "narrative", "description", "why", "concept", "audience")


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def recipe_hashes(spec: dict) -> dict[str, Any]:
    """``{"top": hash, "scenes": {index: hash}}`` over what the recorder films from."""
    top = {k: v for k, v in spec.items() if k != "scenes" and k not in WORDS_ONLY_TOP}
    scenes = [s if isinstance(s, dict) else {} for s in spec.get("scenes") or []]
    return {
        "top": _sha(top),
        "scenes": {
            str(i): _sha({k: s.get(k) for k in RECORDER_FIELDS}) for i, s in enumerate(scenes, start=1)
        },
    }


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _spec_or_none(spec_path: str | Path) -> dict | None:
    from scripts.ddd.spec_io import load_spec_raw

    try:
        spec = load_spec_raw(spec_path)
    except Exception:  # noqa: BLE001 — the recorder reports a bad spec; here it only means "film it all"
        return None
    return spec if isinstance(spec, dict) else None


def load_ledger(run_dir: str | Path) -> dict | None:
    return _load_json(Path(run_dir) / LEDGER_FILE)


def load_plan(run_dir: str | Path, iteration: int | None = None) -> dict | None:
    """This iteration's plan (``None`` when the last plan was for another one)."""
    data = _load_json(Path(run_dir) / PLAN_FILE) or {}
    cur = data.get("current")
    if not isinstance(cur, dict):
        return None
    if iteration is not None and cur.get("iteration") != iteration:
        return None
    return cur


def decide(
    *,
    hashes: dict[str, Any],
    ledger: dict | None,
    base_url: str | None,
    judge_full: bool,
    batch: dict | None,
    recipe_rejudge: list[int] | None = None,
    has_capture: bool = True,
    enabled: bool = True,
) -> dict[str, Any]:
    """Pure decision: ``{mode, scenes, video, reason, recipe_changed, product_scenes}``."""

    def full(reason: str) -> dict[str, Any]:
        return {"mode": FULL, "scenes": sorted(int(s) for s in hashes["scenes"]), "video": True, "reason": reason}

    if not enabled:
        return full("scoped capture is off (loop.scoped_capture: false)")
    if judge_full:
        return full("this pass judges every scene in full — every scene is re-filmed")
    if not has_capture or not ledger:
        return full("no earlier capture in this run to keep")
    if (ledger.get("base_url") or None) != (base_url or None):
        return full(f"render target changed ({ledger.get('base_url')} -> {base_url})")
    if ledger.get("top") != hashes["top"]:
        return full("the spec changed outside its scenes (setup, auth, viewport, base_url, ...)")
    prior = ledger.get("scenes") or {}
    if set(prior) != set(hashes["scenes"]):
        return full("the scene set changed")
    if isinstance(batch, dict) and batch.get("unscoped"):
        return full("the last batch had a finding with no readable scene")
    recipe = sorted(int(s) for s, h in hashes["scenes"].items() if prior.get(s) != h)
    product: list[int] = []
    if isinstance(batch, dict) and batch.get("scope") == "product":
        product = sorted(int(s) for s in batch.get("scenes") or [])
    rr = sorted(int(s) for s in recipe_rejudge or [])
    want = sorted(set(recipe) | set(product) | set(rr))
    if not want:
        if batch is None and not rr:
            return full("no batch plan says what changed since the last capture")
        return {
            "mode": NONE,
            "scenes": [],
            "video": False,
            "reason": "the batch changed only words (narration / why-brief): nothing on screen to re-film",
            "recipe_changed": [],
            "product_scenes": [],
        }
    reason = []
    if recipe:
        reason.append(f"recipe changed on scene(s) {recipe}")
    if product:
        reason.append(f"product batch edited scene(s) {product}")
    if rr:
        reason.append(f"recipe re-judge of scene(s) {rr}")
    return {
        "mode": SCENES,
        "scenes": want,
        "video": False,
        "reason": "; ".join(reason) + " — stills of those scenes only, every other scene keeps its capture",
        "recipe_changed": recipe,
        "product_scenes": product,
        # A product change may reach scenes it did not name (a shared template):
        # what was carried is unverified until the next checkpoint re-films it.
        "carried_unverified": bool(product),
    }


def plan(
    run_id: str, spec_path: str | Path, *, base_url: str | None = None, force_full: bool = False
) -> dict[str, Any]:
    """Decide this pass's capture and record it in ``capture-plan.json``."""
    from scripts.ddd import loop_config, target
    from scripts.ddd.runstate import load, run_dir_for

    state = load(run_id)
    run = run_dir_for(run_id)
    cfg = loop_config.load()
    spec = _spec_or_none(spec_path)
    if spec is None:
        force_full = True
    hashes = recipe_hashes(spec or {})
    exp = target.expected_scope(state, cfg)
    bp = getattr(state, "batch_plan", None)
    batch = bp if isinstance(bp, dict) and bp.get("for_iteration") == state.iteration else None
    rr = getattr(state, "recipe_rejudge", None) or {}
    rr_scenes = (
        list(rr.get("scenes") or [])
        if rr.get("status") == "pending" and rr.get("iteration") == state.iteration
        else None
    )
    out = {"mode": FULL, "scenes": sorted(int(x) for x in hashes["scenes"]), "video": True,
           "reason": "full capture requested (--full-capture)" if spec is not None
           else "the spec did not load here — film every scene"} if force_full else decide(
        hashes=hashes,
        ledger=load_ledger(run),
        base_url=base_url,
        judge_full=bool(exp["full"]),
        batch=batch,
        recipe_rejudge=rr_scenes,
        has_capture=(run / "run-report.json").exists() and (run / "snapshots").is_dir(),
        enabled=cfg.loop.scoped_capture,
    )
    out = {**out, "iteration": state.iteration, "base_url": base_url,
           "planned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if out["mode"] == SCENES:
        out["carried"] = sorted(int(s) for s in hashes["scenes"] if int(s) not in out["scenes"])
    elif out["mode"] == NONE:
        out["carried"] = sorted(int(s) for s in hashes["scenes"])
    else:
        out["carried"] = []
    _write_plan(run, out)
    return out


def _write_plan(run: Path, current: dict[str, Any]) -> None:
    prior = _load_json(run / PLAN_FILE) or {}
    passes = [p for p in prior.get("passes") or [] if isinstance(p, dict)]
    passes = [p for p in passes if p.get("iteration") != current.get("iteration")] + [current]
    (run / PLAN_FILE).write_text(json.dumps({"current": current, "passes": passes[-200:]}, indent=1) + "\n")


def record_capture(run_dir: str | Path, spec_path: str | Path, captured: dict[str, Any]) -> dict[str, Any]:
    """After a successful capture: the recipe each scene's frame is now filmed from.

    Re-filmed scenes take the current recipe hash; carried scenes keep the hash
    they were filmed with.
    """
    run = Path(run_dir)
    spec = _spec_or_none(spec_path)
    if spec is None:
        (run / LEDGER_FILE).unlink(missing_ok=True)  # nothing to vouch for: the next pass films it all
        return {}
    hashes = recipe_hashes(spec)
    prior = load_ledger(run) or {}
    filmed = {str(s) for s in captured.get("scenes") or []} if captured.get("mode") != FULL else set(hashes["scenes"])
    scenes = {
        s: (h if s in filmed or s not in (prior.get("scenes") or {}) else prior["scenes"][s])
        for s, h in hashes["scenes"].items()
    }
    ledger = {
        "iteration": captured.get("iteration"),
        "base_url": captured.get("base_url"),
        # A partial capture cannot vouch for a top-level change it did not film.
        "top": hashes["top"] if captured.get("mode") == FULL else prior.get("top", hashes["top"]),
        "scenes": scenes,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (run / LEDGER_FILE).write_text(json.dumps(ledger, indent=1) + "\n")
    return ledger


def recorder_args(capture: dict[str, Any]) -> list[str]:
    """Extra ``record_video.py`` flags for a capture plan."""
    if capture.get("mode") != SCENES:
        return []
    return ["--capture-scenes", ",".join(str(s) for s in capture["scenes"]), "--no-video"]


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.capture_scope")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="decide (and record) this pass's capture")
    p.add_argument("run_id")
    p.add_argument("--spec", required=True)
    p.add_argument("--base-url", default=None)
    args = ap.parse_args(argv)
    out = plan(args.run_id, args.spec, base_url=args.base_url)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
