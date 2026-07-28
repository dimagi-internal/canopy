---
name: ddd-arc-eval
description: |
  Judge a rendered narrative AS A SEQUENCE, not scene by scene. Scores five
  dimensions (arc_shape .30, escalation .25, visual_variety .20,
  persona_coherence .15, opening_and_close .10) over all the run's scenes at
  once. This is the only lens in the loop that can see repetition, a sagging
  middle, a payoff that lands before its setup, or six frames of the same
  table — every other judge sees exactly one scene and is structurally blind
  to all of it. Gated by ddd-spec-qa; runs after a clean render. Emits
  verdict-arc.yaml + arc_findings.json. Use when asked to "judge the arc",
  "does the demo build", or as part of ddd-run.
---

# DDD Arc Eval

Every other judge in the loop scores one scene at a time, and that is necessary
and insufficient. A narrative whose every scene scores 5 can still be a bad
demo: repetitive, mis-ordered, front-loaded, or arc-less. Nothing was looking
at the thing the viewer actually experiences — the sequence.

This is the lens that separates *"no scene is bad"* from *"this is amazing"*.

## Inputs

- **`run_dir`** — a rendered run dir with `scene_<N>.png` + `scene_<N>_page_text.json`
  for every scene, and `walkthrough-run-data.json`.
- **`unified_spec_path`** — the recipe (or unified spec). Supplies scene order,
  titles, personas and narration.

## Gate

If `ddd-spec-qa` returned `fail`, skip — do not judge a structurally broken
spec. If the render's `run-report.json` shows any failed action, skip too: an
arc judged over a broken take measures the take, not the arc.

Then run `python -m scripts.ddd.duplicate_frames <run_dir>`. It fails when two
CONSECUTIVE scenes captured the same picture. The usual cause is a `scroll_to`
whose target was already in the viewport: it scrolls zero pixels, does not fail,
and the scene captures the previous scene's frame while its narration plays over
a still picture.

This is worth a gate rather than a finding because the rubric prices it at a hard
cap — *"two adjacent scenes make the same point with different pixels: max 2"* —
so a run that trips it cannot score above 2 however good everything else is. It
has twice been caught only by hand-diffing screenshots inside an arc eval, a slow
non-deterministic pass spent on four lines of arithmetic; the second time, the
fix for the first was itself a no-op on the same element. If it fails, report
that as the finding and stop. Re-pointing a camera is a one-line change, and a
full eval over a stalled run measures the stall.

## Procedure

### Step 1 — Assemble the sequence

Read the spec's scenes in `build_order`. For each, collect:

- the screenshot path
- the scene's `narrative` (the spoken beat) and `title`
- its `persona`
- one line describing the dominant on-screen SHAPE, derived from the page
  text: `table` / `map` / `form` / `chart` / `modal` / `prose` / `mixed`

That shape line is what makes `visual_variety` judgeable without the judge
having to squint at thumbnails.

### Step 2 — Dispatch ONE judge over ALL scenes

Unlike the concept eval, this is a **single** dispatch with every scene's
screenshot attached in order. Splitting it per scene would defeat the entire
purpose.

Dispatch as a **fresh, independent sub-agent** — same independence requirement
as `ddd-concept-eval`, and for the same reason. Whoever built or rendered the
narrative is the last person who can see that two of its scenes make the same
point.

Pass:

- `screenshots`: ordered list of every `scene_<N>.png`
- `rubric`: this skill's bundled `rubric.yaml`
- `sequence`: the assembled list from Step 1 (title, narration, persona, shape)
- `context.domain`: the spec `name`
- `context.audience`: the same audience the concept eval uses
- `context.one_sentence_test`: "After seeing all scenes, state the story in one
  sentence. If you cannot, arc_shape is at most 2."

Explicitly instruct the judge:

> Judge the SEQUENCE. Do not re-score per-scene defects — those belong to the
> concept judge. A run of individually-flawed scenes can still have an
> excellent arc, and a run of perfect scenes can have none.

### Step 3 — Findings

For each dimension ≤ 3, emit a finding. Route:

- `arc_shape`, `escalation`, `opening_and_close` → **CONCEPT**, `fix_kind: options`
  or `redesign`. These are narrative-order problems and the fix is a spec/story
  change — reordering, cutting, or re-cutting a beat. They are NOT product bugs
  and must never be routed PRODUCT.
- `visual_variety` → **PRODUCT** when the product genuinely only has one
  surface to show; **CONCEPT** when the narrative simply chose to visit the
  same one repeatedly. Say which.
- `persona_coherence` → **CONCEPT**.

Every finding must name the specific scenes involved. "The middle sags" is
useless; "scenes 3 and 4 both show the cover table, 4 adds only a scroll" is
actionable.

### Step 4 — Write outputs

`<run_dir>/verdict-arc.yaml`:

```yaml
schema_version: 1
kind: arc
gate: gating              # a demo with no arc is not converged
live_state_verified: true
calibration: provisional
rubric_name: ddd-arc-eval
scenes_judged: <N>
dimensions:
  arc_shape:          { score: N, weight: 0.30, justification: "..." }
  escalation:         { score: N, weight: 0.25, justification: "..." }
  visual_variety:     { score: N, weight: 0.20, justification: "..." }
  persona_coherence:  { score: N, weight: 0.15, justification: "..." }
  opening_and_close:  { score: N, weight: 0.10, justification: "..." }
overall_score: N
overall_rule: lowest
one_sentence_story: "<the judge's one-sentence summary, or null if it could not>"
verdict: pass | warn | fail
```

`<run_dir>/arc_findings.json` — the findings array from Step 3.

### Step 5 — Report

```
Arc Eval — <spec name>
══════════════════════════════════════
  Scenes: <N>   Story: "<one_sentence_story>"

  arc_shape:          N/5  — <one line>
  escalation:         N/5  — <one line>
  visual_variety:     N/5  — <one line>
  persona_coherence:  N/5  — <one line>
  opening_and_close:  N/5  — <one line>
  ────────────────────────────────────
  Overall (lowest):   N/5     Verdict: PASS | WARN | FAIL
```

If the judge could not state the story in one sentence, say so first — that is
the single most important signal this lens produces.

## Why `gate: gating`

A narrative can pass every per-scene judge and still not be worth watching.
Convergence that ignores the arc converges on a set of good frames, which is
not the artifact anyone is going to sit through.
