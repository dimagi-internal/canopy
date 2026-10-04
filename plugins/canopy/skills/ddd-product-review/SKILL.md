---
name: ddd-product-review
description: |
  The product lens — judge the PRODUCT the demo films, all screens at once, as
  the persona trying to do their job. Not the demo, not the story, not polish:
  is the key view missing, is prose doing structure's job, are the screens
  consistent with each other and with the product's own design system and
  vocabulary, is the domain logic right. Emits product_findings.json (severity-
  graded findings that BLOCK in the product objective) + an advisory
  verdict-product.yaml. Runs on every full judge pass of a run whose objective
  is product (scripts.ddd.objective). Use when asked for a "product review" of
  a DDD run, or as part of ddd-run Step 3.
---

# DDD Product Review (the product lens)

## Why this lens exists

Every other DDD judge scores one SCENE at a time against the demo's own script.
On connect-labs `supply-sophie-unanswered-round` (2026-10-02..04) that
machinery ran ~24 judged iterations and 12 PRs and never blocked on any of
these — then one ten-minute human look at the slides found all of them:

- "big walls of text that are hard to organize"
- "a lot of AI generated text as opposed to just representing the facts"
- "I still don't get a key view of just a clear *what is the state of this
  procurement*"
- "I thought we weren't calling it a round anymore?"
- "you picked a different font and color scheme" than the rest of labs

The redesign that review triggered deleted 4,148 lines, most of them added by
the loop's own fix batches. This lens is that ten-minute look, run every
checkpoint. Its job is the question a scene judge structurally cannot ask:
**would the persona want to use this product, and what is the biggest thing
stopping them?**

## What you are NOT judging

- The narration, the story arc, scene order, pacing, framing, scroll offsets.
- Pixel polish (filled vs outlined chips, spacing) unless it makes a fact
  unreadable or misleading.
- Whether the screen literally shows what the narration claims. If they
  disagree, the narration moves (that is an accuracy fix the loop already
  makes). **Never recommend a product change whose only justification is the
  script.** The test for every product finding: *would we build this if there
  were no demo?*

## Inputs

- `run_dir` — the rendered run: `snapshots/scene_<N>.png` +
  `scene_<N>_page_text.json` for every scene, `lint_findings.json` if
  `product_lint` ran.
- `why_brief` — the persona and the job (`python -m scripts.ddd.spec_io
  why-brief <recipe>` for a split spec, else `<run_dir>/why_brief.yaml`).
- The target repo's `.canopy/ddd/context.md` and `config.yaml` `product:` block
  (glossary, design fonts) when present.

## Procedure

ONE dispatch over ALL scenes in order (like `ddd-arc-eval`), read as a product
tour rather than a story:

1. **Name the persona's job in one sentence** from the why-brief (e.g. "Sophie
   runs RUTF procurement: get comparable quotes, award, get goods delivered").
2. **Answer, in order, with evidence from the frames:**
   - `missing_view` — Is there one place that answers "where does this stand
     and what do I do next?" for the thing the persona manages? If the answer
     is spread over long scrolls or several pages, that is a finding.
   - `information_density` — Is prose doing structure's job? Sentences that
     explain state ("First, the round's own decision: 2 quotes…") instead of
     fields, chips, counts and tables. Quote the worst lines.
   - `consistency` — Does the same thing have the same name, control and
     visual treatment on every screen? (One action labelled "Open draft" here
     and "Remind" there; a stage vocabulary the next page doesn't use.)
   - `terminology` — Are the product's own words used (glossary in config,
     renamed concepts in context.md)?
   - `design_system` — Do the screens look like the rest of the product
     (typography, palette, components), or like a bolted-on page?
   - `domain_correctness` — Would a practitioner in this domain trust the
     logic? (e.g. landed cost omitting clearing under buyer-import terms; a
     reminder counted as sent when it was only drafted.)
   - `product_coherence` — Do the rules behind what the product shows look
     like a small set of general rules, or a pile of special cases written to
     make one scene work? Hard-coded narrative copy is a finding.
3. **Fold in `lint_findings.json`** — do not re-report what the lint measured;
   cite it where it supports a finding.
4. **Grade severity honestly** — this is what decides whether the loop keeps
   working:
   - `high` — the persona could not do the job, or would be misled.
   - `medium` — the persona would stumble or distrust the screen; a real
     product defect worth a fix batch.
   - `low` — a nit. It goes to the one final polish pass, not the loop.
   Three to eight findings is the normal range. Do not pad with lows.

## Output

`<run_dir>/product_findings.json`:

```json
{"findings": [
  {"scene": "1,3,4",
   "dimension": "missing_view",
   "severity": "high",
   "route": "PRODUCT",
   "fix_kind": "mechanical",
   "source": "product_lens",
   "detail": "<what is wrong, with quoted evidence>",
   "fix_recommendation": "<ONE concrete product change — structure, not copy>"}
]}
```

`scene` names where it shows (`"all"` is fine); `dimension` is one of the seven
above; `fix_kind` is `options` only when there is a genuine design choice.

`<run_dir>/verdict-product.yaml` (advisory — the score never gates; the
findings do):

```yaml
schema_version: 1
kind: product
gate: advisory
live_state_verified: true
rubric_name: ddd-product-review
ran_at: <iso8601>
dimensions:
  missing_view: {score: <1-5>, weight: 0.2}
  information_density: {score: <1-5>, weight: 0.2}
  consistency: {score: <1-5>, weight: 0.15}
  terminology: {score: <1-5>, weight: 0.1}
  design_system: {score: <1-5>, weight: 0.1}
  domain_correctness: {score: <1-5>, weight: 0.15}
  product_coherence: {score: <1-5>, weight: 0.1}
overall_score: <weighted mean>
overall_rule: weighted_mean
verdict: pass | warn | fail
fix_recommendation: <the single highest-leverage product change>
```

A fix recommendation that adds explanatory text to a screen is invalid —
rewrite it as structure, or drop the finding.
