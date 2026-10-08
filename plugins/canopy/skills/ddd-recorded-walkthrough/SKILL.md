---
name: ddd-recorded-walkthrough
description: >
  Use when a DDD narrative should be rendered as a "recorded walkthrough" —
  the low-effort-looking, well-produced "I just recorded myself walking
  through this for someone" style — instead of the produced explainer: no
  music, no title or end cards, no zooms or callouts added afterwards, and
  several short standalone cuts (30 s target, 40 s ceiling) instead of one
  long arc. Covers authoring the cuts (style: recorded + cuts:), linting
  them, rendering one mp4 per cut, and the per-cut timing gate.
---

# DDD recorded walkthrough (`style: recorded`)

The default narrated video (`style: explainer`) is one produced arc: a title
card, every scene back to back, the brand end card, a music bed. A **recorded
walkthrough** is a different thing: a perfect, efficient version of a person
screen-recording themselves showing a colleague how something works. It
should feel low-effort and technical, and every second of it should be
deliberate.

This is a **mode on the existing pipeline**, not a second pipeline. One flag on
the spec (`style: recorded`) and a `cuts:` list change four places:

| Where | Explainer (default) | Recorded |
|---|---|---|
| `scripts/ddd/snippets.py` emitter | one `explainer_spec.yaml`, `intro_title` → scenes → `outro_card` | one `explainer_spec.<cut>.yaml` **per cut**, `body_walkthrough` beats only, no lower-thirds, `role: overview` not folded into a title card |
| `scripts/walkthrough/record_video.py` recorder | straight constant-speed cursor glides | `cursor_path: natural` — arcs + minimum-jerk easing, like a hand |
| `video-engine` renderer (`render.ts`, `src/lib/style.ts`) | music bed, captions on request, footage warp 0.7–2.5× | **no music bed, no captions**, footage warp clamped to 0.85–1.35× (near real time — at 2.5× the cursor darts) |
| gates | timing eval, dead-air | + `scripts.ddd.recorded` lint (pre-render) and cut-length gate (post-render) |

The video-engine schema refuses a `style: recorded` spec that carries a title
card, an end card, any non-walkthrough beat, or a lower-third, so a gloss
element cannot sneak back in through a hand-edited spec.

## The style rules (Sagar Atre's brief, 7 Oct 2026)

- No music, no title cards, no zooms or callouts added afterwards.
- Each cut opens on a **live screen**, and its first spoken line is
  **"This is a quick overview of how we <do X>."**
- Each cut stands alone: **30 s target, 40 s hard ceiling**, about
  **65–80 spoken words**, first person, plain English.
- The cursor moves where a person's would, and **every sentence describes
  something visible on screen**.

What the code enforces, and how hard:

| Rule | Enforced by | Level |
|---|---|---|
| cuts resolve (known scene ids, no scene in two cuts, no empty cut) | `recorded.resolve_cuts`, `spec_qa` | error |
| first line is the opener | `recorded.lint_recorded_spec`, `spec_qa` | error |
| VO estimate (words ÷ 2.6) over 40 s | lint | error |
| VO estimate over 30 s | lint | warn |
| 65–80 words | lint + gate | warn |
| rendered cut over 30 s | gate (`verdict-recorded.json`) | warn |
| rendered cut over 40 s | gate; `render_locally.py` exits 4 | **fail** |
| no music / cards / overlays / captions | video-engine schema + `style.ts` | structural |
| natural cursor | recorder `cursor_path` | structural |
| "every sentence describes something visible" | `ddd-video-judge` (`vo_visual_coherence`) | judged |

Opening on a live screen is structural: a recorded cut's first beat IS the
first scene's footage. Make that scene's first frame the screen you want the
viewer to see — give it a `url:` and a leading `wait_for` so it does not
open on a spinner (de-dwell trims a leading load, but a page that is already
there is better).

## 1. Author the cuts

Add to the narrative's **recipe** (`docs/walkthroughs/<slug>.recipe.yaml`, or
the legacy single-file spec). `style` and `cuts` are recipe fields — render
decisions, not story — so they never round-trip through canopy-web and a
`narrative pull` leaves them alone. The spoken words stay where they always
were: each scene's `narrative` (lock-owned, edited on canopy-web).

```yaml
style: recorded
cuts:
  - id: assign-dispensers            # → <slug>-assign-dispensers.mp4
    title: Assigning a dispenser     # a label only — never drawn on screen
    scenes: [dispenser-list, assign] # Scene.id values, in play order
  - id: verify-refills
    title: Verifying a refill
    scenes: [refill-visits, approve-refill]
```

Each cut's narration is its scenes' `narrative` lines in order, so the
**first scene of each cut** carries the opener:

```yaml
- id: dispenser-list
  narrative: >-
    This is a quick overview of how we assign chlorine dispensers to water
    points. I start on the district's dispenser list, where every row shows
    the dispenser and its last refill.
```

Writing the narration:
- First person ("I open…", "I pick…"), plain English, no product jargon.
- One sentence per visible thing, in the order it happens on screen. If a
  sentence names something the viewer cannot see at that moment, cut it.
- No "welcome", no "in this video", no summary or sign-off. The cut ends
  when the result is on screen.
- 65–80 words is ~25–31 s of voice at the ElevenLabs rate. The footage
  never plays faster than 1.35×, so a scene with a lot of clicking needs
  fewer words, not faster footage.
- A scene in no cut is recorded but not rendered (useful as a reset step).
- `role: overview` is allowed but not required. In recorded mode it plays as
  an ordinary screen, never as a title card.

## 2. Lint before recording

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC="$(pwd)/docs/walkthroughs/<slug>.recipe.yaml"
(cd "$CANOPY" && uv run python -m scripts.ddd.recorded lint "$SPEC")
```

The lint prints each cut's scene count, word count and estimated VO length,
then every error and warning. Exit 1 on any error. `/canopy:ddd-spec-qa` runs
the same errors, and for a recorded spec it waives the "one opening
`role: overview` scene" rule because each cut opens itself.

## 3. Record, emit, render: one mp4 per cut

Follow `/canopy:ddd-ace-render` steps 0–1 unchanged (the convergence gate and
ONE fresh master recording of every scene). The recorder sees
`style: recorded` and glides the cursor naturally. Then:

```bash
# Step 2: emit ONE spec per cut (the emitter lints and refuses on an error)
( cd "$CANOPY" && uv run python -m scripts.ddd.snippets explainer-from-capture \
    "$SPEC" "$WORK/report.json" --clip "$WORK/master.mp4" \
    --out "$WORK/explainer_spec.yaml" ) | tee "$WORK/emit.log"
# prints the lint, then "<cut_id>\t<path>" per cut (explainer_spec.<cut_id>.yaml beside --out)
grep $'\t' "$WORK/emit.log" > "$WORK/cuts.tsv"

# Step 3: render each cut against the same master (exit 4 = over the 40 s ceiling)
while IFS=$'\t' read -r CUT CUT_SPEC; do
  python3 "$VE/render_locally.py" --local-spec "$CUT_SPEC" --master "$WORK/master.mp4" --final \
    || echo "cut $CUT: render exit $? — see the gate above"
done < "$WORK/cuts.tsv"
```

Each render writes `video-engine/programs/<slug>-<cut>/runs/run-001/` with
`output.mp4`, `verdict-recorded.json` (this cut's length gate),
`verdict-timing.json` and `beat-timeline.json`, plus the dead-air report.

## 4. Gate the set

```bash
( cd "$CANOPY" && uv run python -m scripts.ddd.recorded gate --spec "$SPEC" \
    --cut assign-dispensers="$VE/programs/<slug>-assign-dispensers/runs/run-001/output.mp4" \
    --cut verify-refills="$VE/programs/<slug>-verify-refills/runs/run-001/output.mp4" \
    --out "$WORK/verdict-recorded.json" )
```

`pass` ≤ 30 s, `warn` ≤ 40 s, `fail` > 40 s or unmeasurable. The word band
can only add a warning. Overall is the worst cut. On **warn**, trim the
cut's narration if a sentence is not pulling its weight. On **fail**, trim
it or split the cut in two: a cut over 40 s is not shippable.

## 5. Judge and publish

- `/canopy:ddd-video-judge`: run per cut (each cut's run dir + spec). Its
  rubric has a recorded-style note: missing music, cards and callouts are
  the brief, not defects. It still checks that every sentence matches the
  screen (`vo_visual_coherence`), which is this style's core rule.
- `/canopy:ddd-arc-eval` and `/canopy:ddd-concept-eval`: same note. Arc is
  judged **per cut**, with no cross-cut escalation expected.
- Upload, once per cut: `snippets upload-video <slug> <cut output.mp4> --cut
  <cut id> --spec <recipe>`. The narrative version keeps one video **per cut
  id** (re-uploading a cut replaces only that cut), and the review link's
  **Cuts** tab shows each video beside that cut's narration, for guests too
  (canopy-web#1288). `--spec` resolves the cut's title and scenes from the
  recipe; a wrong id fails before anything is uploaded. The narrative's hero
  is the first cut unless you pass `--hero` on the one you want. Without
  `--cut` an upload is the version's single video, as for an explainer — so
  never upload cuts without it, or each one replaces the last. A run package
  still has one hero slot: publish it with the first cut as `--video`.

## Common mistakes

- **Putting `style`/`cuts` in canopy-web**: they are recipe fields. Edit the
  recipe; `narrative pull` will not touch them.
- **Writing the opener into a cut's second scene**: the opener is checked
  on the cut's first spoken sentence, which is its first scene's narrative.
- **Fixing an over-length cut by speeding the footage**: the warp is
  clamped to 1.35× on purpose. Cut words or split the cut.
- **Passing `--lower-thirds`**: ignored with a warning. A recorded cut has no
  post-hoc overlays.
- **Rendering through ace-web's vendored renderer** (`/ace:video-render-local`):
  that copy of the engine predates `style` and would add the music bed back.
  Use canopy's `video-engine/render_locally.py` as above.
