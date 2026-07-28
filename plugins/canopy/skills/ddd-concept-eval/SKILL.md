---
name: ddd-concept-eval
description: |
  LLM-as-judge eval for a rendered walkthrough. Scores six weighted dimensions
  (concept_clarity .20, design_soundness .20, why_groundedness .20, visual_polish .15,
  motion_friction .15, claim_reality_coherence .10 advisory) against the rubric bundled
  with this skill. visual_polish is where pure-aesthetic failures land — misaligned
  elements, inconsistent button styles, bad type, garish colors. Gated by ddd-spec-qa:
  if QA fails, this eval is skipped. Per scene, dispatches canopy:visual-judge with the
  concept rubric and that scene's concept_claim / provenance / captured page text as
  anchors, then aggregates to a weakest-link overall_score. Collects design_findings[]
  tagged PRODUCT / CONCEPT / RESEARCH / DEFER. Writes verdict-concept.yaml +
  design_findings.json. claim_reality_coherence findings are surfaced and scored but
  NEVER set verdict=blocked. Use when asked to "eval the concept", "score the
  walkthrough concept", or after ddd-spec-qa passes.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

## Calibration status

Provisional rubric; calibrate via a defect-creator analog after 3 real runs
(deferred per spec). Not yet calibrated.

# DDD Concept Eval

LLM-as-judge scoring of a rendered walkthrough (per-scene screenshots + captured
page text) against a 5-dimension concept rubric. Measures whether the **product
concept** is sound — not whether the video is pretty. Emits structured
`design_findings[]` that route to fixers.

**QA gate:** If ddd-spec-qa returned verdict: fail, skip this eval.

## Inputs

- **`run_dir`** — path to a rendered walkthrough run dir. Must contain:
  - `scene_<N>.png` screenshots for each scene
  - `scene_<N>_page_text.json` (captured page text from `$B text` — one file per scene)
  - `unified_spec.yaml` (or passed separately via `unified_spec_path`)
  - `why_brief.yaml` (or passed separately via `why_brief_path`)
- **`unified_spec_path`** — optional explicit path to `unified_spec.yaml` if not inside `run_dir`.
- **`why_brief_path`** — optional explicit path to `why_brief.yaml` if not inside `run_dir`.

## Procedure

### Step 0 — Check QA gate

Before scoring, verify ddd-spec-qa has passed for this spec (the script lives in
the canopy runtime):

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
# pass the file arg as an absolute path (resolved before the cd):
SPEC_ABS="$(realpath <run_dir>/unified_spec.yaml)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.spec_qa "$SPEC_ABS")
```

If the exit code is non-zero (verdict: fail), stop immediately and tell the user:

```
ddd-concept-eval: BLOCKED — ddd-spec-qa must pass before concept eval.
  blocking_reason: <spec_qa blocking_reason>
  Fix the structural issues listed above, re-run /ddd-spec-qa, then retry /ddd-concept-eval.
```

Do not score a structurally broken spec.

### Step 1 — Load rubric and artifacts

Read the bundled rubric:

```bash
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'] + '/skills/ddd-concept-eval/rubric.yaml')"
```

Read the file at that path. Also read `unified_spec.yaml` and `why_brief.yaml`.
Build a lookup: `why_brief.spine[].id` → `{claim, rationale, evidence, status}`.

### Step 2 — Per-scene dispatch to canopy:visual-judge

**Dispatch each scene's judge as a FRESH, INDEPENDENT sub-agent** (the
Agent tool), NOT inline in this conversation. The orchestrator that ran
the render — and especially any agent that helped *build* the feature —
is biased upward by ~2 points on its own work (it reads in intent the
viewer can't see and forgives flaws it remembers rationalizing). The
judge's context must contain ONLY the screenshot, the page text, the
rubric, and the context object below — none of the build history,
design rationale, or "why that's actually fine" framing. If you cannot
guarantee that independence, the judge will (per visual-judge's
Independence requirement) mark the verdict `self_assessed: true` and
apply −1 to every dimension — so make the dispatch genuinely fresh.

**Before anything else, confirm the run dir is one render's work.** Run
`python -m scripts.ddd.snapshot_consistency <run_dir>`. It fails when the
snapshots came from more than one render — which happens when an iteration
re-renders into the run dir while a judge is still reading it. A verdict scored
against a mixed dir describes frames that no longer exist and is
indistinguishable from a real one; it has already cost a full judge cycle. Do
not judge a run dir that fails this check; ask for a clean re-render first.

**Build the per-scene action trace once, before the loop**, so the concept
judge can apply action-fidelity deductions (a scene that only HOVERED where its
narration claims a fill/submit, or whose `must_succeed` action timed out, must
not score the same as one that genuinely performed the act):

```python
import json
from scripts.walkthrough._lib.results import action_trace_by_scene

report = json.load(open(f"{run_dir}/run-report.json"))   # may be absent on old runs
traces = action_trace_by_scene(report) if report else {}  # {scene_index: [{kind,target,ok,must_succeed,note}]}
```

For each scene in `unified_spec.yaml`:

> **`role: overview` scenes** are goal-setting "why" openings, not feature demos.
> Build their context as a narrative-only scene (`action_trace: []` → no
> action-fidelity deduction) and judge them on whether the frame + narration
> clearly ESTABLISH THE GOAL/why (this is what `concept_clarity`'s
> "opening scene establishes the problem" anchor rewards). A clean context frame
> (e.g. the program workspace) is the intended artifact, not a flaw — do not
> expect, or deduct for the absence of, a feature being operated.

1. Identify the screenshot path: `<run_dir>/scene_<N>.png` (the after-frame; or
   the path recorded in the run manifest). If a `<run_dir>/scene_<N>_before.png`
   exists (the render used `--capture-action-frames` and this scene effects a
   change), keep it — you'll pass the `{before, after}` pair as `frames` in
   step 4 so the judge can score the CHANGE, not just the end-frame.
2. Load the captured page text: `<run_dir>/scene_<N>_page_text.json`.
3. Build the `context` object for canopy:visual-judge:
   - `artifact_kind`: **`product_walkthrough`** — a DDD scene is a frame of a
     real, shipping web app driven through a flow. The surrounding product
     chrome (nav, sidebar, account menu, the app's own buttons) is EXPECTED
     and grounds the demo as a real product; visual-judge must NOT deduct for
     it or cap a dimension because "it looks like a tool." Fixture/placeholder
     DATA that signals an unfinished build (`test-user`, `Untitled`, lorem) is
     still a flaw — but a real entity's real system-assigned name (even an ugly
     auto-generated slug) is real production data the live product genuinely
     shows, so it is grounding, NOT a flaw. Do not coach the judge to penalize a
     real record's real name; when unsure whether a slug is fixture or real,
     treat it as real and do not deduct. Likewise, an explicit TOP-LEVEL
     synthetic/demo-environment marker (a program picker reading "Labs
     Synthetic", a demo banner, a sandbox label) is honest framing — a labs
     walkthrough SHOULD disclose it runs on synthetic data (showing real
     program data on camera would be worse), so its presence is never a flaw
     on any dimension. The synthetic-data bar applies at the ENTITY level:
     the records themselves (names, orgs, dates, amounts) must look real.
   - `narrative_anchors`: [`scene.concept_claim`, `scene.provenance`, the matching why_brief spine rationale (if resolvable)]
   - `narrative`: `scene.narrative` — the scene's FULL narration (not just the
     concept_claim), so the judge can compare what the narration CLAIMS happened
     against what the `action_trace` actually did.
   - `action_trace`: `traces.get(<N>, [])` — the per-scene action slice built
     above (each entry `{kind, target, ok, must_succeed, note}`). Pass `[]` for a
     narrative-only scene; the judge then applies NO action-fidelity deduction.
     This is what lets `motion_friction` / `design_soundness` catch a hover-only
     scene whose narration claims a fill/submit, or a `must_succeed` action that
     timed out (see the rubric's action-fidelity deduction rules).
   - `domain`: `unified_spec.name`
   - `audience.name`: the person the artifact is FOR — inferred from the scene's
     `concept_claim` / `design_intent`. For an EVIDENCE / data product (dashboard,
     monitoring, audit, analytics — the common case here), this is **the
     practitioner reading the data to decide for themselves**, NOT a stakeholder
     being persuaded. Only use a "forwarding to a funder / would they send it
     untouched" framing for artifacts whose job is genuinely persuasion (a pitch
     deck, a marketing page). The persuasion lens rewards salesmanship — for an
     evidence product that pushes the demo toward on-screen editorializing, which
     the OBJECTIVE-DATA STANDING RULE penalizes.
   - `audience.decision`: for an evidence product, "can I read the objective data
     clearly and draw my own conclusion, with definitions available on demand?";
     for a persuasion artifact, "does the argument land?"
   - `domain_expert`: the harshest relevant expert for this domain (e.g. "an M&E statistician" for impact dashboards, "a clinician" for health content) — used by the claim-scrutiny pass
   - `competitors`: best-in-class analogues for this artifact type (e.g. ["a Bloomberg terminal", "a Stripe dashboard", "an FT data graphic"] for a metrics dashboard)
   - `projector_test_phrasing`: for an evidence product, set this to a
     read-the-data framing (e.g. "could a practitioner read this screen and draw
     the right conclusion themselves?") rather than the visual-judge CEO-send
     default, so the judge does not reward a pre-digested verdict.
   - Do NOT pass `blocking_rules` — claim_reality_coherence is non-blocking by spec.
4. Dispatch `canopy:visual-judge` (as the fresh sub-agent above) with:
   - `screenshot_path`: the scene screenshot (always the after-frame)
   - `frames`: `{before: <run_dir>/scene_<N>_before.png, after: <run_dir>/scene_<N>.png}`
     ONLY when the before-frame exists (step 1) — omit otherwise (single-still
     behavior). Lets the judge's "change test" catch a scene whose effecting
     actions produced no visible change.
   - `page_text`: the captured page text
   - `rubric`: the ddd-concept-eval rubric (from Step 1)
   - `context`: the context object from step 3

Collect the per-scene verdict object. Extract all dimension scores. If
any verdict comes back `self_assessed: true`, surface that prominently
in the report — those scores are not trustworthy as a convergence gate.

### Step 3 — Tag design_findings per scene

For each dimension score ≤ 3 in the per-scene visual-judge output, create a
`design_finding` entry:

```yaml
scene: <scene index or title>
dimension: <dim_id>
severity: high | medium | low   # high if score ≤ 1, medium if score == 2, low if score == 3
route: PRODUCT | CONCEPT | RESEARCH | DEFER
fix_kind: mechanical | options | redesign
detail: <copy the justification from the visual-judge dimension output>
fix_recommendation: <copy the fix_recommendation from visual-judge, or synthesize>
```

Route assignment rules:
- `concept_clarity` findings → CONCEPT
- `use_case_soundness` findings → **always CONCEPT, fix_kind `redesign`**. A thin/trivial use case can only be fixed by changing WHAT the walkthrough demonstrates — i.e. the narrative — so it surfaces as a `concept_change` gate (which re-opens even a LOCKED narrative for redraft). NEVER route this to PRODUCT and NEVER mark it `mechanical`/`options`; the whole point is that no pixel-polish can fix "this use is trivial". Judge it against the WHOLE walkthrough (score once for the story, attach the finding to the most-emblematic scene), and emit it on EVERY render — an accepted narrative is not immune. Litmus for a finding: *would a skeptical buyer say the feature is doing something that matters, or "so what"?* (e.g. an AI coach invoked on a one-line answer → score ≤2, route CONCEPT/redesign, recommendation = "redraft the narrative so the feature does load-bearing work — invoke the coach on a full multi-question application, not a trivial single field").
- `design_soundness` findings → **PRODUCT** if the fix changes how the product is *presented* without changing what it does (e.g. interaction wording, affordance labelling, flow ordering); → **CONCEPT** if fixing it requires changing *what the product does* (e.g. a core interaction is incoherent because the underlying idea is wrong)
- `visual_polish` findings → **PRODUCT** (almost always — visual fixes are CSS/template changes that don't change *what* the product does, only how it looks). Rare exception: if the rendered chrome reveals a CONCEPT-level information-architecture problem (e.g. the layout fights the user's mental model), route CONCEPT. **Two hard rules (see the rubric's visual_polish standing rules):** (1) the host-product chrome's *presence* is grounding — never a finding; only genuinely broken/occluding/placeholder chrome is. (2) Every visual_polish `fix_recommendation` must be a **PRODUCT** change (bigger hero, a focused/expanded view, progressive disclosure, fix the occluding layout) — **never a capture/camera workaround** ("zoom in", "crop", "set full_page:false"). If the load-bearing content is too small to read, the product should present it legibly; the camera must not paper over a product-readiness gap. (The lone capture-side note allowed: a full-PAGE strip of a long scrolling page is an inaccurate capture — judge the real viewport, flag once, don't deduct the product for it.)
- `why_groundedness` findings → RESEARCH (if provenance is missing) or CONCEPT (if the claim contradicts the why_brief)
- `claim_reality_coherence` findings → always DEFER (non-blocking; note discrepancy for later triage)
- `motion_friction` findings → PRODUCT for true product friction (slow loads, dead-ends, confusing nav order). But the **narrated-subject framing** rules (see the rubric's motion_friction section) route as **scene-SCRIPTING** fixes, not product changes — these fire when the demo, not the product, framed a scene wrong: a narrated artifact left partially out of frame when it would have fit, a scene opening on a blank/still-loading frame, or an on-screen action firing out of sync with the words. The `fix_recommendation` is a spec edit (scroll_to/scroll the scene to fully frame the narrated artifact, trim the clip to content-ready, or time the action's hold to the voiceover) under the litmus *"if the UI can fully show the area we are narrating on, it should."* This is the deliberate complement to `visual_polish`'s "never blame the camera" rule: that rule forbids camera fixes when the product is too DENSE to fit a viewport; these rules DO prescribe a camera/scripting fix when the content fits but the scene simply framed it wrong.

`fix_kind` assignment — set it based on the SHAPE of your fix_recommendation,
NOT on what feels right:

- **`mechanical`** — your fix_recommendation names ONE concrete change a
  reader could apply without choosing. Examples:
  - "Add an inline LGA picker on each ambiguous row, populated from the
    candidate list returned by resolve_many."
  - "Patch unified_spec.yaml feature `resolve-many-endpoint.verify` to
    reference the actual shipped URL: POST /labs/explorer/boundaries/resolve_many/."
  - "Rename the scene title from 'Dana sees the wards' to 'Dana confirms
    each ward before commit'."
  This is the COMMON case. Most well-tuned findings are mechanical.

- **`options`** — your fix_recommendation lists 2+ paths and you couldn't
  pick. Smell tests: contains "Alternative:", "or", "could also", "consider
  X or Y". Examples:
  - "Add a spine item with id `name-resolution-confirm`. Alternative: extend
    `area-selection` to cover bulk input and document the link."
  - "Either tighten the narration to match the 6/4 reality, or expand
    scene 2 into two beats — initial resolve, then disambiguation."
  These need a user pick. The orchestrator surfaces them.

- **`redesign`** — the underlying idea needs rethinking; no single change
  fixes it. Smell tests: the recommendation is itself a question, or it
  asks for a meeting/discussion/reconception. Examples:
  - "The concept of 'all wards must be matched before commit' may be wrong
    here — consider whether partial batches make sense."
  - "Rethink what 'matched' means in the context of programmatic ward sets."
  These surface as `concept_change` — never auto-apply.

When in doubt, prefer `options` over `mechanical`. A wrongly auto-applied
finding is much worse than one extra user prompt. The orchestrator's
auto-iterate loop only acts on `mechanical` findings; anything else stops
the loop and surfaces to the user.

**claim_reality_coherence findings are surfaced and scored but NEVER set verdict=blocked.**

### Step 4 — Aggregate overall score

Compute `overall_score` across ALL scenes via `overall_rule: lowest` (the minimum
dimension score across all scenes for the **five gating dimensions**:
`concept_clarity`, `design_soundness`, `visual_polish`, `why_groundedness`, and
`motion_friction`).

`claim_reality_coherence` is EXCLUDED from the weakest-link overall_score, so it
can never drive verdict to warn/fail/blocked. It is advisory: it informs the human
at the pause point, it does not gate convergence. `claim_reality_coherence` scores
are STILL recorded per scene in the `dimensions{}` map and STILL generate
DEFER-routed `design_findings`, but they play no role in computing `overall_score`
or the final verdict.

### Step 5 — Compute verdict

| overall_score | verdict |
|---------------|---------|
| ≥ 4 | pass |
| 3 | warn |
| ≤ 2 | fail |

`verdict: "blocked"` is only set if Step 0 fired (QA gate failed). It is NOT set
for low claim_reality_coherence scores.

### Step 6 — Write outputs

Write two files to `<run_dir>/`:

**`verdict-concept.yaml`** (visual-judge verdict shape):

```yaml
schema_version: 1
kind: concept
gate: gating              # participates in render-loop convergence
live_state_verified: true # scores live per-scene screenshots + captured page text
calibration: provisional  # rubric not yet calibrated against defect fixtures
rubric_name: ddd-concept-eval
ran_at: <ISO timestamp>
run_dir: <input>

dimensions:
  concept_clarity:          { score: N, weight: 0.20, justification: "..." }
  design_soundness:         { score: N, weight: 0.20, justification: "..." }
  visual_polish:            { score: N, weight: 0.15, justification: "..." }
  why_groundedness:         { score: N, weight: 0.20, justification: "..." }
  claim_reality_coherence:  { score: N, weight: 0.10, justification: "...", blocking: false }
  motion_friction:          { score: N, weight: 0.15, justification: "..." }

overall_score: N
overall_rule: lowest

verdict: pass | warn | fail | blocked
blocking_reason: <null unless verdict==blocked>

fix_recommendation: |
  <Concrete fix description addressing the lowest-scoring dimensions.
   Tag: [PRODUCT] for interaction changes, [CONCEPT] for idea changes,
   [RESEARCH] for evidence gaps, [DEFER] for claim_reality_coherence gaps.>
```

**`design_findings.json`**:

```json
[
  {
    "scene": "<scene title or index>",
    "dimension": "<dim_id>",
    "severity": "high | medium | low",
    "route": "PRODUCT | CONCEPT | RESEARCH | DEFER",
    "detail": "<verbatim from visual-judge dimension justification>",
    "fix_recommendation": "<actionable fix>"
  }
]
```

### Step 7 — Report

Print a summary:

```
Concept Eval — <spec name>
══════════════════════════════════════
  Scenes evaluated: <N>

  concept_clarity:          N/5  — <one-line justification>
  design_soundness:         N/5  — <one-line justification>
  visual_polish:            N/5  — <one-line justification>
  why_groundedness:         N/5  — <one-line justification>
  claim_reality_coherence:  N/5  — <one-line justification> [non-blocking]
  motion_friction:          N/5  — <one-line justification>
  ────────────────────────────────────
  Overall (lowest):         N/5

  Verdict: PASS | WARN | FAIL

  design_findings: <count> findings  (PRODUCT: N, CONCEPT: N, RESEARCH: N, DEFER: N)
  Outputs: <run_dir>/verdict-concept.yaml
           <run_dir>/design_findings.json
```

If verdict is `warn` or `fail`, print the `fix_recommendation`.

If verdict is `pass`, tell the user:

```
Concept looks sound. Next step: review design_findings for any PRODUCT/CONCEPT
route items worth addressing before promoting, then proceed with the run.
```

## Output verdict shape

```yaml
schema_version: 1
rubric_name: ddd-concept-eval
ran_at: <ISO timestamp>
dimensions:
  concept_clarity:          { score: <float>, weight: 0.20 }
  design_soundness:         { score: <float>, weight: 0.20 }
  visual_polish:            { score: <float>, weight: 0.15 }
  why_groundedness:         { score: <float>, weight: 0.20 }
  claim_reality_coherence:  { score: <float>, weight: 0.10, blocking: false }
  motion_friction:          { score: <float>, weight: 0.15 }
overall_score: <float>
overall_rule: lowest
verdict: pass | warn | fail | blocked
blocking_reason: <string | null>
fix_recommendation: <string | null>
```
