"""Judge scope — re-judge only what could have changed; reuse cells whose inputs did not.

The problem, measured
---------------------
Every DDD iteration re-rendered AND re-judged every scene. One judge round on a
13-scene narrative costs ~14 minutes and ~600k tokens; a fix cycle costs ~4
minutes and no judge tokens (connect-labs ``.canopy/ddd/learnings.md``,
2026-07-30). On a freshly-built (v1) product the loop spends most of its budget
re-judging scenes no fix touched, and a re-draw of byte-identical inputs only
samples judge noise (+/-1 per cell) — it cannot tell the loop anything new.

The v2 fingerprint still reused nothing on the ACE Spark runs (canopy#780): it
compared each frame byte for byte, so a fix batch that edited a shared report
template moved every scene on that template. v3 (:mod:`scripts.ddd.impact`) asks
what the scene is ABOUT — its action targets and the elements its narration
names, captured by the recorder in ``scene_<N>_regions.json`` — and compares
those elements' DOM text and their crops (tolerant of anti-aliasing), the page
text minus volatile stamps, and a whole-frame LAYOUT guard so a large layout
change still re-judges.

The protocol
------------
Capture is scoped separately (:mod:`scripts.ddd.capture_scope`, canopy#785):
between checkpoints only the scenes a batch changed are re-filmed, as stills,
and merged into the last render — a carried scene's files are byte-identical,
so it fingerprints unchanged. Judging is scoped:

1. ``plan``   — fingerprint every scene's judge INPUTS per component
   (:mod:`scripts.ddd.impact`: ``context``, ``spec``, ``trace``, ``page_text``,
   ``region_dom``, ``region_image``, ``layout``) and compare each with the
   ledger of the last judged iteration. A scene none of whose components moved
   is REUSED; anything else is RE-JUDGED, and ``changed_components`` says which
   input moved. A full pass (``state.next_judge_full``, or no ledger yet)
   re-judges everything. The arc judge re-runs on a full pass or when any scene
   is re-judged. With judge tiering, an incremental pass runs the concept judge
   and — FLOOR-FIRST (:mod:`scripts.ddd.floor`) — the judge holding the gating
   floor, on the floor's scenes, whenever those scenes changed. Writes
   ``judge-scope.json``; every plan is also appended to its ``passes`` list,
   so the scope of every pass of the run stays auditable.
2. ``carry``  — before the concept judge runs, archive stale pass files of the
   scenes being re-judged and restore the reused scenes' SEALED pass files
   (payload + seal, byte-for-byte — the seal stays valid because nothing about
   the pass changed). The concept eval then scores from ``passes/concept/`` as
   always; reused scenes' confirmed cells flow in unchanged.
3. ``merge-user`` — the user-artifact judge dispatches only its scenes
   (``user_scenes`` on a floor-first pass, else the re-judged scenes); this
   merges their rows over the ledger's so the verdict still covers every scene.
4. ``record`` — after assembly, snapshot this iteration's fingerprints, image
   signatures, pass files and verdicts into ``judge-cache/`` as the next
   iteration's ledger.

Convergence is never declared on a reused cell: ``compute_auto_iterate`` answers
``confirm_full`` for an incremental pass that would converge.

    python -m scripts.ddd.judge_scope plan   <run_dir> <spec> [--context F ...]
        # scope derived from <run_dir>/run_state.yaml; --full only with --reason
        # when the state asked for an incremental pass
    python -m scripts.ddd.judge_scope carry  <run_dir>
    python -m scripts.ddd.judge_scope merge-user <run_dir> <partial-verdict-user.yaml>
    python -m scripts.ddd.judge_scope record <run_dir> <spec> --iteration N [--context F ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SCOPE_FILE = "judge-scope.json"
CACHE_DIR = "judge-cache"
INDEX_FILE = "index.json"
SIGNATURES_FILE = "signatures.json"
#: How many past plans ``judge-scope.json`` keeps in ``passes`` (a run is capped
#: at ~10 iterations plus re-judges; this only bounds a pathological loop).
HISTORY_LIMIT = 200
_SCENE_PASS_RE = re.compile(r"^scene_(\d+)(?:_r\d+)?\.json(?:\.seal\.json)?$")
_ARC_FILES = ("verdict-arc.yaml", "arc_findings.json")
# v1 (<= 0.2.528): one hash over resolved targets and raw page text.
# v2: per-component hashes; reseeded ids compared in their ${var} spec form.
# v3 (canopy#780): the frame is no longer compared byte for byte — region DOM +
#     region crops (anti-aliasing tolerant) + a whole-frame layout guard; page
#     text minus volatile stamps.
FINGERPRINT_VERSION = 3


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()


def _find(run_dir: Path, name: str) -> Path | None:
    for base in (run_dir / "snapshots", run_dir):
        p = base / name
        if p.exists():
            return p
    return None


def _page_text_payload(path: Path | None, variables: dict[str, str] | None = None) -> Any:
    """Captured page text WITHOUT the per-render stamp (render_id changes every take)
    and without volatile stamps (clock times, timestamps, "N minutes ago").

    With *variables* (the render's ``${var}`` bindings), id-shaped values are put
    back into their ``${name}`` form, so a reseeded id in the url or text does not
    read as a change (:mod:`scripts.ddd.stable_ids`).
    """
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return path.read_bytes().hex()
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k != "render_id"}
        if isinstance(data.get("page_text"), str):
            from scripts.ddd.impact import scrub_volatile

            data["page_text"] = scrub_volatile(data["page_text"])
    if variables:
        from scripts.ddd.stable_ids import id_vars, unsubstitute_deep

        data = unsubstitute_deep(data, id_vars(variables))
    return data


def _load_report(run_dir: Path) -> dict | None:
    report = run_dir / "run-report.json"
    if not report.exists():
        return None
    try:
        data = json.loads(report.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def render_variables(run_dir: str | Path) -> dict[str, str]:
    """The ``${var}`` bindings of the render in *run_dir* (empty when unknown)."""
    from scripts.ddd.stable_ids import resolved_vars

    return resolved_vars(_load_report(Path(run_dir)))


def _trace_by_scene(run_dir: Path) -> dict[int, list]:
    data = _load_report(run_dir)
    if data is None:
        return {}
    try:
        from scripts.walkthrough._lib.results import action_trace_by_scene

        traces = action_trace_by_scene(data)
    except Exception:
        return {}
    from scripts.ddd.stable_ids import scene_vars, unsubstitute

    # Only what a judge reasons over and is stable take-to-take — notes can
    # carry timings. The target is compared in its SPEC form: a reseeded id
    # (``${round2_tender_id}`` 90 -> 92) is the same action — with the bindings
    # the scene was filmed with (a carried scene keeps its own, canopy#785).
    return {
        int(k): [
            [
                a.get("kind"),
                unsubstitute(a.get("target"), scene_vars(data, k)),
                bool(a.get("ok")),
                bool(a.get("must_succeed")),
            ]
            for a in v
        ]
        for k, v in traces.items()
    }


def context_salt(paths: list[str | Path]) -> str:
    """Run-wide judge context (why-brief, rubric). A change re-judges every scene."""
    h = hashlib.sha256()
    for p in sorted(str(x) for x in paths):
        path = Path(p)
        h.update(path.name.encode())
        h.update(path.read_bytes() if path.exists() else b"<missing>")
    return h.hexdigest()


def spec_scenes(spec_path: str | Path) -> dict[int, dict]:
    """``{1-based scene index: scene dict}`` from a (possibly two-file) spec."""
    from scripts.ddd.spec_io import load_spec_raw

    raw = load_spec_raw(spec_path)
    scenes = raw.get("scenes") or []
    return {i: (s if isinstance(s, dict) else {}) for i, s in enumerate(scenes, start=1)}


def scene_inputs(
    run_dir: str | Path, scenes: dict[int, dict], *, salt: str = ""
) -> tuple[dict[str, dict[str, str]], dict[str, dict]]:
    """``(components, signatures)`` — each judge input of each scene, hashed.

    ``components`` is ``{scene: {component: sha256}}`` over
    :data:`scripts.ddd.impact.COMPONENTS`; ``signatures`` holds what the
    tolerant image comparisons need (region crops, layout grids). See
    :mod:`scripts.ddd.impact` for what each component covers and why.
    """
    from scripts.ddd import impact

    from scripts.ddd.stable_ids import scene_vars

    run = Path(run_dir)
    report = _load_report(run)
    traces = _trace_by_scene(run)
    comps: dict[str, dict[str, str]] = {}
    sigs: dict[str, dict] = {}
    for idx, scene in sorted(scenes.items()):
        variables = scene_vars(report, idx)
        text = _page_text_payload(_find(run, f"scene_{idx}_page_text.json"), variables)
        image, sig = impact.scene_capture(
            _find(run, f"scene_{idx}.png"),
            _find(run, f"scene_{idx}_before.png"),
            _find(run, f"scene_{idx}_regions.json"),
            variables,
        )
        comps[str(idx)] = {
            "context": hashlib.sha256(salt.encode()).hexdigest(),
            "page_text": hashlib.sha256(_canonical(text)).hexdigest(),
            "spec": hashlib.sha256(_canonical(scene)).hexdigest(),
            "trace": hashlib.sha256(_canonical(traces.get(idx, []))).hexdigest(),
            **image,
        }
        sigs[str(idx)] = sig
    return comps, sigs


def fingerprint_components(
    run_dir: str | Path, scenes: dict[int, dict], *, salt: str = ""
) -> dict[str, dict[str, str]]:
    """``{scene: {component: sha256}}`` — each judge input hashed on its own.

    Kept separately so a plan can say WHICH input changed. Equal hashes mean an
    identical input; DIFFERENT hashes on ``region_image`` / ``layout`` are then
    compared tolerantly by :func:`scripts.ddd.impact.compare_scene`.
    """
    return scene_inputs(run_dir, scenes, salt=salt)[0]


def _combine(components: dict[str, str]) -> str:
    return hashlib.sha256(_canonical(components)).hexdigest()


def fingerprints(
    run_dir: str | Path, scenes: dict[int, dict], *, salt: str = ""
) -> dict[str, str]:
    """``{scene index (str): sha256}`` over every input a scene's judges read."""
    return {
        s: _combine(c)
        for s, c in fingerprint_components(run_dir, scenes, salt=salt).items()
    }


def changed_components(
    current: dict[str, dict[str, str]], prior: dict[str, dict[str, str]] | None
) -> dict[str, list[str]]:
    """``{scene: [component, ...]}`` for scenes whose components differ from *prior*."""
    out: dict[str, list[str]] = {}
    for scene, comps in current.items():
        before = (prior or {}).get(scene)
        if not isinstance(before, dict):
            continue
        diff = sorted(k for k in comps if before.get(k) != comps[k])
        if diff:
            out[scene] = diff
    return out


# ---------------------------------------------------------------------------
# Ledger (judge-cache/)
# ---------------------------------------------------------------------------


def load_ledger(run_dir: str | Path) -> dict | None:
    p = Path(run_dir) / CACHE_DIR / INDEX_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load_signatures(run_dir: str | Path) -> dict:
    """The ledger's image signatures (``{}`` when absent — every image compare then
    falls back to its hash, i.e. any difference re-judges)."""
    p = Path(run_dir) / CACHE_DIR / SIGNATURES_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def impact_changes(
    components: dict[str, dict[str, str]],
    signatures: dict[str, dict],
    ledger: dict | None,
    prior_signatures: dict | None,
    *,
    edited: set[str] | None = None,
) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """``({scene: [changed component, ...]}, {scene: detail})`` against the ledger.

    Only scenes with at least one changed component appear. A scene the ledger
    has no components for lists every component (it cannot be reused).
    ``edited`` (scene keys the last fix batch touched) never gets a crop change
    explained away by a reseeded id (:func:`scripts.ddd.impact.compare_scene`).
    """
    from scripts.ddd import impact

    prior = (ledger or {}).get("components") or {}
    changed: dict[str, list[str]] = {}
    detail: dict[str, dict] = {}
    for scene, comps in components.items():
        diff, why = impact.compare_scene(
            comps, prior.get(scene), signatures.get(scene), (prior_signatures or {}).get(scene),
            explain=scene not in (edited or set()),
        )
        if diff:
            changed[scene] = diff
        if why and (diff or "region_image_explained" in why):
            detail[scene] = why
    return changed, detail


def decide_scope(
    current: dict[str, str],
    ledger: dict | None,
    *,
    force_full: bool,
    full_reason: str | None = None,
    judge_scenes: list[int] | None = None,
    changed: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Pure decision: which scenes to re-judge, which to reuse, whether arc re-runs.

    ``changed`` (from :func:`impact_changes`) decides which scenes moved; without
    it a scene moved when its combined fingerprint differs from the ledger's.

    ``judge_scenes`` (a recipe-only batch, :func:`scripts.ddd.fix_scope.batch_plan`)
    limits an incremental pass to the scenes the batch edited: any OTHER scene
    whose inputs moved (a per-render reseed, render noise) is HELD to its ledger
    cells and listed in ``held``. A pass that held anything cannot decide
    (``target.decision_needs_checkpoint``); the next checkpoint judges it fresh."""
    scenes = sorted(current, key=int)
    prior = (ledger or {}).get("fingerprints") or {}
    if force_full or not ledger:
        reason = (
            (full_reason or "full pass requested")
            if force_full
            else "no ledger from a prior judged iteration"
        )
        return {
            "full": True,
            "rejudge": [int(s) for s in scenes],
            "reuse": [],
            "arc": True,
            "reason": reason,
        }
    if changed is not None:
        moved = [s for s in scenes if changed.get(s)]
    else:
        moved = [s for s in scenes if prior.get(s) != current[s]]
    held: list[str] = []
    if judge_scenes is not None:
        allowed = {str(int(x)) for x in judge_scenes}
        held = [s for s in moved if s not in allowed]
        moved = [s for s in moved if s in allowed]
    same = [s for s in scenes if s not in moved]
    out = {
        "full": False,
        "rejudge": [int(s) for s in moved],
        "reuse": [int(s) for s in same],
        "arc": bool(moved),
        "reason": (
            f"{len(moved)} scene(s) changed since iteration {ledger.get('iteration')}; "
            f"{len(same)} unchanged scene(s) reuse their cells"
            + ("" if moved else " — NOTHING changed: the last batch had no visible effect")
        ),
    }
    if held:
        out["held"] = [int(s) for s in held]
        out["reason"] += (
            f" — recipe-only batch: scene(s) {out['held']} moved but the batch did not edit "
            "them, so they keep their ledger cells until the next checkpoint"
        )
    return out


def _floor_judges(
    floor: dict | None, rejudge: list[int], *, all_scenes: list[int]
) -> tuple[list[str], dict[str, list[int]], str]:
    """Floor-first (:mod:`scripts.ddd.floor`): which non-concept judges join an
    incremental tiered pass, on which scenes, and why.

    The judge holding the gating floor re-judges the floor's scenes whenever
    those scenes' inputs changed. When they did not, its carried cell IS the
    floor — re-drawing identical inputs only samples judge noise. A floor whose
    scenes are unknown re-judges every changed scene with that judge.
    """
    if not floor:
        return [], {}, ""
    extra: list[str] = []
    scenes_by: dict[str, list[int]] = {}
    notes: list[str] = []
    changed = set(rejudge)
    for judge in floor.get("judges") or []:
        if judge == "concept":
            continue  # the concept judge already re-judges every changed scene
        if judge == "arc":
            if changed:
                extra.append("arc")
                notes.append("arc holds the floor and scenes changed: arc re-runs")
            else:
                notes.append("arc holds the floor; nothing changed, so its verdict is carried")
            continue
        cells = [c for c in floor.get("cells") or [] if c.get("judge") == judge]
        wanted = sorted({s for c in cells for s in c.get("scenes") or []}) or list(all_scenes)
        scenes = sorted(s for s in wanted if s in changed)
        dims = sorted({c.get("dimension") for c in cells if c.get("dimension")})
        if scenes:
            extra.append(judge)
            scenes_by[judge] = scenes
            notes.append(
                f"{judge} holds the floor ({', '.join(dims) or 'overall'} = {floor.get('score')}) "
                f"on scene(s) {wanted}: it re-judges {scenes}"
            )
        else:
            notes.append(
                f"{judge} holds the floor on scene(s) {wanted}, none of which changed: "
                "the floor cell is carried (the last batch did not touch it)"
            )
    return extra, scenes_by, "; ".join(notes)


def _append_history(run: Path, scope: dict[str, Any], iteration: Any) -> list[dict]:
    """Every plan of the run, oldest first — the audit trail ``judge-scope.json``
    used to lose by overwriting itself each pass."""
    prior = load_scope(run) or {}
    passes = [p for p in prior.get("passes") or [] if isinstance(p, dict)]
    entry = {
        k: scope.get(k)
        for k in (
            "planned_at", "ledger_iteration", "full", "judges", "rejudge", "reuse", "held",
            "user_scenes", "arc", "reason", "changed_components", "impact", "floor",
            "would_reuse", "override", "capture", "carried_unverified",
        )
        if scope.get(k) is not None
    }
    entry["iteration"] = iteration
    passes.append(entry)
    return passes[-HISTORY_LIMIT:]


def plan(
    run_dir: str | Path,
    spec_path: str | Path,
    *,
    force_full: bool = False,
    context: list[str | Path] | None = None,
    tiered: bool = False,
    full_reason: str | None = None,
    judge_scenes: list[int] | None = None,
    override: dict | None = None,
    floor: dict | None = None,
    iteration: int | None = None,
    edited_scenes: list[int] | str | None = None,
) -> dict[str, Any]:
    """Plan the judge scope.

    ``edited_scenes`` — the scenes the last fix batch touched
    (``state.batch_plan``): their crop changes are never explained away by a
    reseeded id, so a fix that lands on an id-bearing element re-judges.
    ``"all"`` when the batch had a finding with no readable scene.

    ``tiered`` (judge tiering, :mod:`scripts.ddd.target`): an INCREMENTAL pass
    runs the concept judge on the changed scenes, plus — when ``floor`` names
    one — the judge holding the gating floor on the floor's changed scenes
    (``user_scenes``); ``carry`` restores the last verdicts of every judge that
    does not run. A full pass always runs every judge.

    ``changed_components`` (per scene, which input moved) and ``impact`` (the
    tolerant image comparisons behind ``region_image`` / ``layout``) are written
    on every pass that has a comparable ledger — a full pass included, with
    ``would_reuse`` naming the scenes an incremental pass would have reused.
    """
    run = Path(run_dir)
    ctx = list(context or [])
    wb = run / "why_brief.yaml"
    if wb.exists() and str(wb) not in {str(c) for c in ctx}:
        ctx.append(wb)
    components, signatures = scene_inputs(run, spec_scenes(spec_path), salt=context_salt(ctx))
    current = {s: _combine(c) for s, c in components.items()}
    ledger = load_ledger(run)
    comparable = bool(
        ledger
        and set((ledger.get("fingerprints") or {})) == set(current)
        and ledger.get("fingerprint_version") == FINGERPRINT_VERSION
    )
    changed: dict[str, list[str]] | None = None
    detail: dict[str, dict] = {}
    if comparable:
        changed, detail = impact_changes(
            components, signatures, ledger, load_signatures(run),
            edited=(
                set(current)
                if edited_scenes == "all"
                else {str(int(x)) for x in edited_scenes or []}
            ),
        )
    # A ledger recorded against a different scene set cannot be reused safely.
    if ledger and set((ledger.get("fingerprints") or {})) != set(current):
        scope = decide_scope(current, None, force_full=True)
        scope["reason"] = "scene set changed since the ledger — full pass"
    elif ledger and ledger.get("fingerprint_version") != FINGERPRINT_VERSION and not force_full:
        # A ledger hashed by an older scheme cannot match any current fingerprint;
        # say so instead of reporting every scene as "changed".
        scope = decide_scope(current, None, force_full=True)
        scope["reason"] = (
            f"ledger fingerprints use scheme v{ledger.get('fingerprint_version', 1)}, "
            f"this plan uses v{FINGERPRINT_VERSION} — full pass (reuse resumes next iteration)"
        )
    else:
        scope = decide_scope(
            current, ledger, force_full=force_full, full_reason=full_reason,
            judge_scenes=judge_scenes, changed=changed,
        )
    if override:
        scope["override"] = override
    if changed is not None:
        scope["changed_components"] = changed
        if detail:
            scope["impact"] = detail
        if scope.get("full"):
            scope["would_reuse"] = sorted(int(s) for s in current if not changed.get(s))
    if floor:
        scope["floor"] = {
            k: floor.get(k) for k in ("score", "judges", "dimensions", "scenes", "iteration")
            if floor.get(k) is not None
        }
    from scripts.ddd import capture_scope

    cap = capture_scope.load_plan(run, iteration) if iteration is not None else None
    if cap:
        scope["capture"] = {k: cap.get(k) for k in ("mode", "scenes", "carried")}
        if cap.get("carried_unverified") and cap.get("carried") and not scope.get("full"):
            # A product batch re-filmed only the scenes it named; what it carried
            # may sit on a template it changed. Like a held scene, that is not a
            # read this pass may decide on (canopy#785).
            scope["carried_unverified"] = sorted(int(s) for s in cap["carried"])
            scope["reason"] += (
                f" — scoped capture after a product batch: scene(s) {scope['carried_unverified']} "
                "were not re-filmed, so this pass cannot decide (the next checkpoint re-films them)"
            )
    if tiered and not scope.get("full"):
        extra, scenes_by, note = _floor_judges(
            floor, list(scope.get("rejudge") or []), all_scenes=[int(s) for s in current]
        )
        scope["judges"] = ["concept", *extra]
        scope["arc"] = "arc" in extra
        if "user" in scenes_by:
            scope["user_scenes"] = scenes_by["user"]
        scope["reason"] += (
            " — judge tiering: concept judge on changed scenes"
            + (f"; FLOOR-FIRST: {note}" if note else "")
            + ("" if extra else " (user + arc carried to the checkpoint)")
        )
    else:
        scope["judges"] = ["concept", "user", "arc"]
    scope.update(
        {
            "planned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ledger_iteration": (ledger or {}).get("iteration"),
            "fingerprints": current,
        }
    )
    scope["passes"] = _append_history(run, scope, iteration)
    (run / SCOPE_FILE).write_text(json.dumps(scope, indent=1) + "\n")
    return scope


def load_scope(run_dir: str | Path) -> dict | None:
    p = Path(run_dir) / SCOPE_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _archive_dir(run: Path, iteration: Any, kind: str) -> Path:
    d = run / f"iter{iteration if iteration is not None else 'prev'}-archive" / "passes" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _move(src: Path, dest_dir: Path) -> None:
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.name}.{datetime.now().strftime('%H%M%S%f')}"
    shutil.move(str(src), str(dest))


def carry(run_dir: str | Path) -> dict[str, Any]:
    """Prepare ``passes/`` for a scoped judge pass (see module docstring)."""
    run = Path(run_dir)
    scope = load_scope(run)
    if scope is None:
        raise ValueError(f"no {SCOPE_FILE} in {run} — run `judge_scope plan` first")
    rejudge = {int(s) for s in scope.get("rejudge") or []}
    reuse = {int(s) for s in scope.get("reuse") or []}
    prev = scope.get("ledger_iteration")
    concept = run / "passes" / "concept"
    concept.mkdir(parents=True, exist_ok=True)
    cache = run / CACHE_DIR

    archived: list[str] = []
    for f in sorted(concept.iterdir()):
        m = _SCENE_PASS_RE.match(f.name)
        if not m:
            continue
        scene = int(m.group(1))
        if scope.get("full") or scene in rejudge:
            _move(f, _archive_dir(run, prev, "concept"))
            archived.append(f.name)

    restored: list[str] = []
    for f in sorted((cache / "concept").glob("*")) if (cache / "concept").is_dir() else []:
        m = _SCENE_PASS_RE.match(f.name)
        if not m or int(m.group(1)) not in reuse:
            continue
        dest = concept / f.name
        if dest.exists() and dest.read_bytes() == f.read_bytes():
            continue
        if dest.exists():
            _move(dest, _archive_dir(run, prev, "concept"))
        shutil.copy2(f, dest)
        restored.append(f.name)
    missing = sorted(
        s for s in reuse if not (concept / f"scene_{s}.json").exists()
    )

    arc = run / "passes" / "arc"
    if scope.get("arc"):
        if arc.is_dir():
            for f in sorted(arc.iterdir()):
                _move(f, _archive_dir(run, prev, "arc"))
    else:
        # Arc is reused: make sure its verdict AND sealed pass are present.
        if (cache / "arc").is_dir():
            arc.mkdir(parents=True, exist_ok=True)
            for f in sorted((cache / "arc").iterdir()):
                if not (arc / f.name).exists():
                    shutil.copy2(f, arc / f.name)
        for name in _ARC_FILES:
            if not (run / name).exists() and (cache / name).exists():
                shutil.copy2(cache / name, run / name)

    # Judge tiering: the user-artifact judge does not run on this pass, so its
    # last verdict (findings included) is carried wholesale. On a floor-first
    # pass it runs on ``user_scenes`` only and ``merge-user`` lays those rows
    # over the carried verdict.
    user_carried = False
    if "user" not in (scope.get("judges") or ["user"]):
        prior_user = cache / "verdict-user.yaml"
        if prior_user.exists():
            shutil.copy2(prior_user, run / "verdict-user.yaml")
            user_carried = True

    return {
        "archived": archived,
        "restored": restored,
        "reuse_missing_cache": missing,
        "rejudge": sorted(rejudge),
        "reuse": sorted(reuse),
        "arc": bool(scope.get("arc")),
        "judges": scope.get("judges") or ["concept", "user", "arc"],
        "user_carried": user_carried,
        "user_scenes": user_scenes(scope),
        "user_reuse": user_reuse(scope),
        "expect_concept_passes": len(
            [f for f in concept.iterdir() if f.is_file() and not f.name.endswith(".seal.json")]
        ),
    }


def _all_scenes(scope: dict) -> list[int]:
    return sorted({int(s) for k in ("rejudge", "reuse", "held") for s in scope.get(k) or []})


def user_scenes(scope: dict) -> list[int]:
    """The scenes the user-artifact judge dispatches on this pass."""
    if scope.get("full"):
        return _all_scenes(scope)
    if "user" not in (scope.get("judges") or []):
        return []
    if scope.get("user_scenes") is not None:
        return sorted(int(s) for s in scope["user_scenes"])
    return sorted(int(s) for s in scope.get("rejudge") or [])


def user_reuse(scope: dict) -> list[int]:
    """The scenes whose user-artifact rows come from the ledger on this pass."""
    if scope.get("full"):
        return []
    judged = set(user_scenes(scope))
    return [s for s in _all_scenes(scope) if s not in judged]


def reused_user_rows(run_dir: str | Path) -> dict[str, Any]:
    """The ledger's user-artifact per-scene rows for the scenes being reused."""
    run = Path(run_dir)
    scope = load_scope(run) or {}
    reuse = {str(s) for s in user_reuse(scope)}
    prior = _load_yaml(run / CACHE_DIR / "verdict-user.yaml") or {}
    return {k: v for k, v in _user_rows(prior).items() if k in reuse}


def _user_rows(verdict: dict) -> dict[str, dict]:
    """``{scene: {dimension: score}}`` from either shape the user judge writes:
    a ``per_scene`` mapping, or a ``scenes`` list (:func:`scripts.ddd.floor.scene_scores`)."""
    per = verdict.get("per_scene")
    if isinstance(per, dict) and per:
        return {str(k): v for k, v in per.items()}
    from scripts.ddd.floor import scene_scores

    return {str(k): v for k, v in scene_scores(verdict).items()}


def _load_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def merge_user(prior: dict, partial: dict, reuse: list[int]) -> dict:
    """Merge a partial user-artifact verdict (re-judged scenes) over the prior one.

    ``per_scene`` = the partial's rows plus the prior's rows for REUSED scenes.
    Each dimension's score is the minimum over the merged rows (``overall_rule:
    lowest``); its justification / fix_recommendation / fix_kind come from
    whichever verdict owns the minimum — the prior's when the floor sits on a
    reused scene, the partial's otherwise. ``overall_score`` is the minimum
    dimension. A reused scene cannot be dropped: if the prior has no row for it,
    this raises rather than writing a verdict that silently covers fewer scenes.
    """
    prior_rows = _user_rows(prior)
    rows = _user_rows(partial)
    for s in reuse:
        key = str(s)
        if key in rows:
            continue
        if key not in prior_rows:
            raise ValueError(f"reused scene {key} has no per_scene row in the prior user verdict")
        rows[key] = prior_rows[key]
    merged = dict(partial)
    dims_prior = prior.get("dimensions") or {}
    dims_new = partial.get("dimensions") or {}
    merged_dims: dict[str, Any] = {}
    reuse_keys = {str(s) for s in reuse}
    for dim in sorted(set(dims_prior) | set(dims_new)):
        scores = [
            (float(r[dim]), k) for k, r in rows.items() if isinstance(r, dict) and dim in r
        ]
        base = dims_new.get(dim) or dims_prior.get(dim) or {}
        if not scores:
            merged_dims[dim] = base
            continue
        low, owner = min(scores)
        source = dims_prior if owner in reuse_keys and dim in dims_prior else dims_new
        entry = dict(source.get(dim) or base)
        entry["score"] = low
        merged_dims[dim] = entry
    merged["dimensions"] = merged_dims
    ordered = sorted(rows, key=lambda k: (0, int(k), "") if k.isdigit() else (1, 0, k))
    merged["per_scene"] = {(int(k) if k.isdigit() else k): rows[k] for k in ordered}
    if isinstance(prior.get("scenes"), list) or isinstance(partial.get("scenes"), list):
        # Keep the judge's own per-scene entries (notes, adversarial lists) too:
        # the partial's for re-judged scenes, the prior's for reused ones.
        from scripts.ddd.floor import _scene_key

        by_scene: dict[int, Any] = {}
        reuse_ints = {int(s) for s in reuse}
        for entry in prior.get("scenes") or []:
            k = _scene_key(entry.get("scene", entry.get("scene_index")) if isinstance(entry, dict) else None)
            if k is not None and k in reuse_ints:
                by_scene[k] = entry
        for entry in partial.get("scenes") or []:
            k = _scene_key(entry.get("scene", entry.get("scene_index")) if isinstance(entry, dict) else None)
            if k is not None:
                by_scene[k] = entry
        merged["scenes"] = [by_scene[k] for k in sorted(by_scene)]
    if merged_dims:
        overall = min(float(d.get("score")) for d in merged_dims.values() if d.get("score") is not None)
        merged["overall_score"] = overall
        merged["verdict"] = "pass" if overall >= 4 else ("warn" if overall >= 3 else "fail")
    # Findings: the partial's (re-judged scenes) plus the prior's for REUSED
    # scenes — assemble routes on them, so a reused scene's open defects must not
    # silently vanish from the backlog. A finding whose scene cannot be read is
    # kept only from the partial (it came from a fresh judgement).
    reused_findings = [
        f
        for f in prior.get("findings") or []
        if isinstance(f, dict) and _finding_scene(f) in reuse_keys
    ]
    if reused_findings or "findings" in partial or "findings" in prior:
        merged["findings"] = list(partial.get("findings") or []) + reused_findings
    merged["reused_scenes"] = sorted(int(s) for s in reuse)
    return merged


def _finding_scene(finding: dict) -> str | None:
    """A finding's scene as a key (``3``, ``"3"`` and ``"3: title"`` -> ``"3"``)."""
    m = re.match(r"\s*(\d+)", str(finding.get("scene", "")))
    return m.group(1) if m else None


def record(
    run_dir: str | Path,
    spec_path: str | Path,
    *,
    iteration: int,
    context: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Snapshot this judged iteration as the next iteration's ledger."""
    run = Path(run_dir)
    ctx = list(context or [])
    wb = run / "why_brief.yaml"
    if wb.exists() and str(wb) not in {str(c) for c in ctx}:
        ctx.append(wb)
    components, signatures = scene_inputs(run, spec_scenes(spec_path), salt=context_salt(ctx))
    fps = {sc: _combine(c) for sc, c in components.items()}
    scope = load_scope(run) or {}
    # A HELD or REUSED scene was not re-judged: its cells are the ledger's, so its
    # ledger entry stays the one those cells were judged on. For a held scene the
    # next pass that is not recipe-scoped re-judges it if its inputs still differ;
    # for a reused one this stops sub-tolerance drift from accumulating pass
    # over pass — every comparison is against the inputs the cell was judged on.
    prior = load_ledger(run) or {}
    prior_sigs = load_signatures(run)
    keep = set(str(x) for x in scope.get("held") or [])
    if not scope.get("full", True) and prior.get("fingerprint_version") == FINGERPRINT_VERSION:
        keep |= {str(x) for x in scope.get("reuse") or []}
    for sc in keep:
        if sc in (prior.get("fingerprints") or {}):
            fps[sc] = prior["fingerprints"][sc]
            if sc in (prior.get("components") or {}):
                components[sc] = prior["components"][sc]
            if sc in prior_sigs:
                signatures[sc] = prior_sigs[sc]
    cache = run / CACHE_DIR
    tmp = run / f"{CACHE_DIR}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "concept").mkdir(parents=True)
    (tmp / "arc").mkdir(parents=True)
    concept = run / "passes" / "concept"
    n_concept = 0
    if concept.is_dir():
        for f in concept.iterdir():
            if _SCENE_PASS_RE.match(f.name):
                shutil.copy2(f, tmp / "concept" / f.name)
                n_concept += 1
    arc = run / "passes" / "arc"
    if arc.is_dir():
        for f in arc.iterdir():
            if f.is_file():
                shutil.copy2(f, tmp / "arc" / f.name)
    for name in (*_ARC_FILES, "verdict-user.yaml", "verdict-concept.yaml"):
        if (run / name).exists():
            shutil.copy2(run / name, tmp / name)
    index = {
        "iteration": iteration,
        "full": bool(scope.get("full", True)),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fingerprint_version": FINGERPRINT_VERSION,
        "fingerprints": fps,
        "components": components,
    }
    (tmp / INDEX_FILE).write_text(json.dumps(index, indent=1) + "\n")
    (tmp / SIGNATURES_FILE).write_text(json.dumps(signatures) + "\n")
    if cache.exists():
        shutil.rmtree(cache)
    tmp.rename(cache)
    return {"iteration": iteration, "scenes": len(fps), "concept_pass_files": n_concept}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_state(run_dir: str | Path):
    """The run's RunState from ``<run_dir>/run_state.yaml``, or ``None``."""
    path = Path(run_dir) / "run_state.yaml"
    if not path.exists():
        return None
    from scripts.ddd.schemas.models import RunState

    return RunState.model_validate(yaml.safe_load(path.read_text()) or {})


def _batch_scenes(state: Any) -> list[int] | str | None:
    """The scenes the batch applied before THIS pass edited (``state.batch_plan``);
    ``"all"`` when one of its findings named no scene."""
    bp = getattr(state, "batch_plan", None)
    if not isinstance(bp, dict) or bp.get("for_iteration") != getattr(state, "iteration", None):
        return None
    if bp.get("unscoped"):
        return "all"
    return [int(s) for s in bp.get("scenes") or []]


def resolve_plan_args(
    run_dir: str | Path,
    *,
    full: bool = False,
    tiered: bool = False,
    reason: str | None = None,
    state: Any = None,
    cfg: Any = None,
) -> dict[str, Any]:
    """``plan`` kwargs for this pass, derived from run_state.

    The scope is the LOOP's decision (``compute_auto_iterate`` ->
    ``state.next_judge_full`` -> :func:`scripts.ddd.target.expected_scope`), not
    the caller's. On ``supply-sophie-rutf-2026-09-26-001`` the orchestrator
    re-used a command line with a literal ``--full`` for three iterations after
    the loop had switched to incremental, so every pass judged all 7 scenes with
    all three judges and the record said "full pass requested". Now:

    * no flags -> the state's scope (full or incremental, tiered, recipe scenes);
    * ``--full`` the state agrees with -> full, with the state's reason;
    * ``--full`` the state does NOT ask for -> refused, unless ``reason`` is
      given; then it is recorded in ``judge-scope.json`` as ``override`` and
      ``assemble`` prints it.

    Also refuses when the sealed decision was overruled without a logged
    reason (:mod:`scripts.ddd.decision`). No run_state (a bare run dir, unit
    use) -> the flags as given.
    """
    if state is None:
        state = _run_state(run_dir)
    if state is None:
        return {"force_full": full, "tiered": tiered}
    from scripts.ddd import decision, loop_config, target

    decision.require(state, where="judge_scope plan")
    cfg = cfg or loop_config.load()
    exp = target.expected_scope(state, cfg)
    kw: dict[str, Any] = {
        "force_full": exp["full"],
        "tiered": tiered or exp["tiered"],
        "full_reason": exp["why"],
        "judge_scenes": exp["judge_scenes"],
        # Floor-first (canopy#780): the cell the last assemble found holding the
        # gating score down; plan adds its judge to an incremental pass.
        "floor": getattr(state, "gating_floor", None) if cfg.loop.floor_first else None,
        "iteration": getattr(state, "iteration", None),
        "edited_scenes": _batch_scenes(state),
    }
    if full and not exp["full"]:
        if not (reason or "").strip():
            raise ValueError(
                "--full contradicts run_state: the loop asked for an INCREMENTAL pass "
                f"({exp['why']}). Drop --full (plan derives the scope from run_state), "
                "or pass --reason \"<why this pass must be full>\" to override it on the record."
            )
        kw.update(
            {
                "force_full": True,
                "judge_scenes": None,
                "full_reason": f"full pass OVERRIDE: {reason.strip()} (the loop asked for: {exp['why']})",
                "override": {
                    "requested": "full",
                    "expected": "incremental",
                    "reason": reason.strip(),
                    "iteration": getattr(state, "iteration", None),
                },
            }
        )
    return kw


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.judge_scope")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("run_dir")
    p.add_argument("spec")
    p.add_argument(
        "--full",
        action="store_true",
        help="force a full judge pass. Derived from run_state when omitted (the normal "
        "case); a --full that contradicts run_state is refused unless --reason is given",
    )
    p.add_argument("--reason", default=None, help="why a --full overrides run_state (recorded)")
    p.add_argument("--context", action="append", default=[])
    p.add_argument("--tiered", action="store_true", help="judge tiering: incremental passes run the concept judge only")
    c = sub.add_parser("carry")
    c.add_argument("run_dir")
    u = sub.add_parser("reused-user")
    u.add_argument("run_dir")
    m = sub.add_parser("merge-user")
    m.add_argument("run_dir")
    m.add_argument("partial")
    r = sub.add_parser("record")
    r.add_argument("run_dir")
    r.add_argument("spec")
    r.add_argument("--iteration", type=int, required=True)
    r.add_argument("--context", action="append", default=[])
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            kw = resolve_plan_args(args.run_dir, full=args.full, tiered=args.tiered, reason=args.reason)
            out = plan(args.run_dir, args.spec, context=args.context, **kw)
            out = {k: v for k, v in out.items() if k not in ("fingerprints", "passes")}
        elif args.cmd == "carry":
            out = carry(args.run_dir)
        elif args.cmd == "reused-user":
            out = reused_user_rows(args.run_dir)
        elif args.cmd == "merge-user":
            run = Path(args.run_dir)
            scope = load_scope(run) or {}
            prior = _load_yaml(run / CACHE_DIR / "verdict-user.yaml") or {}
            partial = _load_yaml(Path(args.partial))
            if partial is None:
                raise ValueError(f"{args.partial}: not a YAML mapping")
            merged = merge_user(prior, partial, user_reuse(scope))
            (run / "verdict-user.yaml").write_text(yaml.safe_dump(merged, sort_keys=False))
            out = {
                "wrote": str(run / "verdict-user.yaml"),
                "overall_score": merged.get("overall_score"),
                "reused_scenes": merged.get("reused_scenes"),
            }
        else:
            out = record(args.run_dir, args.spec, iteration=args.iteration, context=args.context)
    except (ValueError, OSError) as exc:
        print(f"judge_scope {args.cmd}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
