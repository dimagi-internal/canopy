---
name: ddd-timing-eval
description: >
  Deterministic VO↔UI timing eval for a rendered DDD walkthrough video. Unlike the
  concept/visual judge (which scores per-scene SCREENSHOTS and never sees the video
  or its audio), this measures the one thing only the rendered video has: do the
  form fields the narration NAMES actually land on their spoken word? No LLM —
  it reads the action↔word warp anchors render.ts already computes and emits a
  pass|warn|fail verdict with a 0–5 field-sync score. Use after rendering a
  connect-ddd-walkthrough (the render writes it automatically), or to re-score an
  existing run.
---

# DDD timing eval (field↔word sync)

## Why this exists — the gap in the judge

The DDD loop judges the **wrong artifact** for timing. Its concept/visual judge
(`canopy:ddd-concept-eval` → `canopy:visual-judge`) ingests **per-scene
screenshots + captured page text** and scores concept soundness. It has **no
audio, no video, no temporal channel**, and its screenshots come from
`canopy:walkthrough` re-rendering the spec against the *live app* — it never even
opens the produced `.mp4`. So whether the cursor reaches a field when the voiceover
names it is **structurally invisible** to it.

This eval fills that gap. It is deterministic (no LLM): render.ts already resolves
each field's `action_marks` against the beat's ElevenLabs VO alignment to build the
time-warp; this scores the result.

## Where it fits in the orchestration

```
SPEC ──┬─► canopy:walkthrough ─► SCREENSHOTS ─► ddd-concept-eval + visual-judge  (concept verdict)
       │
       └─► ddd-ace-render ─► record ─► VO synth ─► render.ts ──► output.mp4
                                                       └────────► verdict-timing.json   ← THIS eval
```

It is a **render-path** eval, emitted next to `output.mp4`, parallel to (not part
of) the screenshot-judging loop. The two are complementary: concept-eval answers
"is the product idea sound?"; timing-eval answers "does the produced video's audio
track its visuals?".

## What it measures

Of the form fields the **narration actually names**, how many land on their spoken
word (became monotonic warp anchors) vs drift (dropped as *inversions* — the
narration enumerates fields in a different ORDER than the form lays them out).

- `overallScore` = `5 × coverage`, where `coverage = syncedFields / wordMatchableFields`.
  `null` ⇒ the narration never names a field (a pure dashboard read) — field-sync n/a.
- `verdict`: `pass` (coverage ≥ 0.75) · `warn` (≥ 0.4) · `fail` (< 0.4).
- `meanLagRemovedS` / `worstLagRemovedS`: the field↔word lag the warp removed — the
  size of the bug it fixed (a "worst 7s" means a field was spoken 7s before the
  cursor reached it).
- It deliberately **does not** score held-frame overrun (VO over a held frame): a
  teach hold under the voice is intentional, and render already prints a
  footage-vs-VO overrun line — folding it in here double-counts and punishes
  legitimate holds.

## How to run

The render writes it automatically:

```bash
# video-engine is the ONE dev-checkout exception (Remotion node_modules is too
# heavy to bundle per plugin version — /canopy:setup step 6 installs it):
for V in ~/emdash-projects/canopy ~/emdash/repositories/canopy; do
  [ -d "$V/video-engine/node_modules" ] && VE="$V/video-engine" && break
done
[ -n "${VE:-}" ] || { echo "ERROR: no canopy checkout with video-engine/node_modules — run /canopy:setup on a dev checkout first"; exit 1; }
python3 "$VE/render_locally.py" --local-spec <explainer_spec.yaml> --master <master.mp4>
# → prints "DDD timing eval: PASS|WARN|FAIL (field-sync N/5) …"
# → writes $VE/programs/<slug>/runs/<run>/verdict-timing.json
```

Read the verdict:

```bash
python3 -c "import json; d=json.load(open('$VE/programs/<slug>/runs/<run>/verdict-timing.json')); \
print(d['verdict'], d['overallScore'], 'coverage', d['coverage']); \
[print(' ·', f) for f in d['findings']]"
```

## Acting on findings

- **`warn`/`fail` with inversions** — the narration names fields in a different
  order than the form. Either reorder the narration to follow the form, or add a
  `say:`/`word:` hint to the specific `action` so it binds to the intended word.
  (The emit-time pacing lint in `scripts/ddd/snippets.py` flags the same thing.)
- **`null` / n/a** — the walkthrough is a dashboard/map read with no narrated form
  fields; the warp doesn't engage and there's nothing to sync. Expected.

## Relationship to other evals

- `canopy:ddd-concept-eval` — concept soundness from screenshots. Orthogonal; this
  is its missing audio-visual-timing counterpart for the rendered video.
- `actionsync.ts` / `docs/action-word-sync.md` — the warp this scores.
- The render's own timing report (clip footage vs rendered duration vs held-frame
  overrun) — the footage-coverage axis this eval intentionally leaves to render.
