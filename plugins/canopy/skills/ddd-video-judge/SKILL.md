---
name: ddd-video-judge
description: >
  Multimodal LLM judge for a RENDERED connect-ddd-walkthrough video — the
  audio-visual-timing counterpart to the screenshot concept judge. The concept/
  visual judge scores per-scene screenshots of the LIVE app and never opens the
  mp4, so VO↔visual coherence, pacing, and motion are invisible to it. This judge
  WATCHES the produced video: a frame is grabbed at the instant the voiceover
  speaks each named field, plus pacing frames across each scene, and a fresh
  per-scene multimodal pass scores whether the screen shows what's being said and
  whether it flows well. Use after rendering (report-only) to get a verdict-video
  with findings routed to NARRATION / FOOTAGE / PRODUCT / RENDER.
---

# DDD video judge (multimodal, rendered-video)

## Why — the gap this fills

`canopy:ddd-concept-eval` scores **screenshots of the live app** for concept
soundness and never opens the produced mp4. `canopy:ddd-timing-eval` scores
field↔word sync **deterministically** (does a named field's cursor arrive on its
word) but can't judge *semantics* (does the screen actually SHOW what's narrated)
or *feel* (pacing, motion jank). This judge is the multimodal layer: it watches
the rendered video and scores what only a viewer can.

It is **report-only** by default — a render-path verdict, parallel to the
screenshot loop. Run it on demand; do NOT gate the fast product loop on it.

## Pipeline

```
output.mp4 + beat-timeline.json + VO alignments
        │  scripts/ddd/video_judge.py  (harness)
        ▼
per-scene montages (VO-word-mark frames + pacing frames) + manifest.json
        │  per-scene multimodal judge pass (this skill)
        ▼
verdict-video.json  (per-scene scores + routed findings)
```

## Step 1 — build the evidence packets

```bash
# run_dir holds output.mp4 + beat-timeline.json (render.ts writes both)
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
(cd "$CANOPY_ROOT" && uv run python -m scripts.ddd.video_judge <run_dir> <explainer_spec.(json|yaml)> <audio_dir> <out_dir>)
```
Each scene gets `<beat>_montage.png` (frames labelled with the spoken word / pacing
%) and a `manifest.json` row (narration, the word→time→field list, window).

## Never hand back what you can measure

Anything the rendered file can answer, answer from the rendered file. "Does this
read well?", "is this the story we want?" — taste, and the human's. "Does the map
actually move?", "is there a silent tail?", "does the screen show what's being
said?" are all measurable right here. A judge that forwards a measurable question
to a human has not finished its job; it has renamed its own work as someone
else's decision. When you catch yourself about to write "worth checking whether
…", check it.

## Step 2 — judge each scene (fresh context per scene = Tough Judge)

For EACH scene, dispatch a subagent whose context contains ONLY that scene's
montage + manifest row + the rubric below — no build history, no other scenes
(cross-contamination inflates scores). Score 1–5 per dimension; default to 3 and
move up/down only with evidence from the frames.

**Rubric (weakest-link overall):**
- `vo_visual_coherence` (.35) — at each VO-word-mark frame, does the screen show
  the field/thing being spoken? A frame where the VO says "contact" but the screen
  is on a different field is the core defect. When the narration asserts a CHANGE
  rather than a state, this dimension is scored on the motion check below, not on
  the still.
- `pacing` (.25) — across the pacing frames, is the scene comprehensible — not
  fast-forward-blurred, not frozen-and-dead?
- `motion_quality` (.20) — smooth playback vs visible jank / unnatural speed-ramps
  from the time-warp.
- `narration_fit` (.20) — does the narration match what's shown (no long frozen
  tail with VO still talking; no rushed cram)?

### Asserted motion must be verified AS motion

A still-frame montage cannot falsify a narration verb that claims a visible
change — "the map reframes", "the panel expands", "the row turns green", "the
pin drops". One frame of the END STATE satisfies every still-frame check while
the viewer never sees it happen. So when the narration asserts a change, diff
consecutive frames across that word's window and confirm the region actually
moves:

```bash
ffmpeg -loglevel error -ss <t0> -i output.mp4 -t <dur> \
  -vf "fps=4,crop=<w>:<h>:<x>:<y>" f_%03d.png
# then: mean |Δ| between consecutive frames. A flat series means nothing moved,
# however correct the final frame looks.
```

Observed (`microplans-study-groups` s6, 2026-09): the line said "the map reframes
on both" over a map whose `fitBounds` had already finished OFF-SCREEN — the click
that triggered it fired while the page was scrolled down to the table, and the
scroll back up arrived after the 900ms animation was over. Every sampled frame
showed both arms correctly framed, so a still-frame read passed it. 102 of 117
frames were byte-identical: nothing moved on camera.

Route it `NARRATION` (describe the state the frames actually show) or `FOOTAGE`
(stage the change so it happens on camera — bring the target into view BEFORE the
action that triggers it). Do NOT route it `PRODUCT`: the product did the right
thing, the camera was pointed elsewhere while it did.

**Findings** — each carries a `route`:
- `NARRATION` — reword/reorder/trim the scene's `narrative` (most VO↔visual misses
  are narration naming things in a different order than the demo shows them).
- `FOOTAGE` — re-record / add demo footage (footage too short for the narration).
- `PRODUCT` — a UI moment that's confusing IN MOTION (a real app finding the
  screenshot judge missed — route it back to the product loop).
- `RENDER` — an engine artifact (bad warp ramp, clipped audio).

Return per scene: `{beat, scores:{...}, overall, verdict: pass|warn|fail, findings:[{route, text}]}`.

## Step 3 — assemble + report

Aggregate to `verdict-video.json` (overall = mean of per-scene overalls; verdict =
worst scene's verdict). Surface NARRATION/FOOTAGE findings to the video-improvement
loop; surface PRODUCT findings back to the product loop (the bonus — this judge is
a second lens on the app, not just the video).

## Relationship to other evals
- `canopy:ddd-timing-eval` — the cheap deterministic gate; run it first and only
  spend multimodal tokens here when it (and the render's overrun line) pass a floor.
- `canopy:ddd-concept-eval` — concept soundness from screenshots; orthogonal.
- `canopy:visual-judge` — the per-screenshot Tough Judge; this reuses its
  methodology over a temporal frame-strip instead of one frame.
