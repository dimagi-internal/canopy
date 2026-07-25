"""Emit first-class *narrative snippets* from a converged DDD run.

A DDD narrative already carries, per scene/beat, the two things a video snippet
fundamentally needs:

  * **exact in/out timestamps** — from the recorder's per-scene timing
    (``run-report-iter<N>.json`` → ``scenes[].start_seconds`` / ``duration_seconds``);
  * **one clean sentence** — the scene's ``concept_claim`` (a single falsifiable
    beat), which doubles as the caption / lower-third text AND the voiceover script.

So a snippet here is a *logical* range into the run's master walkthrough clip plus
its sentence — NOT a physically re-cut file. The same manifest drives two
downstream consumers in ACE: (a) the first-class snippet *library* (each snippet
stored with its in/out + narration, far richer than a whole-clip + slug), and
(b) the semi-gloss *explainer* render (sequence the ranges, each with per-beat
ElevenLabs VO + a lower-third from the sentence).

This is the canopy/source half of the planned canopy↔ACE narrative substrate,
scoped to the snippet/explainer use case: canopy owns the narrative + timing + the
master clip; ACE owns the library, voice synthesis, and render.

CLI::

    # from the canopy repo, with DDD_DIR pointing at the target repo's .canopy/ddd
    DDD_DIR=/path/to/repo/.canopy/ddd \
      uv run python -m scripts.ddd.snippets emit <run_id> [--iteration N] [--out PATH]

Writes ``<run_dir>/snippet_manifest.json`` and prints it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from scripts.ddd.runstate import _resolve_ddd_dir, load

SCHEMA_VERSION = 1


def _run_dir(run_id: str) -> Path:
    return _resolve_ddd_dir() / "runs" / run_id


def _find_report(run_dir: Path, iteration: int) -> Path:
    """The recorder's run report for *iteration* (per-scene timing lives here)."""
    candidates = [
        run_dir / f"run-report-iter{iteration}.json",
        run_dir / "run-report.json",  # un-suffixed (iteration 0 default)
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"no run report found for {run_id_hint(run_dir)} iter {iteration}: "
        f"looked for {[str(c) for c in candidates]}"
    )


def run_id_hint(run_dir: Path) -> str:
    return run_dir.name


def _find_spec(run_dir: Path) -> Path:
    """The unified spec staged into the run dir (concept_claim per scene)."""
    spec = run_dir / "unified_spec.yaml"
    if not spec.exists():
        raise FileNotFoundError(
            f"no unified_spec.yaml in {run_dir} — run /canopy:ddd-upload (which stages it) "
            f"or copy docs/walkthroughs/<slug>.yaml there first."
        )
    return spec


def _slugify(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in (text or ""))
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:60] or "beat"


def _scene_change_times(clip_path: str, start: float, dur: float, threshold: float) -> list[float] | None:
    """On-screen-motion timestamps (seconds, relative to the [start, start+dur]
    window) via ffmpeg ``select=gt(scene,threshold)``. Returns None on failure."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}",
             "-t", f"{dur:.3f}", "-i", clip_path,
             "-vf", f"select='gt(scene,{threshold})',metadata=print",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:  # noqa: BLE001 — detection is best-effort
        return None
    # pts_time is relative to the window (=-ss before -i resets timestamps).
    return sorted(float(m) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr or ""))


def dedwell_segments(
    clip_path: str,
    start: float,
    dur: float,
    *,
    scene_threshold: float = 0.03,
    gap_max: float = 6.0,
    dwell: float = 0.8,
    tail: float = 1.4,
    floor: float = 2.0,
    keep_dwell: bool = False,
) -> list[tuple[float, float]]:
    """De-dwell a scene's master range into motion sub-ranges (absolute
    ``in_seconds``, ``duration``), collapsing dead-air gaps wherever they sit.

    The recorder's per-scene clip length is wall-clock — it bakes in slow-load
    ``wait_for`` time, the end-of-scene hold, and the snapshot dwell. Those show
    as static dead air, and they can sit anywhere: trailing (page loaded then
    sat), mid-clip (a slow async load between two actions), or leading.

    We find on-screen motion (ffmpeg scene-change) across the whole range and
    keep the moving spans, collapsing any gap with no motion for longer than
    ``gap_max`` down to ``dwell`` seconds (a brief beat of the settled state),
    and trimming the trailing static to ``last_motion + tail``. The kept spans
    are returned as ordered sub-ranges; the renderer plays them back-to-back, so
    each collapsed gap becomes a clean jump-cut. Generalizes a trailing-only
    trim to leading / mid / trailing dead air.

    **Pace contract:** ``keep_dwell=True`` (set by the caller for ``pace: teach``
    scenes) returns the scene's FULL range untouched — teach scenes are NOT
    de-dwelled, because their holds are deliberate: a teach beat sits on a
    highlighted table/column for 8-15s while the voiceover explains it, and that
    held footage carries the narration. Collapsing it to ``dwell`` would leave
    the VO running over a frozen frame. ``flow`` and default/None scenes pass
    ``keep_dwell=False`` (the default) and de-dwell as before.

    Best-effort: returns a single full range on any ffmpeg/parse failure. Floors
    the total kept duration at ``floor``; a range with no detected motion keeps
    a short ``floor`` of its opening frame.
    """
    full = [(round(start, 3), round(dur, 3))]
    # teach scenes keep their holds — the held footage carries the narration.
    if keep_dwell or not clip_path or dur <= floor:
        return full
    times = _scene_change_times(clip_path, start, dur, scene_threshold)
    if times is None:
        return full
    if not times:
        return [(round(start, 3), round(min(floor, dur), 3))]

    spans: list[tuple[float, float]] = []  # (rel_start, rel_end) within the window
    seg_start = 0.0
    prev = 0.0
    for m in times:
        if m - prev > gap_max:  # dead span (prev, m) — collapse to `dwell`
            seg_end = min(prev + dwell, dur)
            if seg_end > seg_start + 0.05:
                spans.append((seg_start, seg_end))
            seg_start = m
        prev = m
    seg_end = min(prev + tail, dur)
    if seg_end > seg_start + 0.05:
        spans.append((seg_start, seg_end))
    if not spans:
        spans = [(0.0, min(floor, dur))]

    total = sum(e - s for s, e in spans)
    if total < floor:  # extend the last span toward the floor (within the window)
        s0, e0 = spans[-1]
        spans[-1] = (s0, min(dur, e0 + (floor - total)))
    return [(round(start + s, 3), round(e - s, 3)) for s, e in spans]


# --- Ground-truth loading-wait excision -----------------------------------
# A `wait_for` action blocks until its target appears; the recorder records that
# span (RunReport.load_waits, recording-timeline seconds). Excising the long ones
# here — keep a brief lead-in, then jump-cut to the result — collapses a mid-scene
# "Generating…"/"Reviewing…" spinner precisely, even in a teach scene and even
# when the spinner ANIMATES (which the freeze-based render cap can't see, since
# animation reads as motion). This is the durable mechanism; the render-time
# freeze cap (#231/#239) is the fallback for waits with no recorded action.

LOAD_WAIT_LEAD_IN_SECONDS = 1.2
LOAD_WAIT_MIN_SECONDS = 3.0


def _excise_span_from_segs(
    segs: list[tuple[float, float]], span_start: float, span_end: float
) -> list[tuple[float, float]]:
    """Remove the absolute master-clip range ``[span_start, span_end]`` from segs.

    Each seg is ``(start_seconds, duration_seconds)`` in absolute master time. A
    seg overlapping the span is split into its kept left/right parts and the
    middle dropped (a clean jump-cut when the renderer plays the segments)."""
    out: list[tuple[float, float]] = []
    for s, d in segs:
        e = s + d
        if span_end <= s or span_start >= e:  # no overlap — keep whole seg
            out.append((s, d))
            continue
        if span_start > s:  # left part survives
            out.append((round(s, 3), round(span_start - s, 3)))
        if span_end < e:  # right part survives
            out.append((round(span_end, 3), round(e - span_end, 3)))
    return out


def excise_load_waits(
    segs: list[tuple[float, float]],
    load_waits: list[dict[str, Any]],
    *,
    lead_in: float = LOAD_WAIT_LEAD_IN_SECONDS,
    threshold: float = LOAD_WAIT_MIN_SECONDS,
) -> list[tuple[float, float]]:
    """Excise long ground-truth loading-wait spans from de-dwelled segments.

    Each load_wait is ``{"start_seconds", "duration_seconds", ...}`` in absolute
    master seconds. A wait longer than ``threshold`` keeps ``lead_in`` of the
    spinner (so the cut reads as deliberate) then drops the rest up to the
    result. Short settles (≤ ``threshold``) are left untouched. PURE."""
    out = list(segs)
    for lw in load_waits:
        ws = float(lw.get("start_seconds") or 0.0)
        we = ws + float(lw.get("duration_seconds") or 0.0)
        if we - ws <= threshold:  # a short settle, not a load worth collapsing
            continue
        cut_start = ws + lead_in
        if we - cut_start > 1e-6:
            out = _excise_span_from_segs(out, cut_start, we)
    return out


# --- Action↔word marks ----------------------------------------------------
# Each action's raw master-clip timestamp (ActionResult.start_seconds) is mapped
# through the SAME de-dwell + load-wait excision the footage gets, into ON-SCREEN
# seconds, then tagged with candidate narration words. The render side resolves
# each word against the beat's ElevenLabs VO timings and time-warps the footage so
# the named field lands on its word. See video-engine/docs/action-word-sync.md.

# Kinds that put a FIELD on camera (a moment the narration can name). hover is
# included (a glide onto a CTA). wait_for/hold/goto/capture move no field into
# view as their own act, so they never anchor a word.
_MARK_KINDS: frozenset[str] = frozenset(
    {"scroll_to", "scroll", "fill", "select", "type", "click", "hover", "press"}
)

# REVEAL kinds bring a NEW thing on camera (scroll it into view, open a dropdown,
# click to a result), so the named element renders at the action's END. We bias
# their mark by the action's measured duration. fill/hover/type act on an
# already-visible field and keep their start.
_REVEAL_KINDS: frozenset[str] = frozenset({"scroll_to", "scroll", "select", "click", "press"})


def onscreen_for_abs(segs: list[tuple[float, float]], abs_t: float) -> float:
    """Map an ABSOLUTE master-clip time to ON-SCREEN time across ``segs``.

    ``segs`` are the kept ``(start_seconds, duration_seconds)`` sub-ranges (after
    de-dwell + load-wait excision), played back-to-back, so on-screen time is the
    running sum of segment durations. A time that falls in an excised gap maps to
    that gap's jump-cut point (the boundary between the two kept segments); a time
    past the end clamps to the total. PURE."""
    onscreen = 0.0
    for s, d in segs:
        if abs_t < s:  # inside an excised/collapsed gap — the jump-cut point.
            return round(onscreen, 3)
        if abs_t <= s + d:
            return round(onscreen + (abs_t - s), 3)
        onscreen += d
    return round(onscreen, 3)  # past the last kept frame — clamp to total.


def _mark_words(action: dict[str, Any]) -> list[str]:
    """Ordered, deduped candidate narration words for an action (most specific
    first). Explicit ``word``/``say`` override → field-id tokens → note tokens.
    The render side keeps the FIRST candidate that resolves against the VO, so
    unresolved noise words (a note's filler) are harmless — they just don't bind.
    PURE."""
    words: list[str] = []
    for k in ("word", "say"):
        v = action.get(k)
        if v:
            words.append(str(v).strip().lower())
    target = str(action.get("target") or "")
    m = re.search(r"id_([a-z0-9_]+)", target)
    if m:
        words += [t for t in m.group(1).split("_") if len(t) > 2]
    note = str(action.get("note") or "")
    words += re.findall(r"[a-z]{4,}", note.lower())
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def build_action_marks(
    scene_actions: list[dict[str, Any]], segs: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """Build ``action_marks`` for one scene from its action trace + kept segments.

    ``scene_actions`` are the run-report ``actions`` entries for this scene (each
    with ``start_seconds`` in absolute master time, ``kind``, ``target``,
    ``note``). Returns ``[{on_seconds, words, target, kind}, ...]`` in on-screen
    order — the field anchors the renderer warps the footage onto. Actions with no
    ``start_seconds`` (old reports), non-field kinds, or no word candidates are
    skipped. PURE."""
    marks: list[dict[str, Any]] = []
    for a in scene_actions:
        ts = a.get("start_seconds")
        if ts is None or a.get("kind") not in _MARK_KINDS:
            continue
        words = _mark_words(a)
        if not words:
            continue
        # Anchor on the action's EFFECT, not its start. For REVEAL kinds (scroll
        # the field into view, open a dropdown, click) the thing the narration
        # names appears at the action's END, not when the cursor starts moving —
        # the video judge flagged scroll/reveal marks landing ~1s before the field
        # actually rendered. Bias those marks by the action's measured duration so
        # the RENDERED result lands on the spoken word. fill/hover act on an
        # already-visible field, so they keep their start. (elapsed_ms missing ⇒
        # +0, so old reports are unchanged.)
        eff = float(ts)
        if a.get("kind") in _REVEAL_KINDS:
            eff += (a.get("elapsed_ms") or 0) / 1000.0
        marks.append(
            {
                "on_seconds": onscreen_for_abs(segs, eff),
                "words": words,
                "target": a.get("target"),
                "kind": a.get("kind"),
            }
        )
    marks.sort(key=lambda m: m["on_seconds"])
    return marks


def build_snippets(
    *,
    narrative_slug: str,
    spec: dict[str, Any],
    report: dict[str, Any],
    source_clip_local: str | None,
    source_clip_hosted: str | None,
) -> list[dict[str, Any]]:
    """Pair each rendered scene (timing) with its spec scene (sentence).

    When the master clip is available locally, each scene's clip is de-dwelled
    (slow-load / hold / snapshot dead air removed — leading, mid, or trailing)
    into one or more motion sub-ranges (``segments``), which the renderer plays
    back-to-back. See :func:`dedwell_segments`.

    Exception — ``pace: teach`` scenes are NOT de-dwelled: their holds are
    choreographed (a beat that sits on a highlighted column for ~8-15s while the
    voiceover explains it), so collapsing them would run the narration over a
    frozen frame. Only ``pace: flow`` scenes are de-dwelled. Per the ``Scene``
    model, ``pace`` defaults to ``teach`` when unset (``None``), so the dead-air
    trim is opt-IN via ``flow`` — matching the recorder's own pace semantics
    (``apply_scene_pace`` compresses only ``flow``).
    """
    clip_for_trim = source_clip_local if (source_clip_local and Path(source_clip_local).exists()) else None
    spec_scenes = spec.get("scenes") or []
    report_scenes = report.get("scenes") or []
    snippets: list[dict[str, Any]] = []

    for rs in report_scenes:
        # run-report scene_index is 1-based; spec scenes are 0-based.
        idx = rs.get("scene_index")
        if idx is None:
            continue
        spec_scene = spec_scenes[idx - 1] if 0 < idx <= len(spec_scenes) else {}
        start = float(rs.get("start_seconds") or 0.0)
        dur = float(rs.get("duration_seconds") or 0.0)
        # De-dwell into motion sub-ranges. Without a local clip, keep one range.
        # Only an explicit `pace: teach` scene keeps its holds intact (they carry
        # the narration — e.g. a choreographed table explanation). `flow` AND
        # untagged/default scenes are de-dwelled so dead wait/hold time (a long
        # "generating…" wait, an end-of-scene hold) is trimmed, not shown in full.
        pace = spec_scene.get("pace")
        keep_dwell = pace == "teach"
        if clip_for_trim and dur > 0:
            segs = dedwell_segments(clip_for_trim, start, dur, keep_dwell=keep_dwell)
        else:
            segs = [(round(start, 3), round(dur, 3))]
        # Excise ground-truth loading waits (the recorder's `wait_for` spans) in
        # this scene — collapses a mid-scene spinner to a lead-in + jump-cut to
        # the result, even in a teach scene and even if the spinner animates (the
        # freeze-based render cap can't catch that). Deterministic, no pixels.
        scene_waits = [lw for lw in (report.get("load_waits") or []) if lw.get("scene_index") == idx]
        if scene_waits:
            _before = round(sum(d for _, d in segs), 3)
            segs = excise_load_waits(segs, scene_waits)
            _after = round(sum(d for _, d in segs), 3)
            if _after < _before - 0.05:
                print(f"  · scene {idx}: excised {round(_before - _after, 2)}s loading-wait(s) (ground-truth)")
        segments = [{"start_seconds": s, "duration_seconds": d} for s, d in segs]
        # Action↔word marks: each field action's raw timestamp mapped through the
        # SAME kept segments into on-screen time + tagged with narration words, so
        # the renderer can warp the footage to land each field on its spoken word.
        scene_actions = [a for a in (report.get("actions") or []) if a.get("scene_index") == idx]
        action_marks = build_action_marks(scene_actions, segs)
        kept_dur = round(sum(d for _, d in segs), 3)  # summed on-screen length
        in_seconds = segs[0][0]
        out_seconds = round(segs[-1][0] + segs[-1][1], 3)
        title = spec_scene.get("title") or rs.get("title") or f"Scene {idx}"
        sentence = (spec_scene.get("concept_claim") or "").strip()
        # The per-scene narrative is the spoken line — it's what the author edits
        # in the narrative review (canopy-web round-trips edits into scene.narrative).
        # concept_claim is the falsifiable design claim, used only as a fallback.
        narration = (spec_scene.get("narrative") or sentence).strip()
        # Pacing lint: if a field-heavy scene's narration is far denser than its
        # footage, even the action↔word warp's rate cap (RATE_MAX≈2.5× in
        # actionsync.ts) can't compress the footage enough to land each field on
        # its word — the fix is to split the scene or pace the narration, not warp.
        # ~2.6 words/sec matches the ElevenLabs voice; mirror actionsync's clamp.
        words = len(re.findall(r"[A-Za-z0-9']+", narration))
        vo_est = words / 2.6
        if len(action_marks) >= 4 and vo_est > 0.1 and kept_dur / vo_est > 2.5:
            print(
                f"  ⚠ scene {idx}: narration ~{vo_est:.0f}s but footage demos "
                f"{len(action_marks)} fields over {kept_dur:.0f}s — warp will hit its "
                f"{kept_dur / vo_est:.1f}× cap; split the scene or pace the narration."
            )
        features = spec_scene.get("features") or []
        tags = [narrative_slug] + [
            f.get("id") for f in features if isinstance(f, dict) and f.get("id")
        ]

        snippets.append(
            {
                "id": f"{narrative_slug}-scene-{idx}",
                "scene_index": idx,
                "title": title,
                # Logical ranges into the master clip — NOT re-cut files.
                # `segments` are the de-dwelled motion sub-ranges (played
                # back-to-back); in/out/duration bound them (in=first start,
                # out=last end, duration=summed on-screen length).
                "segments": segments,
                "action_marks": action_marks,
                "in_seconds": in_seconds,
                "out_seconds": out_seconds,
                "duration_seconds": kept_dur,
                # `narration` (scene.narrative) IS the spoken line — the narrative
                # the author writes/edits while picturing the demo. `sentence`
                # (concept_claim) is kept as the design claim / caption fallback.
                "narration": narration,
                "sentence": sentence,
                # `role: overview` marks the goal-setting opener — the explainer
                # builder absorbs it into the narrated intro title card rather
                # than replaying it as a body scene.
                "role": (spec_scene.get("role") or "").strip() or None,
                "tags": tags,
                "provenance": spec_scene.get("provenance"),
                "source_clip": source_clip_local,
                "source_clip_url": source_clip_hosted,
            }
        )
    return snippets


def emit_snippet_manifest(run_id: str, iteration: int | None = None) -> dict[str, Any]:
    """Build the snippet manifest for *run_id* and return it (also written to disk)."""
    state = load(run_id)
    it = iteration if iteration is not None else int(state.iteration)
    run_dir = _run_dir(run_id)

    spec = yaml.safe_load(_find_spec(run_dir).read_text()) or {}
    report = json.loads(_find_report(run_dir, it).read_text())

    clip_local = run_dir / f"iter{it}_clip.mp4"
    hosted = (state.iteration_clips or {}).get(it) or (state.iteration_clips or {}).get(
        str(it)
    )

    snippets = build_snippets(
        narrative_slug=state.narrative_slug,
        spec=spec,
        report=report,
        source_clip_local=str(clip_local) if clip_local.exists() else None,
        source_clip_hosted=hosted,
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "narrative_slug": state.narrative_slug,
        "run_id": run_id,
        "iteration": it,
        "name": spec.get("name") or state.narrative_slug,
        "source_clip": str(clip_local) if clip_local.exists() else None,
        "source_clip_url": hosted,
        "snippet_count": len(snippets),
        "snippets": snippets,
    }
    out = run_dir / "snippet_manifest.json"
    out.write_text(json.dumps(manifest, indent=2))
    manifest["_written_to"] = str(out)
    return manifest


# --------------------------------------------------------------------------
# canopy → ACE bridge: snippet manifest → ace-web "connect-ddd-walkthrough" spec.yaml
# --------------------------------------------------------------------------
# The ACE walkthrough-explainer template (templates/connect-ddd-walkthrough) renders
# ONE master clip narrated section-by-section: a `beats:` list (intro_title →
# body_walkthrough×N → outro_card) drives the arc, each body_walkthrough beat
# plays a RANGE of the master clip (walkthrough.<id>.{start_seconds,
# duration_seconds, lower_third}) with per-beat ElevenLabs VO
# (narration.by_beat.<id>). Our snippets map onto it 1:1 — in/out → the clip
# range, title → lower_third, sentence → VO. See the ace-web template's
# example.spec.yaml for the canonical shape this mirrors.

# ElevenLabs defaults match ace-web's connect-ddd-walkthrough example.spec.yaml.
DEFAULT_VOICE_ID = "XB0fDUnXU5powFXDhCwa"
DEFAULT_VOICE_MODEL = "eleven_turbo_v2"


def build_explainer_spec(
    manifest: dict[str, Any],
    *,
    workspace: str,
    master_ref: str,
    base_url: str,
    tagline: str,
    country_focus: str,
    voice_id: str = DEFAULT_VOICE_ID,
    voice_model: str = DEFAULT_VOICE_MODEL,
    generated_at: str = "1970-01-01T00:00:00Z",
    lower_thirds: bool = False,
) -> dict[str, Any]:
    """Map a snippet manifest onto an ace-web connect-ddd-walkthrough spec dict."""
    slug = manifest["narrative_slug"]
    name = manifest.get("name") or slug
    snippets = manifest.get("snippets") or []

    beats: list[dict[str, Any]] = [{"id": "title", "kind": "intro_title", "seconds": 4}]
    walkthrough: dict[str, Any] = {}
    # The opening title card's VOICEOVER. When the narrative declares a
    # `role: overview` scene (the goal-setting opener), THAT scene's narrative is
    # the intro voiceover — spoken over the held title card — and the scene is
    # absorbed into the title beat rather than replayed as a body scene (its
    # visual is the same product screen the first demo scene already shows). The
    # title CARD text stays the `tagline` (a tight headline; see TitleCard).
    # Without an overview scene, the tagline itself is spoken (a real headline —
    # NOT "<slug>: …", which read the raw narrative slug aloud).
    #
    # This is the fix for the opening-slide mismatch: the card used to narrate
    # the raw `tagline` while the overview replayed as a separate body scene, so
    # the video opened on a stale headline instead of the narrated overview.
    overview_id: str | None = None
    if any((s.get("role") or "") != "overview" for s in snippets):
        # never absorb the ONLY scene — that would leave a body-less title card
        ov = next((s for s in snippets if (s.get("role") or "") == "overview"), None)
        if ov and (ov.get("narration") or "").strip():
            overview_id = ov.get("id")
    intro_vo = ""
    if overview_id is not None:
        intro_vo = (
            next(s for s in snippets if s.get("id") == overview_id).get("narration") or ""
        ).strip()
    by_beat: dict[str, str] = {"title": intro_vo or tagline or _humanize_slug(name)}

    for sn in snippets:
        if sn.get("id") == overview_id:
            continue  # absorbed into the narrated intro title beat above
        bid = f"s{sn['scene_index']}"
        beats.append(
            {"id": bid, "kind": "body_walkthrough", "seconds": round(sn["duration_seconds"], 1)}
        )
        walkthrough[bid] = {
            "asset": "@master",
            # De-dwelled motion sub-ranges, played back-to-back (dead-air gaps
            # collapsed → jump-cuts). The renderer prefers `segments`; the
            # bounding start/duration stay for older specs / non-de-dwelled emits.
            "segments": sn.get("segments")
            or [{"start_seconds": sn["in_seconds"], "duration_seconds": sn["duration_seconds"]}],
            "start_seconds": sn["in_seconds"],
            "duration_seconds": sn["duration_seconds"],
            # Off by default — the recorded dashboard self-labels and the VO
            # narrates, so a lower-third pill just covers the content. Opt in
            # with --lower-thirds.
            "lower_third": sn["title"] if lower_thirds else "",
        }
        # Action↔word marks (only when the recording produced them), so beats
        # without per-action timestamps emit an identical walkthrough block.
        if sn.get("action_marks"):
            walkthrough[bid]["action_marks"] = sn["action_marks"]
        # Spoken line = the scene's narration (what the author edits in review).
        # The renderer holds the section's last frame if narration runs longer
        # than the clip range, so the narrative can be any length without drift.
        by_beat[bid] = sn.get("narration") or sn.get("sentence") or ""
    beats.append({"id": "outro", "kind": "outro_card", "seconds": 5})
    by_beat["outro"] = ""

    return {
        "provenance": {
            "generator": "video-from-walkthrough",
            "template": "connect-ddd-walkthrough",
            "generated_from": f"{slug} DDD run {manifest.get('run_id')}",
            "generated_at": generated_at,
        },
        "slug": f"{slug}-explainer",
        "workspace": workspace,
        "name": name,
        "country_focus": country_focus,
        "status": "DDD walkthrough",
        "tagline": tagline,
        "program_url": base_url,
        "manifest": {"master": master_ref},
        "beats": beats,
        "walkthrough": walkthrough,
        "narration": {
            "generator": "manual",
            "prompt_version": "v1",
            "start_seconds": 0,
            "by_beat": by_beat,
            # Full VO blob (required by ProgramSpecSchema) — the per-beat
            # sentences in timeline order, so script and by_beat never drift.
            "script": "\n".join(
                by_beat[b["id"]] for b in beats if by_beat.get(b["id"])
            ),
        },
        "voice": {"provider": "elevenlabs", "voice_id": voice_id, "model": voice_model},
    }


def emit_explainer_spec(
    run_id: str,
    *,
    iteration: int | None = None,
    workspace: str = "dimagi-team",
    master_ref: str | None = None,
    tagline: str = "",
    country_focus: str = "",
    lower_thirds: bool = False,
) -> dict[str, Any]:
    """Build the explainer spec for *run_id* and write explainer_spec.yaml."""
    manifest = emit_snippet_manifest(run_id, iteration=iteration)
    run_dir = _run_dir(run_id)

    # base_url + a tagline default come from the unified spec when present.
    spec = yaml.safe_load(_find_spec(run_dir).read_text()) or {}
    base_url = spec.get("base_url") or "https://labs.connect.dimagi.com/"
    if not tagline:
        tagline = spec.get("tagline") or ""

    # Default the master ref to a library: path (operator uploads the master
    # clip + runs videos_ingest_snippets which links it). Fall back to the
    # hosted clip URL if present, else a file: basename.
    if not master_ref:
        if manifest.get("source_clip_url"):
            master_ref = manifest["source_clip_url"]
        elif manifest.get("source_clip"):
            master_ref = f"library:video/ddd/{Path(manifest['source_clip']).name}"
        else:
            master_ref = f"library:video/ddd/{manifest['narrative_slug']}.mp4"

    explainer = build_explainer_spec(
        manifest,
        workspace=workspace,
        master_ref=master_ref,
        base_url=base_url,
        tagline=tagline,
        country_focus=country_focus,
        lower_thirds=lower_thirds,
    )
    out = run_dir / "explainer_spec.yaml"
    out.write_text(yaml.safe_dump(explainer, sort_keys=False, allow_unicode=True))
    explainer["_written_to"] = str(out)
    return explainer


def _humanize_slug(slug: str | None) -> str:
    """A safe tagline fallback: the narrative slug as Title Case words
    (``microplans-study-groups`` → ``Microplans Study Groups``).

    Deliberately NOT derived from the narrative text. The old fallback took the
    narrative's first clause, which is scene 1's opening line — so the intro
    title card just repeated the first thing the narration says. Authors should
    set an explicit ``tagline:`` on the spec (or pass --tagline) for a real
    headline; this humanized-name default is only there so the (schema-required,
    non-empty) tagline never silently becomes a duplicate of the narration."""
    words = re.split(r"[-_\s]+", (slug or "").strip())
    return " ".join(w[:1].upper() + w[1:] for w in words if w)


def emit_explainer_from_capture(
    spec_path: str,
    report_path: str,
    *,
    clip_path: str | None = None,
    master_ref: str | None = None,
    workspace: str = "dimagi-team",
    tagline: str = "",
    country_focus: str = "",
    lower_thirds: bool = False,
    out_path: str | None = None,
) -> dict[str, Any]:
    """Build a connect-ddd-walkthrough spec from a fresh capture — no run-state.

    This is the run-state-free sibling of :func:`emit_explainer_spec`: it takes
    a unified spec (``scene.narrative`` = the spoken line) plus the recorder's
    run report (``record_video.py --report`` — ``scenes[].{scene_index,
    start_seconds, duration_seconds}``) and the master clip, and writes the
    explainer ``spec.yaml`` next to the spec (or ``out_path``). The narrative
    slug is the spec's ``name``. The default master ref is a ``file:`` path so
    the local renderer (``render_locally.py --local-spec``) copies the clip in.

    Used by ``/canopy:ddd-ace-render``, which records a fresh master clip and
    hands the emitted spec + clip to ``/ace:video-render-local``.
    """
    spec = yaml.safe_load(Path(spec_path).read_text()) or {}
    report = json.loads(Path(report_path).read_text())
    slug = spec.get("name") or Path(spec_path).stem

    snippets = build_snippets(
        narrative_slug=slug,
        spec=spec,
        report=report,
        source_clip_local=str(clip_path) if clip_path else None,
        source_clip_hosted=None,
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "narrative_slug": slug,
        "run_id": None,
        "iteration": None,
        "name": spec.get("name") or slug,
        "source_clip": str(clip_path) if clip_path else None,
        "source_clip_url": None,
        "snippet_count": len(snippets),
        "snippets": snippets,
    }

    base_url = spec.get("base_url") or "https://labs.connect.dimagi.com/"
    # The connect-ddd-walkthrough schema requires tagline + country_focus to be
    # non-empty. Prefer explicit values (CLI / spec); otherwise derive a sane
    # default so any narrative renders without hand-editing (authors can set
    # `tagline:` / `country_focus:` on the spec, or pass --tagline / --country,
    # for a sharper line).
    if not tagline:
        tagline = spec.get("tagline") or _humanize_slug(slug)
    if not country_focus:
        country_focus = spec.get("country_focus") or "Global"
    if not master_ref:
        # file: ref so the local renderer materializes the clip at this path.
        master_ref = f"file:assets/programs/{slug}-explainer/walkthrough.mp4"

    explainer = build_explainer_spec(
        manifest,
        workspace=workspace,
        master_ref=master_ref,
        base_url=base_url,
        tagline=tagline,
        country_focus=country_focus,
        lower_thirds=lower_thirds,
    )
    out = Path(out_path) if out_path else Path(spec_path).parent / "explainer_spec.yaml"
    out.write_text(yaml.safe_dump(explainer, sort_keys=False, allow_unicode=True))
    explainer["_written_to"] = str(out)
    return explainer


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="scripts.ddd.snippets")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emit", help="emit the snippet manifest for a run")
    e.add_argument("run_id")
    e.add_argument("--iteration", type=int, default=None)

    x = sub.add_parser(
        "explainer-spec", help="emit an ace-web connect-ddd-walkthrough spec.yaml from a run"
    )
    x.add_argument("run_id")
    x.add_argument("--iteration", type=int, default=None)
    x.add_argument("--workspace", default="dimagi-team")
    x.add_argument("--master-ref", default=None, help="manifest ref for the master clip (library:/file:/gdrive:/url)")
    x.add_argument("--tagline", default="")
    x.add_argument("--country", dest="country_focus", default="")
    x.add_argument("--lower-thirds", dest="lower_thirds", action="store_true",
                   help="overlay a lower-third title pill per section (default off — clean dashboard)")

    c = sub.add_parser(
        "explainer-from-capture",
        help="emit a connect-ddd-walkthrough spec from a unified spec + a fresh "
             "record_video.py report (no run-state needed)",
    )
    c.add_argument("spec", help="path to the unified spec (docs/walkthroughs/<slug>.yaml)")
    c.add_argument("report", help="path to record_video.py --report JSON")
    c.add_argument("--clip", default=None, help="path to the recorded master clip")
    c.add_argument("--master-ref", default=None,
                   help="manifest ref for the master clip (default file:assets/programs/<slug>-explainer/walkthrough.mp4)")
    c.add_argument("--out", default=None, help="where to write explainer_spec.yaml (default: beside the spec)")
    c.add_argument("--workspace", default="dimagi-team")
    c.add_argument("--tagline", default="")
    c.add_argument("--country", dest="country_focus", default="")
    c.add_argument("--lower-thirds", dest="lower_thirds", action="store_true")

    u = sub.add_parser(
        "upload-video",
        help="upload a rendered mp4 and pin it to the narrative's current "
             "version on canopy-web (stamps narrative_review_id)",
    )
    u.add_argument("slug", help="narrative slug (the unified spec's name)")
    u.add_argument("video", help="path to the rendered mp4")
    u.add_argument("--base-url", default=None, help="canopy-web API base URL")
    u.add_argument("--title", default=None)

    args = p.parse_args(argv)

    if args.cmd == "emit":
        manifest = emit_snippet_manifest(args.run_id, iteration=args.iteration)
        print(json.dumps(manifest, indent=2))
    elif args.cmd == "explainer-spec":
        explainer = emit_explainer_spec(
            args.run_id,
            iteration=args.iteration,
            workspace=args.workspace,
            master_ref=args.master_ref,
            tagline=args.tagline,
            country_focus=args.country_focus,
            lower_thirds=args.lower_thirds,
        )
        print(yaml.safe_dump(explainer, sort_keys=False, allow_unicode=True))
    elif args.cmd == "explainer-from-capture":
        explainer = emit_explainer_from_capture(
            args.spec,
            args.report,
            clip_path=args.clip,
            master_ref=args.master_ref,
            out_path=args.out,
            workspace=args.workspace,
            tagline=args.tagline,
            country_focus=args.country_focus,
            lower_thirds=args.lower_thirds,
        )
        print(explainer["_written_to"])
    elif args.cmd == "upload-video":
        from scripts.ddd.upload import upload_narrative_video

        result = upload_narrative_video(
            args.slug, args.video, base_url=args.base_url, title=args.title
        )
        print(
            f"attached video to {args.slug} v{result['version']} → {result['narrative_url']}\n"
            f"  video: {result['video_url']}"
        )


if __name__ == "__main__":
    main()
