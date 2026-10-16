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
    Every scene. The first capture of a run, a change of render target, a change
    to the spec outside its scenes (setup, auth, viewport, base_url, ...), a
    changed scene count — WITH video — and every checkpoint / full-judge pass,
    as stills. Also whenever this module cannot tell what changed — a stale
    frame is worse than a re-film.
``scenes``
    Stills (``--capture-scenes``, ``--no-video``). After a recipe-only batch:
    the scenes whose RECIPE changed (url, actions, viewport, persona, pace,
    ``before:``) and a pending recipe re-judge's scenes; every other scene keeps
    its last capture (the recorder merges,
    :mod:`scripts.walkthrough._lib.scene_merge`). After a PRODUCT batch — named
    scenes or not — EVERY scene, so nothing is carried unverified (below).
``none``
    Nothing to film: the batch changed only narration / why-brief / the arc
    (the judges read the new words beside the old frame). The judge scope still
    re-judges the scenes whose spec changed.

Shared templates (DDD-2)
------------------------
A product batch can change a scene it did not name. Before DDD-2 a ``scenes``
pass re-filmed only the named scenes and marked the rest ``carried_unverified``,
so the pass could not decide and the next checkpoint re-filmed (with video) and
re-judged everything; and a batch with ONE finding whose scene the loop could
not read went straight to a full video re-film. Measured on 2026-10-15/16:
ACE noora (``newborn-care-classes-programme-cascade-2026-10-15-001``) — every
product batch "edited scene(s) [1..5]" (one labs template); ACE smallholder
(``smallholder-seasonal-programme-cascade-2026-10-16-001``) — iterations 1 and 2
were full video re-films because the arc judge named a scene by its spec id.

Now a product batch takes STILLS of every scene (cheap: ~15 s a scene, and a
reseeding ``setup`` replays the earlier scenes anyway) with
``verify_by_impact: true``, and :mod:`scripts.ddd.judge_scope` decides per scene
from :mod:`scripts.ddd.impact` (region DOM, region crops, the layout guard)
which actually changed; the rest keep their verdicts. Scene-less findings are
attributed by spec id first (:func:`scripts.ddd.fix_scope.attribute_scenes`);
what still has no scene goes to ``batch_plan.global`` — words (arc / narrative)
film nothing and re-run the arc judge; product means stills of every scene.
The safety rule is unchanged — if this module cannot tell, it re-captures — but
it re-captures as stills, and "cannot tell" no longer includes "a template".

When a pass records video
-------------------------
Only where a video is the thing being made or judged: the first capture (the
run's baseline clip, and the hero fallback when the Video phase is skipped), a
new render target, a spec change outside the scenes, a changed scene set, an
explicit ``--full-capture`` / ``--video`` (a pass that judges motion or pacing
off the clip itself). Nothing else needs it: every per-pass judge reads stills
— the concept judge scores ``motion_friction`` from the still frame plus the
action trace, the user and arc judges read frames and page text — and the
narrated video is made once, by the Video phase, after convergence. A video
re-film costs the recording AND the mp4 encode for every scene; on noora the
checkpoints at iterations 3–5 were each a full video re-film nobody watched.

``capture-plan.json`` in the run dir keeps the plan of every pass, and
``capture-ledger.json`` the recipe hash each scene's current frame was filmed
from — so a scene carried for several passes is still compared with the recipe
it was actually filmed with.

    python -m scripts.ddd.capture_scope plan <run_id> --spec <recipe>   # prints the plan
    python -m scripts.ddd.capture_scope report <run_dir|run_id> [--json]
        # per pass: what was filmed (video / stills / nothing) and re-judged, and why

The recorder refuses to write into a run dir without a plan for the run's
current iteration (:func:`recorder_guard`; ``--unplanned-capture "<why>"``
overrides it, and the reason is kept in ``capture-plan.json`` ``overrides`` and
flagged by ``report``).
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
    video: bool = False,
) -> dict[str, Any]:
    """Pure decision: ``{mode, scenes, video, reason, recipe_changed, product_scenes}``.

    ``video`` — this pass judges a video-specific dimension (motion / pacing off
    the clip itself): film with video whatever the mode. See the module doc,
    "When a pass records video".
    """
    every = sorted(int(s) for s in hashes["scenes"])

    def full(reason: str, *, with_video: bool = True) -> dict[str, Any]:
        with_video = with_video or video
        return {
            "mode": FULL, "scenes": every, "video": with_video,
            "reason": reason + ("" if with_video else
                                " — as stills: no judge of this pass reads the video "
                                "(motion_friction is scored from stills + the action trace)"),
        }

    def stills_of_all(reason: str, product: list[int], recipe: list[int]) -> dict[str, Any]:
        return {
            "mode": SCENES, "scenes": every, "video": bool(video), "reason": reason,
            "recipe_changed": recipe, "product_scenes": product,
            # Every scene is re-filmed, so nothing is carried and nothing is
            # unverified: the judge scope's impact fingerprint decides per scene.
            "carried_unverified": False, "verify_by_impact": True,
        }

    if not enabled:
        return full("scoped capture is off (loop.scoped_capture: false)")
    # ---- the cases that need a VIDEO re-film --------------------------------
    if not has_capture or not ledger:
        return full("no earlier capture in this run to keep (first capture)")
    if (ledger.get("base_url") or None) != (base_url or None):
        return full(f"render target changed ({ledger.get('base_url')} -> {base_url})")
    if ledger.get("top") != hashes["top"]:
        return full("the spec changed outside its scenes (setup, auth, viewport, base_url, ...)")
    prior = ledger.get("scenes") or {}
    if set(prior) != set(hashes["scenes"]):
        return full("the scene set changed")
    # ---- everything else: stills ---------------------------------------------
    if judge_full:
        return full("this pass judges every scene in full — every scene is re-filmed", with_video=False)
    recipe = sorted(int(s) for s, h in hashes["scenes"].items() if prior.get(s) != h)
    product: list[int] = []
    if isinstance(batch, dict) and batch.get("scope") == "product":
        named = batch.get("product_scenes")
        product = sorted(int(s) for s in (named if named is not None else batch.get("scenes") or []))
    if isinstance(batch, dict) and batch.get("unscoped"):
        return stills_of_all(
            "the last batch had a PRODUCT finding with no readable scene (a shared template "
            "can reach any scene) — stills of every scene; the impact fingerprint decides "
            "which actually changed",
            product, recipe,
        )
    if product:
        # A product fix lands in a template the batch's scenes share with scenes it
        # never named (ACE noora: every product batch was "edited scene(s) [1..5]").
        # Assuming they all changed re-judges everything; carrying the unnamed ones
        # unverified blocks the pass from deciding. Stills are cheap (~15 s a scene)
        # and the recorder replays earlier scenes anyway when setup reseeds — so
        # re-film them all as stills and let impact.py say which really moved.
        return stills_of_all(
            f"product batch edited scene(s) {product}"
            + (f"; recipe changed on scene(s) {recipe}" if recipe else "")
            + " — stills of every scene (a shared template reaches scenes the batch did not "
            "name); the impact fingerprint decides which actually changed, the rest keep their verdicts",
            product, recipe,
        )
    rr = sorted(int(s) for s in recipe_rejudge or [])
    want = sorted(set(recipe) | set(rr))
    if not want:
        if batch is None and not rr:
            return full("no batch plan says what changed since the last capture", with_video=False)
        g = (batch or {}).get("global") or {}
        return {
            "mode": NONE,
            "scenes": [],
            "video": False,
            "reason": "the batch changed only words (narration / why-brief"
            + (f"; {g['findings']} scene-less arc/narrative finding(s)" if g.get("findings") else "")
            + "): nothing on screen to re-film",
            "recipe_changed": [],
            "product_scenes": [],
        }
    reason = []
    if recipe:
        reason.append(f"recipe changed on scene(s) {recipe}")
    if rr:
        reason.append(f"recipe re-judge of scene(s) {rr}")
    return {
        "mode": SCENES,
        "scenes": want,
        "video": bool(video),
        "reason": "; ".join(reason) + " — stills of those scenes only, every other scene keeps its capture",
        "recipe_changed": recipe,
        "product_scenes": [],
        # Only the recipe moved and the recipe is per scene: what was carried
        # cannot have changed.
        "carried_unverified": False,
    }


def plan(
    run_id: str, spec_path: str | Path, *, base_url: str | None = None, force_full: bool = False,
    video: bool = False,
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
        video=video,
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
    out: dict[str, Any] = {"current": current, "passes": passes[-200:]}
    if prior.get("overrides"):
        out["overrides"] = prior["overrides"]  # a later plan never erases a recorded bypass
    (run / PLAN_FILE).write_text(json.dumps(out, indent=1) + "\n")


# ---------------------------------------------------------------------------
# recorder guard — no capture into a run dir without this pass's plan (DDD-3)
# ---------------------------------------------------------------------------
#
# ACE noora (``newborn-care-classes-programme-cascade-2026-10-15-001``)
# iterations 3–5: the recorder was run by hand, its command copied out of an
# earlier render log — ``--ddd-orchestrated`` included, so the hand-drive guard
# was silent. No plan was written, so none of the incremental rules applied
# (stills only, carry unchanged scenes, video once): each was a full video
# re-film nobody watched, and ``report`` could only call them "unplanned". The
# flag proves nothing about who is calling; a plan for THIS pass does — it is
# what ``iteration render`` writes immediately before it starts the recorder.

#: The recorder's escape hatch. Takes a reason, which is kept in capture-plan.json.
OVERRIDE_FLAG = "--unplanned-capture"


def ddd_run_dir(paths: list[str | Path | None]) -> Path | None:
    """The DDD run dir a recorder's output paths land in, or ``None``.

    A run dir is a directory holding ``run_state.yaml`` — wherever the runs root
    is (the external root has no ``.canopy/ddd/runs/`` segment). Checked: each
    path, its parent and grandparent (``<run>/snapshots/``, ``<run>/iter3_clip.mp4``).
    """
    for raw in paths:
        if not raw:
            continue
        p = Path(raw).expanduser().resolve()
        for cand in (p, *list(p.parents)[:2]):
            if (cand / "run_state.yaml").is_file():
                return cand
    return None


def _run_iteration(run: Path) -> int | None:
    try:
        import yaml

        it = (yaml.safe_load((run / "run_state.yaml").read_text()) or {}).get("iteration")
    except Exception:  # noqa: BLE001 — unreadable state: fall back to "any current plan"
        return None
    return it if isinstance(it, int) else None


def record_override(run_dir: str | Path, *, iteration: int | None, reason: str, argv: list[str]) -> dict:
    """Keep a recorder bypass in ``capture-plan.json`` ``overrides`` (never in ``passes``:
    it is not a plan, and the next ``plan`` must not mistake it for one)."""
    run = Path(run_dir)
    data = _load_json(run / PLAN_FILE) or {}
    entry = {
        "iteration": iteration,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video": "--no-video" not in argv,
        "argv": argv,
    }
    data["overrides"] = [*[o for o in data.get("overrides") or [] if isinstance(o, dict)], entry][-200:]
    (run / PLAN_FILE).write_text(json.dumps(data, indent=1) + "\n")
    return entry


def recorder_guard(
    out_paths: list[str | Path | None], *, override_reason: str | None, argv: list[str],
) -> dict[str, Any]:
    """May the recorder write these outputs? ``{ok, run_dir, iteration, message, override}``.

    Outside a DDD run dir: always. Inside one: only with a capture plan for the
    run's current iteration, or an explicit :data:`OVERRIDE_FLAG` reason (recorded).
    """
    run = ddd_run_dir(out_paths)
    if run is None:
        return {"ok": True, "run_dir": None, "iteration": None, "message": None, "override": None}
    it = _run_iteration(run)
    if load_plan(run, it) is not None:
        return {"ok": True, "run_dir": str(run), "iteration": it, "message": None, "override": None}
    reason = (override_reason or "").strip()
    if reason:
        entry = record_override(run, iteration=it, reason=reason, argv=argv)
        return {
            "ok": True, "run_dir": str(run), "iteration": it, "override": entry,
            "message": f"capture-plan guard overridden for iteration {it}: {reason} "
            f"(recorded in {run / PLAN_FILE}; `capture_scope report` flags it)",
        }
    return {
        "ok": False, "run_dir": str(run), "iteration": it, "override": None,
        "message": (
            f"Refusing to record into DDD run dir {run}: there is no capture plan for "
            f"iteration {it} ({PLAN_FILE}).\n"
            "Without one the incremental rules are skipped — stills only, unchanged scenes "
            "carried, video once — and every scene is re-filmed as video.\n"
            f"Render through:  python -m scripts.ddd.iteration render {run.name} --spec <recipe>\n"
            "(it plans the capture, then runs this recorder with the right flags).\n"
            f"For a deliberate one-off:  {OVERRIDE_FLAG} \"<why>\"  (recorded in {PLAN_FILE})."
        ),
    }


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
    no_video = [] if capture.get("video", True) else ["--no-video"]
    if capture.get("mode") != SCENES:
        return no_video
    return ["--capture-scenes", ",".join(str(s) for s in capture["scenes"]), *no_video]


# ---------------------------------------------------------------------------
# report — what every pass filmed and re-judged, and why (DDD-2)
# ---------------------------------------------------------------------------
#
# The incremental machinery existed for weeks while the runs re-walked
# everything; nobody could see it without reconstructing capture-plan.json and
# judge-scope.json by hand. This puts both on one table per pass, so a
# regression ("every pass re-judges every scene again") is visible on the first
# run it happens in.


def _pass_by_iteration(data: dict | None) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for p in (data or {}).get("passes") or []:
        if isinstance(p, dict) and isinstance(p.get("iteration"), int):
            out[p["iteration"]] = p  # the last plan of an iteration wins
    return out


def _context_only(changed: dict | None) -> list[int]:
    """Scenes whose ONLY moved input was the run-wide judge context."""
    return sorted(int(s) for s, comps in (changed or {}).items() if list(comps or []) == ["context"])


#: Recorded reasons that still mean a VIDEO re-film under the current rules.
_STILL_VIDEO = ("first capture", "no earlier capture", "render target changed",
                "outside its scenes", "scene set changed", "scoped capture is off",
                "--full-capture", "did not load")


def _replay(row: dict[str, Any], *, first: bool = False) -> dict[str, Any]:
    """What the CURRENT rules would have done on this recorded pass — an estimate
    read off the recorded reason, for measuring a change against an old run.

    Capture: a video re-film survives only for :data:`_STILL_VIDEO` reasons (and
    the run's first pass); a checkpoint, an unscoped batch or an unplanned render
    becomes stills of every scene. Judge: scenes re-judged on the context alone
    on an incremental pass are reused (the plan/record context mismatch is fixed).
    """
    c = row["capture"]
    reason = str(c.get("reason") or "")
    if c["mode"] == NONE:
        cap = {"mode": NONE, "video": False}
    elif first or any(k in reason for k in _STILL_VIDEO):
        cap = {"mode": c["mode"], "video": bool(c["video"])}
    elif c["mode"] == SCENES and "product batch" not in reason:
        cap = {"mode": SCENES, "video": False}
    else:
        cap = {"mode": "stills-all", "video": False}
    j = row.get("judge") or {}
    rejudged = [s for s in j.get("rejudged") or [] if s not in set(j.get("context_only") or [])]
    return {"capture": cap, "rejudged": rejudged if j else None}


def report(run_dir: str | Path) -> dict[str, Any]:
    """Per-pass capture + judge scope, joined by iteration, plus run totals."""
    run = Path(run_dir)
    plan_data = _load_json(run / PLAN_FILE)
    caps = _pass_by_iteration(plan_data)
    overrides: dict[int | None, list[dict]] = {}
    for o in (plan_data or {}).get("overrides") or []:
        if isinstance(o, dict):
            overrides.setdefault(o.get("iteration") if isinstance(o.get("iteration"), int) else None, []).append(o)
    from scripts.ddd import judge_scope

    judges = _pass_by_iteration(judge_scope.load_scope(run))
    iterations = sorted(set(caps) | set(judges) | {i for i in overrides if i is not None})
    rows: list[dict[str, Any]] = []
    for it in iterations:
        cap = caps.get(it)
        js = judges.get(it) or {}
        clip = (run / f"iter{it}_clip.mp4").exists()
        if cap:
            n_all = len(cap.get("scenes") or []) + len(cap.get("carried") or [])
            capture = {
                "mode": cap.get("mode"),
                "video": bool(cap.get("video")),
                "filmed": len(cap.get("scenes") or []) if cap.get("mode") != NONE else 0,
                "carried": len(cap.get("carried") or []),
                "scenes": n_all,
                "reason": cap.get("reason"),
                "verify_by_impact": bool(cap.get("verify_by_impact")),
            }
        else:
            # A render nobody planned (a hand-assembled recorder command): say so —
            # it is the most expensive kind of pass and the least visible one.
            capture = {
                "mode": "unplanned",
                "video": clip,
                "filmed": None,
                "carried": None,
                "scenes": None,
                "reason": "no capture-plan entry for this iteration (render not run through "
                "`iteration render`)" + ("; a clip was recorded" if clip else ""),
            }
        changed = js.get("changed_components") or {}
        rejudge = sorted(int(s) for s in js.get("rejudge") or [])
        row = {
            "iteration": it,
            "capture": capture,
            "judge": {
                "full": bool(js.get("full", True)) if js else None,
                "judges": js.get("judges"),
                "rejudged": rejudge,
                "reused": sorted(int(s) for s in js.get("reuse") or []),
                "held": sorted(int(s) for s in js.get("held") or []),
                "arc": js.get("arc"),
                "reason": js.get("reason"),
                "why": {str(s): changed.get(str(s)) for s in rejudge if changed.get(str(s))},
                # Only an incremental pass could have reused them; a full pass
                # re-judges every scene by design.
                "context_only": (
                    [s for s in _context_only(changed) if s in rejudge] if not js.get("full", True) else []
                ),
            } if js else None,
        }
        # A recorder run past the plan guard (--unplanned-capture): the reason the
        # operator gave, so a bypass is never mistaken for a planned pass.
        row["overrides"] = [
            {"reason": o.get("reason"), "at": o.get("at"), "video": bool(o.get("video"))}
            for o in overrides.get(it) or []
        ]
        row["replay"] = _replay(row, first=not rows)
        rows.append(row)
    totals = {
        "passes": len(rows),
        "video_refilms": sum(1 for r in rows if r["capture"]["video"]),
        "still_captures": sum(1 for r in rows if r["capture"]["mode"] in (FULL, SCENES) and not r["capture"]["video"]),
        "no_capture": sum(1 for r in rows if r["capture"]["mode"] == NONE),
        "unplanned": sum(1 for r in rows if r["capture"]["mode"] == "unplanned"),
        "scenes_rejudged": sum(len((r["judge"] or {}).get("rejudged") or []) for r in rows),
        "scenes_reused": sum(len((r["judge"] or {}).get("reused") or []) for r in rows),
        "context_only_rejudges": sum(len((r["judge"] or {}).get("context_only") or []) for r in rows),
        "overrides": sum(len(v) for v in overrides.values()),
    }
    replay = {
        "video_refilms": sum(1 for r in rows if r["replay"]["capture"]["video"]),
        "scenes_rejudged": sum(len(r["replay"]["rejudged"] or []) for r in rows),
    }
    return {
        "run_dir": str(run), "passes": rows, "totals": totals, "replay_totals": replay,
        # Bypasses whose run_state could not be read (no iteration to join on).
        "unattributed_overrides": overrides.get(None) or [],
    }


def format_report(rep: dict[str, Any]) -> str:
    lines = [f"capture/judge scope — {rep['run_dir']}", ""]
    lines.append(f"{'iter':>4}  {'capture':<16} {'filmed':>7}  {'judge':<6} {'rejudge':<16} {'reuse':<12} why")
    for r in rep["passes"]:
        c, j = r["capture"], r["judge"] or {}
        mode = f"{c['mode']}{'+video' if c['video'] else ''}"
        filmed = "-" if c["filmed"] is None else f"{c['filmed']}/{c['scenes']}"
        jmode = "-" if not j else ("full" if j.get("full") else "inc")
        lines.append(
            f"{r['iteration']:>4}  {mode:<16} {filmed:>7}  {jmode:<6} "
            f"{str(j.get('rejudged', '-')):<16} {str(j.get('reused', '-')):<12} {c.get('reason') or ''}"
        )
        for s, comps in (j.get("why") or {}).items():
            lines.append(f"{'':>6}scene {s} re-judged: {', '.join(comps)}")
        for o in r.get("overrides") or []:
            lines.append(
                f"{'':>6}!! recorder run past the capture-plan guard ({OVERRIDE_FLAG}"
                f"{', with video' if o.get('video') else ''}): {o.get('reason')}"
            )
        if j.get("context_only"):
            lines.append(
                f"{'':>6}!! scene(s) {j['context_only']} re-judged ONLY because the judge context "
                "moved — if the why-brief/rubric did not change, plan and record hashed different "
                "context files"
            )
    t = rep["totals"]
    lines += [
        "",
        f"passes {t['passes']}: video re-films {t['video_refilms']}, still captures {t['still_captures']}, "
        f"no capture {t['no_capture']}, unplanned renders {t['unplanned']}",
        f"scenes re-judged {t['scenes_rejudged']}, reused {t['scenes_reused']}, "
        f"re-judged on context alone {t['context_only_rejudges']}",
    ]
    if t.get("overrides"):
        lines.append(f"!! capture-plan guard overridden {t['overrides']} time(s) ({OVERRIDE_FLAG})")
    for o in rep.get("unattributed_overrides") or []:
        lines.append(f"!! override at {o.get('at')} (iteration unknown): {o.get('reason')}")
    rt = rep.get("replay_totals")
    if rt:
        lines.append(
            f"replayed under the current rules (estimate): video re-films {rt['video_refilms']}, "
            f"scenes re-judged {rt['scenes_rejudged']}"
        )
    return "\n".join(lines)


def _resolve_run_dir(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_dir():
        return p
    from scripts.ddd.runstate import run_dir_for

    return run_dir_for(arg)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.capture_scope")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="decide (and record) this pass's capture")
    p.add_argument("run_id")
    p.add_argument("--spec", required=True)
    p.add_argument("--base-url", default=None)
    p.add_argument("--video", action="store_true",
                   help="this pass judges motion/pacing off the clip itself: record video")
    r = sub.add_parser("report", help="per pass: what was filmed and re-judged, and why")
    r.add_argument("run", help="a run dir, or a run id")
    r.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "report":
        rep = report(_resolve_run_dir(args.run))
        print(json.dumps(rep, indent=1) if args.json else format_report(rep))
        return 0
    out = plan(args.run_id, args.spec, base_url=args.base_url, video=args.video)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
