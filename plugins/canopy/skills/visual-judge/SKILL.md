---
name: visual-judge
description: |
  Score a captured screenshot against a caller-provided rubric using the
  Tough Judge methodology — adversarial listing first, conservative scoring
  from a 3/5 default, projector-test gate. Used by canopy:walkthrough for
  per-scene scoring and by ACE Phase 6 polish-eval for visual judgment.
  Use when asked to "judge a screenshot", "visual eval", or "tough judge".
---

# Visual Judge

Stand-alone scoring of a captured screenshot against a caller-provided
rubric. Implements the **Tough Judge methodology** — adversarial-first,
score-from-3 default, projector-test gate, calibration prior — that
canopy:walkthrough's scene loop used to inline. Extracted in canopy
v0.2.79 so the same judge can be reused by:

- **`canopy:walkthrough`** — once per captured scene (its 5-dim rubric:
  Content / App Page / Screenshot / Slide / Demo Readiness)
- **ACE `synthetic-workflow-polish-eval`** — once per polished workflow
  render (its 2-dim rubric: Visual Hierarchy / Brand Fit, alongside its
  text-based dimensions)
- **Future: any eval that needs visual judgment of a captured page**

The methodology is the durable part — adversarial-listing-before-scoring,
score-from-3-default, calibration prior, projector test, blocking rules.
Per-rubric dimensions are caller-provided so this skill stays general.

## Invocation

```
Skill('canopy:visual-judge', args={
  screenshot_path: '/abs/path/to/scene_3.png',
  page_text:       '<optional, output of `$B text` for anchoring>',
  rubric:          { ... see below ... },
  context:         { ... optional, see below ... },
})
```

Returns a verdict object (see `## Output verdict shape`).

## Inputs

### `screenshot_path` (required)

Absolute path to a PNG. The skill reads it via the Read tool so the
captured image is in the judge's context window during scoring.

### `frames` (optional) — a before/after pair for action scenes

For a scene that EFFECTS a state change (fills a form, submits, awards), one
still frame can't show what CHANGED — the before state is gone by the time the
end-frame is taken. When the caller has captured a before/after pair (the
recorder's `--capture-action-frames` writes `scene_<N>_before.png` alongside
the canonical `scene_<N>.png`), pass them as:

```yaml
frames:
  before: '/abs/path/to/scene_2_before.png'   # state at the action loop's start
  after:  '/abs/path/to/scene_2.png'          # state after the actions ran (== screenshot_path)
```

When `frames` is provided, Read BOTH images and judge the **change**: does the
after-frame show the RESULT of the act the narration claims (a new row in the
list, a confirmation, an awarded badge)? An after-frame identical to the
before-frame means the actions effected nothing visible — the task was claimed,
not shown (combine with `action_trace` if present). Score the dimensions over
the after-frame (it is the steady-state the user lands on), but let the
before→after delta INFORM the action-fidelity check: no visible change where
the narration claims an effecting act caps the relevant dimension per the
caller's `deduction_rules`.

**Back-compat:** `frames` absent ⇒ judge the single `screenshot_path` exactly
as today. `screenshot_path` remains REQUIRED and is always the after-frame.

### `page_text` (optional but strongly recommended)

The output of `$B text` (or equivalent) for the page that was screenshotted.
Without it, dimensions that cite "exact text" can't be scored faithfully —
e.g. "quote the worst sentence verbatim" only works against text the judge
can read. The walkthrough loop captures this; ACE evals capture it via
`browse text` after `browse goto`.

### `rubric` (required)

Caller-provided rubric YAML/object:

```yaml
name: <rubric name, e.g. "walkthrough" or "synthetic-polish">
default_score: 3        # the start-from default (3/5 by Tough Judge convention)
overall_rule: lowest    # or "weighted-mean"; "lowest" is canon for walkthrough
dimensions:
  - id: visual_hierarchy
    label: "Visual Hierarchy"
    weight: 0.40        # required; weights sum to 1.0
    anchor:
      "5": "World-class. Hero KPI prominent, perfect type scale, clear primary/secondary."
      "4": "Strong, with one designer-polish thing left to do. Name the thing."
      "3": "Functional. Ships. Nothing embarrassing, nothing delightful. (DEFAULT)"
      "2": "Visible problem a careful viewer catches: cramped spacing, etc."
      "1": "Damages credibility. Misaligned, unstyled, broken."
    deduction_rules:           # optional; per-dimension hard-caps
      - "Demo data artifacts ('Untitled', 'test-user') visible: max 2"
      - "Empty state dominating frame: max 3"

  - id: brand_fit
    label: "Brand fit"
    weight: 0.20
    anchor:
      "5": "..."
      ...
```

**Required fields per dimension:** `id`, `label`, `weight`, `anchor`.
**Optional:** `deduction_rules` (per-dimension hard-deduct triggers).

**Default score (3/5):** the calibration prior is "every dimension starts
at 3 — every step up earned with specific evidence, every step down
reflects a specific problem." Override only with a documented reason.

**Overall rule:** `lowest` (Tough Judge canon — weakest-link scoring,
walkthrough-style) or `weighted-mean` (more permissive — useful when
dimensions are independent).

### `context` (optional)

Caller-provided context that shapes the adversarial pass:

```yaml
artifact_kind: product_walkthrough   # or: standalone_deliverable (default)
audience:
  name: "the CEO who is about to forward this into a high-stakes external thread"
  decision: "whether to hit Send with their name on it, or fix something first"
competitors: ["Linear", "Notion", "Slack", "Vercel", "Superhuman"]
projector_test_phrasing: |
  "I am the CEO, about to forward this — with my name on it — into a
  high-stakes external thread (a board, a major customer, a funder).
  Would I find ANYTHING I'd want fixed before I hit Send? Answer NO
  only if I would send it untouched, with zero verbal caveats."
domain_expert: "a domain expert who would nitpick the methodology"  # e.g. "an M&E statistician", "a security auditor"
narrative_anchors:                 # optional; specific claims this judgment can verify
  - "the headline panel must show ≥3 named FLWs with archetype labels"
  - "Dinesh's coaching arc must be visually called out"
domain: "turmeric market survey"   # informs brand-fit + content judgment
narrative: |                       # optional; the scene's FULL narration (not just an anchor)
  "Dana opens the solicitation builder and fills out the funding terms,
   then submits — the new solicitation appears in the list."
action_trace:                      # optional; what the demo ACTUALLY did this scene
  - { kind: hover, target: "New solicitation", ok: true, must_succeed: false }
  - { kind: hover, target: "Funding terms",   ok: true, must_succeed: false }
```

**`action_trace` (optional) — what the demo ACTUALLY did, not just what it
shows.** A screenshot is a single still FRAME: a scene that filled and submitted
a form and a scene that only HOVERED over the same controls produce the *same
end-frame*. Without the trace you cannot tell "the task was performed" from "the
task was merely claimed". When the caller passes `action_trace` (the per-scene
slice of the recorder's run-report — each entry `{kind, target, ok, must_succeed,
note}`), use it as a SCORING INPUT for action-fidelity:

- **EFFECTING action kinds** — `click`, `click_menu`, `fill`, `select`, `type`,
  `press`, `draw`. These actually change state (submit a form, pick an option,
  award a response).
- **NON-EFFECTING action kinds** — `hover`, `scroll_to`, `scroll`, `wait_for`,
  `hold`, and `goto` (navigates, but on its own effects no form action). These
  only move the camera or wait.
- **The claim↔action check:** if `context.narrative` (or a `narrative_anchor`)
  asserts an *effecting* act — "create / fill out / submit / select / award /
  publish / enter / type" — but the `action_trace` for this scene contains **no
  effecting action** (only hovers/scrolls/waits), the task is CLAIMED, not
  SHOWN. List it as a top-rank Phase-1 flaw and apply the caller's
  action-fidelity deduction (the caller's rubric `deduction_rules` say which
  dimension caps and to what).
- **The failed-action check:** any `action_trace` entry with `ok: false` means a
  demo action failed or timed out (e.g. an award click that silently timed out).
  The captured frame then does NOT show the claimed action's result. List it and
  apply the caller's deduction. A failed `must_succeed` action is the most
  severe form.

When `action_trace` is ABSENT, behave exactly as today — score from the
screenshot + page_text alone, no action-fidelity reasoning. The trace is
purely additive: it can only *lower* a score that the still frame would have
let pass, never raise one.

When omitted, defaults are the high-stakes-send lens above (the CEO is
about to forward it with their name on it; competitors Linear/Notion/etc.;
projector test as quoted). The send lens is intentionally harsher than a
generic "would you adopt this" — personal stakes and a binary Send/fix
decision surface flaws a detached reviewer waves through.

**`artifact_kind` — what is being judged (read this; it changes the chrome rule):**

- **`product_walkthrough`** — the screenshot is a frame of a *real, shipping
  web application* being driven through a flow (a DDD/walkthrough capture, a
  demo of a live product). Here the **surrounding product chrome — the nav
  bar, sidebar, breadcrumbs, account menu, the app's own buttons — is
  EXPECTED and GROUNDING, not a flaw.** It is the evidence that this is a real
  working product and not a mockup or a slide. **Do NOT deduct for product
  chrome, and do NOT cap any dimension because "it looks like a tool
  screenshot" — it IS the tool, on purpose.** What you still penalize here is
  *fake or broken* content inside that real frame: **fixture/placeholder DATA
  that signals an unfinished build** (`test-user`, `Untitled`, lorem, a
  duplicate title, a placeholder avatar), inconsistent/misformatted numbers, a
  self-contradicting verdict, illegible charts, an empty state dominating the
  frame. **But distinguish placeholder data from real-but-ugly PRODUCTION
  data:** a real record's real system-assigned identifier — even an ugly
  auto-generated slug — is what the live product genuinely shows, so under
  `product_walkthrough` it is GROUNDING like the chrome, not a flaw. Penalize a
  slug only when it reads as a *fixture* (`test-user`, `Untitled`), not when it
  is the real (if ugly) name of a real entity in the running system. When you
  can't tell, treat it as real production data and do not deduct — penalizing
  the product for being real is the failure mode this mode exists to prevent.
  **An explicit TOP-LEVEL synthetic/demo-environment marker is also never a
  flaw** (a program picker reading "Labs Synthetic", a demo banner, a sandbox
  label): a demo SHOULD disclose it runs on synthetic data — putting real
  program data on camera would be worse — so honest environment framing is
  intended, on any dimension including trust. The fixture-data penalty above
  applies at the ENTITY level (the records themselves — names, orgs, dates,
  amounts — must look real), not to the environment's honest self-labeling.
  The bar is "is this a polished, coherent, honest view *of a real product*",
  not "is this a chrome-free standalone graphic."

- **`standalone_deliverable`** (default) — the artifact is meant to stand on
  its own when forwarded: a slide, an exported figure, a report page. Here
  app chrome IS a leak (it reveals an unfinished export), and the chrome rules
  below apply at full strength.

When `artifact_kind` is omitted, treat it as `standalone_deliverable`
(preserves the strict default). Callers judging a live-product demo should
pass `product_walkthrough`.

## Process — Tough Judge methodology

You are the harshest reviewer this product will ever face. Your job is
to find what's wrong, not feel good about what's right. If you're
scoring generously, you're scoring wrong.

### Independence requirement (read before scoring)

**The judge must have NO stake in, and no context from, building the
thing it judges.** A builder scoring their own artifact reliably scores
1–2 points too high — they know what each element *means to*, forgive
flaws they remember rationalizing, and read intent the viewer can't see.
This is the single largest source of inflated scores.

- **Callers MUST dispatch this skill as a fresh sub-agent** (e.g. the
  Agent tool) whose context contains ONLY the inputs to this skill —
  the screenshot, `page_text`, `rubric`, and `context`. It must NOT
  inherit the build conversation, the design rationale, or the author's
  framing of why a choice is fine.
- **If you are scoring something you (or your current conversation)
  helped build, you cannot give independence — apply a hard −1 to every
  dimension** and mark the verdict `self_assessed: true (unreliable)`.
  Say so explicitly. A self-assessed 5 is a 4 at best; a self-assessed
  4 is a 3.

**Calibration prior:** you are biased upward. Don't mechanically
deduct — **justify every 4 or 5 in one sentence a skeptical stranger
would accept.** If the justification reads as "it works" or "it's
clean," the score isn't a 4 — that's a 3.

**The CEO-send gate (the bar for a 5):** before any dimension can earn
a 5, pass this test — *"I am the CEO. I am about to forward this, with
my name on it, into a high-stakes external thread (a board, a major
customer, a funder). Would I find ANYTHING I'd want fixed before I hit
Send?"* If the honest answer is "I'd tweak one thing first," that
dimension is a 4, not a 5. One needed caveat, one hedge, one number that
needs explaining = not a 5 — and the fix for a number that needs
explaining is a clearer structure (its label, its unit, its column), never
an explanation added beside it.

### Phase 1: Adversarial listing (MANDATORY before any scoring)

Read the screenshot (Read tool on `screenshot_path`) and the
`page_text` if provided. Then write the following lists, in this order.
Be specific throughout: quote exact text from `page_text`, name exact UI
elements visible in the screenshot. Vague flaws ("a bit cluttered")
don't count — if you can't point at it, you didn't find it.

1. **Every real defect you'd want fixed before `<context.audience>` hits
   Send, each with a SEVERITY** — no quota. Sweep every major region of
   the layout (header/chrome, hero/KPI area, charts, map/media,
   footer/panels) so a region is skipped because it is clean, not because
   you skimmed it, but list only what you can point at and would actually
   fix. A clean region yields nothing; padding the list to look thorough
   turns into a backlog the loop chases forever. Grade each:
   - **high** — misleads or blocks: a wrong/contradictory number, a claim
     the screen does not back, a broken or occluding layout, placeholder
     data, the 5-second read lands wrong.
   - **medium** — a careful user would stumble: inconsistent formats or
     vocabulary across the screen, illegible load-bearing content, an
     action whose effect is not visible, prose doing a structure's job.
   - **low** — polish a designer would note (spacing, a slightly flat
     hierarchy); it never holds a score below 4 on its own.
   Rank by severity. Things to hunt:
   - **Fixture/placeholder DATA that signals an unfinished build** (`Untitled`,
     duplicate titles, `test-user`, placeholder avatars, lorem text) — these
     are flaws in any `artifact_kind`. **But do NOT conflate this with
     real-but-ugly PRODUCTION data:** a real entity's real system-assigned name
     — even an ugly auto-generated slug — is what the live product actually
     shows. Under `product_walkthrough` that is grounding, not a flaw (you'd be
     penalizing the product for being real). Flag a slug only when it reads as a
     fixture, not when it is the genuine name of a real record in the running
     system; when unsure, treat it as real and don't deduct.
   - **Internal app chrome** (nav bars, breadcrumbs, "Select context",
     account menus, edit/admin affordances). **Conditional on
     `artifact_kind`:** for a `standalone_deliverable` this is a leak — list
     it. For a **`product_walkthrough` it is NOT a flaw — it grounds the
     demo as a real shipping product; do not list it, and do not let it cap
     any dimension** (see Phase 3). Real product chrome that is broken,
     inconsistent, or mislabeled is still fair game in either mode.
   - Empty states dominating the frame; error/warning banners
   - **Inconsistencies** — the same quantity formatted two ways
     (`2,300` vs `2300`), two different numbers both presented as "the"
     headline, mismatched units, drifting capitalization
   - **Labels a non-expert in the audience can't parse** (acronyms, stats
     notation `pp`/`CI`/`n=`, domain shorthand). The fix is a plainer
     LABEL or unit in the structure itself — never a glossary, tooltip,
     legend or definition added beside it.
   - **Explanatory copy doing structure's job** — helper sentences,
     captions, "click X to Y" hints, legends, info bubbles, inline
     definitions. Each one is a flaw (medium), not a remedy: the screen
     should say it through a label, a field, a chip, a column or its order.
   - **Hardcoding / special-casing the demo** — copy or behaviour that only
     makes sense for these records: a persona or partner named in the
     product's own copy, a status or chip that exists for one row, a column
     only this dataset fills, wording that narrates this scenario. A flaw
     (high when it would mislead a real user, else medium); the fix is the
     general rule or removal.
   - Charts/figures with no axis labels, units, or scale
   - Low contrast, cramped spacing, inconsistent icon/type sizes,
     flat hierarchy (housekeeping metrics sized like headline findings)
   - Claimed-but-not-shown behavior (narrative says "streaming" but
     nothing streams)

   **Every fix you name removes or restructures.** "Add a tooltip /
   legend / definition / explanatory line" is never the recommendation;
   if the only fix you can see is more words, say so and grade the flaw
   low.

2. **Claim-scrutiny — does the artifact's implicit claim survive
   `<context.domain_expert>`?** State, in one sentence, the claim a
   viewer will infer from this view. Then attack it as the expert would.
   Hard triggers (each one is a real flaw to list):
   - A **self-disclaiming** element — a caveat/footnote/banner that
     undercuts the headline it sits next to ("...not a causal estimate"
     under a big causal-looking number). If the artifact argues against
     its own hero, that's a top-rank flaw.
   - A comparison or number that an expert would call **unsupported by
     what's shown** (no baseline, no denominator, selective rigor —
     e.g. a confidence interval on only the flattering metric).
   - The displayed numbers matching the spec is NOT the bar. The bar is
     **the claim a viewer draws matching what the data can honestly
     support.**

3. **At least three ways a competitor does this better.** Pick from
   `<context.competitors>` (or the default list:
   Linear/Notion/Slack/Vercel/Height/Superhuman). Describe concretely
   what they do that this view doesn't. If you cannot name three,
   you are not thinking adversarially enough — look again.

4. **The 5-second test.** Glance at the screenshot as a first-time
   viewer for five seconds. What is the ONE thing this view is trying to
   tell you — and did you get it, or get a *wrong* read, in five
   seconds? Name the single biggest source of "wait, what am I looking
   at?" Misreads here cap `concept_clarity` / `visual_hierarchy`.

5. **The binary send test.** Use `<context.projector_test_phrasing>`
   verbatim, answer YES or NO. This answer is a hard gate on any
   "demo readiness" / "shippable" dimension below, AND on whether any
   dimension can reach 5 (see the CEO-send gate above).

6. **Action-fidelity check (ONLY when `context.action_trace` is provided).**
   Compare what `context.narrative` claims happened against what the
   `action_trace` actually did this scene. State, in one sentence, the act the
   narration asserts (e.g. "Dana submits the new solicitation"). Then check the
   trace:
   - **Claimed-but-not-performed** — the narration asserts an effecting act
     (create / fill / submit / select / award / publish / enter / type) but the
     trace has NO effecting action (`click`/`fill`/`select`/`type`/`press`/
     `draw`) — only hovers/scrolls/waits. List this as a TOP-RANK flaw: the
     viewer is told an action happened that the demo never performed.
   - **Action failed** — any trace entry has `ok: false` (the action failed or
     timed out). List it, noting whether it was `must_succeed`. The end-frame
     does not reflect the claimed result.
   These feed the caller's action-fidelity `deduction_rules` in Phase 2.

7. **The change test (ONLY when `context.frames` is provided).** Read BOTH the
   before- and after-frame. State, in one sentence, what VISIBLY changed
   between them. If the scene's narration claims an effecting act (create /
   fill / submit / award) but the after-frame is visually unchanged from the
   before-frame, list it as a top-rank flaw — the act effected nothing the
   viewer can see. This is the single-still-frame blind spot this input exists
   to close, and it feeds the caller's action-fidelity `deduction_rules`.

Output all the lists above as a block. ONLY THEN proceed to scoring.

### Phase 2: Score each dimension, starting from `rubric.default_score`

For each dimension in `rubric.dimensions`:

- Apply the `anchor` definitions verbatim — don't paraphrase.
- Default to `default_score` (3 typically). Move up or down from the
  default with specific evidence.
- Apply per-dimension `deduction_rules` (if any) as HARD caps:
  rule fires → score caps at the rule's `max`, regardless of other
  evidence.
- For every score ≥ 4, write a one-sentence justification a skeptical
  stranger would accept.
- For every score ≤ 2, write a one-sentence reason citing a specific
  observable problem.

Computed `overall`:
- `rubric.overall_rule == "lowest"` → minimum dimension score
- `rubric.overall_rule == "weighted-mean"` → sum(dim.score * dim.weight)

### Phase 3: Cross-check (sanity floor)

Before emitting the verdict, check these sanity rules:

- **If the send test (Phase 1.5) is NO, NO dimension can reach 5.** The
  CEO-send gate is the definition of a 5; a "fix one thing first" answer
  caps every dimension at 4.
- **If ANY of your Phase-1.1 flaws graded HIGH is in the screenshot, any
  "demo readiness" / "shippable" dimension cannot exceed 3**; a MEDIUM one
  caps it at 4. LOW flaws never cap a score on their own.
- **If a claim-scrutiny trigger fired (Phase 1.2)** — a self-disclaiming
  element, or a claim an expert calls unsupported by what's shown — then
  `claim_reality_coherence` (and any "concept" dimension) cannot exceed
  2. A view that argues against its own hero is not coherent.
- **If internal app chrome is visible AND `context.artifact_kind` is
  `standalone_deliverable`** (the default), any "visual hierarchy" /
  "design" / "screenshot quality" dimension cannot exceed 3 — it reads as a
  tool screenshot, not a deliverable. **This cap does NOT apply when
  `artifact_kind` is `product_walkthrough`** — there the surrounding product
  chrome is expected and grounding, so it never caps a score. (Test/placeholder
  DATA artifacts — a raw slug, `test-user`, lorem — still cap, in either mode.)
- **If the 5-second test (Phase 1.4) produced a wrong read or a "what am
  I looking at?"**, `concept_clarity` / `visual_hierarchy` cannot exceed 3.
- **If a competitor does it obviously better in all 3 named ways, the
  "app page quality" / "visual hierarchy" / equivalent dimension cannot
  exceed 3.**
- **If this is a self-assessment** (you lacked build-independence — see
  Independence requirement), apply −1 to every dimension after all other
  rules, floor 1, and set `self_assessed: true` in the verdict.
- **Every 4 or 5 needs a one-sentence justification a stranger would
  accept.** "It works" / "it's clean" is not a 4 — that's a 3. Revise
  down if you can't name what earns the step up.

If a sanity rule binds and the dimension score has to drop, do it
explicitly and note "(sanity floor: <rule>)" in the dimension's
justification.

### Blocking rules (caller may opt in)

The walkthrough loop applies these blocking rules; other callers may
opt in by passing `context.blocking_rules: ["demo_readiness_low",
"narrative_falsified"]`:

1. **`demo_readiness_low`** — if any scored dimension labeled
   "demo readiness" (case-insensitive) lands ≤ 2, stop and tell the
   caller. Don't silently log it.
2. **`narrative_falsified`** — if the page contradicts a
   `context.narrative_anchors` claim, stop. The premise is wrong, not
   just the polish.

When opted-in and either fires, return a verdict with
`verdict: "blocked"` and `blocking_reason: <which rule>` instead of
the normal scored verdict.

## Output verdict shape

```yaml
schema_version: 1
rubric_name: <rubric.name>
ran_at: <ISO timestamp>
screenshot_path: <input>

# Phase 1 outputs
adversarial:
  embarrassing:          # every real defect, ranked by severity — no quota; [] for a clean frame
    - { severity: high, flaw: "verbatim quote / specific UI description", fix: "the structural change (never added copy)" }
    - { severity: medium, flaw: "...", fix: "..." }
  claim_scrutiny:
    inferred_claim: "<the claim a viewer draws from this view>"
    survives_expert: YES | NO
    triggers: ["self-disclaiming caveat under hero", "no baseline shown", "..."]
  competitors_better:
    - { product: "Linear", what: "specific thing they do" }
    - { product: "Notion", what: "..." }
    - { product: "Superhuman", what: "..." }
  five_second_read:
    got_intended_message: YES | NO
    actual_first_read: "<what a first-time viewer takes away in 5s>"
  projector_test: YES | NO     # the CEO-send gate; NO ⇒ no dimension reaches 5
  projector_test_reason: <one-sentence>

# Phase 2 outputs
dimensions:
  visual_hierarchy: { score: 4, weight: 0.40, justification: "..." }
  brand_fit:        { score: 3, weight: 0.20, justification: "..." }
  ...

overall_score: <number>
overall_rule: lowest | weighted-mean

# Phase 3 outputs (sanity floors that bound)
sanity_floors_applied:
  - { dimension: "visual_hierarchy", rule: "competitor_dominance_3", from: 4, to: 3 }

verdict: pass | warn | fail | blocked
blocking_reason: <when verdict==blocked> demo_readiness_low | narrative_falsified
self_assessed: false   # true if the judge lacked build-independence (scores then unreliable, −1 applied)

fix_recommendation: |
  Concrete fix description. [CODE | SPEC | DATA | INFRA] tag.
```

## Calibration target

The methodology itself is calibrated against the walkthrough corpus
(canopy `evals/walkthrough/fixtures/` — defects with known severity
scores). When `canopy:walkthrough-eval` runs against fixtures, the
visual-judge dispatch should produce ground-truth-aligned scores. If
detection rate drops below 80% or inter-run variance rises above 0.5,
fix the methodology here, not in caller rubrics.

Per-rubric calibration (the `rubric` itself) is the caller's
responsibility — see `skills/eval-calibration/` in canopy or the
equivalent doc in the consuming repo.

## Why this lives in canopy

The Tough Judge methodology was authored for canopy:walkthrough but
is genuinely general — any eval that needs visual judgment of a
captured page benefits from the same adversarial-first + score-from-3
discipline. Embedding it inside walkthrough's scene loop forced
copy-paste for every other vision-judging eval. canopy is the right
home: it's the plugin that owns the methodology, the eval-fixture
corpus, and the calibration doc.

## Change Log

| Date | Change | Author |
|---|---|---|
| 2026-05-07 | Initial extraction from canopy:walkthrough Phase 1–4 inline scoring. Methodology preserved verbatim; per-rubric dimensions parameterized via the `rubric` input. canopy:walkthrough now dispatches this skill per scene; ACE polish-eval consumes it for visual dimensions. | canopy team |
| 2026-06-02 | Harshness pass. Added (1) an **Independence requirement** — judge must run as a fresh sub-agent with no build context; self-assessment forces −1/dimension + `self_assessed` flag. (2) The **CEO-send gate** as the definition of a 5 (would the CEO forward it untouched, with their name on it?). (3) Raised the Phase-1 flaw floor from 3 to **≥8, ≥1 per layout region**, ranked. (4) A **claim-scrutiny** pass (self-disclaiming elements / unsupported claims cap claim_reality_coherence ≤2). (5) A **5-second first-impression** pass and an **internal-chrome / deliverable-readiness** check, each with sanity-floor caps. Motivated by an observed builder-as-judge inflation of ~2 points. | jjackson |
| 2026-06-18 | **`action_trace` + `narrative` context fields (action-aware judging).** The judge scored ONE still screenshot per scene, so a scene that filled+submitted a form and one that only HOVERED over the same controls (same end-frame) scored identically. Added an optional `action_trace` (the per-scene slice of the recorder's run-report: `{kind, target, ok, must_succeed, note}`) and the scene's full `narrative`. When present, a new Phase-1 action-fidelity check compares the narrated act against what the trace actually did: a narration that asserts an effecting act (create/fill/submit/select/award/publish) while the trace only hovers/scrolls ⇒ "claimed, not shown" (top-rank flaw); any `ok:false` entry ⇒ the action failed/timed out. These feed the caller's action-fidelity `deduction_rules`. Back-compat: absent `action_trace` ⇒ score from the still frame exactly as before; the trace can only LOWER a score, never raise one. | jjackson |
| 2026-06-18 | **`frames` input (before/after change-judging).** A scene that EFFECTS a state change (fills/submits/awards) can't be judged from one still — the before state is gone by the end-frame. Added an optional `frames: {before, after}` pair (the recorder's `--capture-action-frames` writes `scene_<N>_before.png` beside the canonical `scene_<N>.png`). When provided, the judge reads both and a new Phase-1 "change test" lists it as a flaw when the narration claims an effecting act but the after-frame is visually unchanged — feeding action-fidelity deductions. Back-compat: `frames` absent ⇒ judge the single `screenshot_path` (still required; always the after-frame) exactly as before. | jjackson |
| 2026-10-07 | **Severity-gated findings replace the "at least EIGHT" quota (canopy#786).** A fixed flaw quota guaranteed a backlog on every pass, and with "any unfixed Phase-1.1 flaw caps demo readiness at 3" it guaranteed a 3 — the Spark and supply runs (~9 h each, 434 and 200 subagents) never converged. Phase 1.1 now lists only real defects, each graded high/medium/low; only high caps readiness at 3, medium at 4, low never caps. Added **explanatory copy** and **hardcoding / special-casing the demo** as flaws, re-pointed the jargon item at plainer labels instead of "a plain-language read", and every recommended fix must remove or restructure — never add a tooltip, legend or definition. | jjackson |
| 2026-06-03 | **`artifact_kind` context field.** Distinguishes `product_walkthrough` (a frame of a real, shipping web app being driven through a flow) from `standalone_deliverable` (a slide/figure/report meant to stand alone, the default). For a `product_walkthrough` the surrounding product chrome — nav bar, sidebar, breadcrumbs, account menu, the app's own buttons — is EXPECTED and grounding, NOT a flaw: it is the evidence the demo is a real product and not a mockup. The "internal app chrome → max 3" sanity floor no longer fires in walkthrough mode. Test/placeholder DATA (raw primary-key slugs, `test-user`, `Untitled`, lorem) still caps in either mode. Motivated by walkthrough judges wrongly penalizing real-website nav that is the point of a live-product demo. | jjackson |
