---
name: ddd-spec
description: |
  Author a unified spec (docs/walkthroughs/<narrative-slug>.yaml) from a validated
  why_brief.yaml. Write ONE cohesive multi-persona demo narrative first, then
  decompose it into ordered story-beat scenes (each carrying concept_claim,
  provenance, design_intent, and verifiable features). The output is
  simultaneously a design doc and a runnable canopy walkthrough spec. Loops
  until scripts.ddd.validate unified_spec passes. Use when asked to "write the
  spec", "author the unified spec", or after ddd-why-qa passes.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

## Lock gate (run BEFORE authoring — do NOT skip)

An approved narrative is **durable input**, not something to regenerate. If the
target spec already exists and is `narrative_locked`, the human has signed off on
this story arc at the narrative-agreement gate — re-authoring it would silently
discard their edits (the whole spec: narrative paragraph + every scene's
narrative/show/design_intent/features/actions).

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
test -f "docs/walkthroughs/<narrative-slug>.yaml" && \
  (cd "$DDD_REPO" && uv run python -m scripts.ddd.narrative locked "<abs-spec-path>") || echo unlocked
```

- Prints **`locked`** → **STOP. Do not regenerate.** Report "narrative is locked
  (approved) — reusing it verbatim" and exit. The caller proceeds to render with
  the existing spec. The ONLY way to re-author a locked narrative is an explicit
  **redraft** (the narrative-agreement gate returning `redraft`, which clears the
  lock via `apply_narrative_edits`) or a manual `python -m scripts.ddd.narrative
  unlock <spec>`.
- Prints **`unlocked`** (or no spec yet) → author/refresh the spec as normal.

# DDD Unified Spec

## Where the spec lives (ownership, L1)

A narrative has **two owners**, so after it is agreed it lives in two files:

| file | owner | what it holds |
|---|---|---|
| `<slug>.recipe.yaml` | **git** | how to film it — `show`, `url`, `viewport`, `actions`, `pace`, `concept_claim` |
| canopy-web | **cloud** | the story — `narrative`, per-scene `title`/`persona`/`provenance`/`narrative`/`features` |
| `<slug>.narrative.lock.json` | **generated** | a committed cache of the story at a pinned version |

**While authoring a NEW narrative you still write ONE file**,
`docs/walkthroughs/<narrative-slug>.yaml` — you are inventing the story and the
recipe together and splitting them early would only get in the way. It becomes a
**draft**: once the narrative-agreement gate approves it, run

```bash
python -m scripts.ddd.split_spec docs/walkthroughs/<narrative-slug>.yaml
git rm docs/walkthroughs/<narrative-slug>.yaml    # the draft is spent
```

and from then on canopy-web owns the story; `narrative pull` refreshes the lock.

**Never hand-write or hand-edit a `.narrative.lock.json`.** It is generated, and
`python -m scripts.ddd.check_locks docs/walkthroughs` fails the build if a recipe
field appears in one. To change the story, change it on canopy-web and re-pull.

Every scene needs a stable explicit **`id:`** — a permanent slug, never derived
from the title. It is the key the recipe and the story are joined on, and the key
canopy-web's version diff pairs on. Renaming an id is deleting a scene and adding
another, and reads that way everywhere.

## What the spec is

Author a `docs/walkthroughs/<narrative-slug>.yaml` that is simultaneously:
1. The **design doc** — every scene asserts a testable concept_claim backed by a
   spine item (provenance).
2. A **runnable canopy walkthrough spec** — keys `name`, `narrative`, `base_url`,
   `auth`, `personas`, `scenes` conform exactly to the canopy walkthrough engine
   so it can be played directly by `/canopy:walkthrough`.

The unified spec is the linchpin artifact of the DDD v2 loop.  It is authored
FROM the grounded `why_brief.yaml` produced by Phase 0 (ddd-why-brief + ddd-why-qa).

## Inputs

- **`why_brief_path`** — path to the validated `why_brief.yaml`.
- **`narrative_slug`** — short slug used in the output filename and `name` field.
- **`base_url`** — the URL of the live environment to walk through.
- **`run_dir`** — directory to write the spec (default: `docs/walkthroughs/`).

## Authoring checklist (best practices at a glance)

Before submitting a spec for the actionability gate, scan this list. Each
bullet points down to the section that explains it. Authors who already know
the patterns scan the checklist; authors who don't read the deeper sections.

- **Use `Scene.url:` for the entry point — don't repeat the URL as the first `goto` action.** The recorder navigates to `scene.url` automatically; a duplicate leading `goto` causes a visible page reload ~1-2s into the scene. (See Step 4 — Quick reference for scene-start authoring.)
- **Open with `wait_for`, not `hold`.** A leading `wait_for` also skips the recorder's default `initial_hold_ms` + `goto_settle_ms` blind pauses. (Step 4.)
- **For long waits use `wait_for seconds: N`, not `hold seconds: N`.** `wait_for` exits the instant the success text appears; `hold` burns the full duration even if the job finished in 5 seconds. (Step 4.)
- **Wait only on TERMINAL states — never on transient/flickering text.** A `wait_for` on intermediate text like `"Creating 10 plan…"` races the resolver and reports as a false-positive failure ~50% of the time. (See "Don't wait on transient state".)
- **Prefer prefix syntax — `testid:foo` / `role:button:Foo` / `aria:Foo` — over fragile `:nth-of-type` CSS chains.** Bare strings work for the common case; prefixes make the resolution explicit and refactor-resistant. (See "Target resolution syntax".)
- **Use `must_succeed: true` on actions whose failure makes later scenes gibberish.** The form-submit that creates the entity later scenes operate on; the navigation that lands on the page later scenes screenshot. (See "`must_succeed: true` for critical actions".)
- **Add `timeout_ms` to the state-minting clicks that navigate (publish / submit / award).** Playwright waits for the click's scheduled navigation inside the same timeout, so a slow server-side POST aborts a healthy render at the global 6s default. `timeout_ms: 30000` on that one action loosens it (`max(override, preset)` — it can never tighten); leave ordinary clicks on the default so broken targets still fail fast.
- **When a single scene needs more room than the rest, set `viewport: {width, height}` on it.** Most specs render at one viewport. A dense plan-review scene can override per-scene without inflating the whole recording. (See "Per-scene `viewport:` override".)
- **Map / WebGL scenes capture fine — give the map a `hold` to paint, and set `full_page: false` on map+table pages.** Headless WebGL (Mapbox/deck.gl/three.js) renders via SwiftShader out of the box; a "blank map" is almost always too short a settle (tiles still loading) or a full-page capture of a tall table+map page, not a WebGL failure. (See "Map / WebGL scenes".)
- **Narrative-only back-half scenes are fine** — the recorder skips them with `--skip-empty-scenes`. Deck slides still cover them; the mp4 doesn't waste `min_hold_ms` on identical static pages.
- **A scene that omits `url:` continues on whatever page the previous scene's actions navigated to** (continue-scene pattern). This is how a narrative can CREATE an entity in one scene and operate on it in later scenes whose URL can't be known ahead of time.
- **Synthetic-data demos declare a `setup:` block and reference minted IDs as `${var}` — never hardcode them.** The synthetic generator runs before every render (`rerun: per_render`, the default — required when the demo itself mutates state) and the recorder substitutes `${run_id}` etc. from its outputs JSON into scene `url`s and action `target`/`value` fields. A hardcoded ID silently goes stale on every reseed; a `${...}` placeholder without `setup.outputs` is rejected by spec-qa. **To mint an id ON CAMERA** (create an entity mid-demo, then use its real id in later scenes — a fresh lifecycle each render), use a `capture` action instead of a setup output. (See "Data setup contract" and "Capture + late binding" in Step 5.)

## Procedure

### Step 1 — Read why_brief.yaml

```bash
cat <why_brief_path>
```

Parse the why_brief.  Note:
- `narrative_slug` — becomes the spec `name` and filename slug.
- `problem` — seeds the spec `narrative`.
- `spine` — each `SpineItem` becomes one or more scenes; the item's `id` becomes
  the scene `provenance`.
- `gaps` — surface any DECISION gaps to the user before proceeding (they may
  affect design_intent choices).

### Step 2 — Cast the personas (define them explicitly, first)

Personas are a **first-class, explicit** part of the spec — define them BEFORE the
narrative, because the narrative will name a persona in every beat and the scenes
inherit their persona from that beat.  Get the cast right here and the rest falls
out.

**Personas are PEOPLE only — never the tool, system, product, or feature.** The thing
being demoed is what the personas *use*; it is never itself a character. "DDD",
"the pipeline", "the API", "the agent" are NOT personas — the human who drives them
is. Many features have exactly **one** persona (the single user) until there is a
genuine second *human* role; don't invent a system-persona to manufacture a handoff.

From the feature context, cast 1–3 personas — the *characters* in the demo.  Each
persona must have:
- `name` — the character's name. Use a real first name for an individual ("Maya"),
  or the organization's name when the actor *is* an org acting as one role
  ("Dimagi", "LLO"). Pick one convention and hold it across the cast.
- `role` — the actor's role in the workflow (e.g. "Program Manager", "Local partner").
- `color` — a hex color that will appear in the walkthrough UI (e.g. `"#3B82F6"`).
- `intro` — one sentence describing who this persona is and their goal.

Every scene's `persona` field must be a key that exists in this `personas` dict.

When persona identity matters to the user (named orgs, specific roles), confirm the
exact names with them — personas are durable and reused across runs, and they are
not editable on the review surface after the fact.

**If you cast more than one persona, they must hand off to each other** across the
demo — the story moves from one character to the next as the workflow crosses a
role boundary (e.g. a Program Lead designs the plan → a Field Partner runs it →
the Lead reviews the result).  A multi-persona spec where each persona owns an
isolated, unconnected block is a red flag: that's a feature catalog, not a demo.
Look at `baobab-demo.yaml` (Sarah posts → Amina responds → James reviews → Sarah
reports) for the handoff pattern.

### Step 3 — Write the cohesive demo narrative FIRST

**This is the most important step. Do it before drafting any scenes.**

Write the `narrative` — the single, continuous story the demo tells, the thing a
viewer would watch top to bottom and follow as one arc.  It is *not* a 1-line
tagline and *not* a list of capabilities.  It is a short paragraph (3–6 sentences)
that:

- Names the personas and follows them **in sequence** through the workflow, handing
  off where roles change.
- Has a beginning (the situation / the problem the user walks in with), a middle
  (what they do), and a payoff (what they walk away with).
- Reads as one journey, not "the product can do A; it can also do B; it also does C."
- Is grounded in the why_brief `problem` + the logical arc of the `spine` — but
  told as a *story*, not as a spine summary.

Test it: read the narrative aloud. If it sounds like a demo you'd be proud to
record, continue. If it sounds like a feature list ("First, area selection. Then,
plan generation. Then, monitoring."), rewrite it as a story before going on.

This narrative is rendered at the top of the review surface, so the reviewer reads
the whole arc before the per-scene breakdown.  It is the thing they approve or
send back.

**Narrative voice — write it as the persona, not as a UI tour.** The narrative
(and every scene's `narrative` beat) is a story about a *named person doing
things*, not a description of the product. This is the single biggest quality
lever; apply all four every time:

- **The persona is the named subject of (almost) every sentence.** "David drops to
  the map… David clicks the flagged surveyor's GPS…" — NOT "The page shows a map…
  A panel opens…". Lead beats with the persona's name, not "he/she/they" and not
  the UI. Test: delete the persona's name; if the sentence still reads as a UI
  description, rewrite it so the persona is doing the acting.
- **Ground the persona in the real user — role + the live question they walked in
  with.** Not a generic stand-in ("a funder"); the actual user of *this* product
  with the actual decision they're trying to make ("David manages a Connect
  program and wants to know whether his visits are changing anything"). Pull it
  from the why_brief `problem` + persona, made specific.
- **Active and first-order: the persona does, reads, notices, decides.** The verbs
  belong to the persona ("reads the distance between the two numbers", "checks the
  people who collected the data"), not to the UI ("the trend displays", "the table
  renders").
- **Show-don't-tell — end on the persona drawing their own conclusion.** Don't
  state the takeaway; let the persona reach it ("…and decided for himself whether
  his program is working"). No interpretive verbs telling the viewer what to think.

Before → after, same moment and same UI:
- ✗ UI tour: "The dashboard leads with a six-cycle trend; the self-reported line
  sits above the independent survey every cycle."
- ✓ Persona story: "David starts with the six-cycle trend, lining up what his
  program reported against what the independent survey found and reading the
  distance between them."

**Write it so the scenes fall out of the literal text.** The narrative is the
single source of truth; the scene list is a *mechanical decomposition of it*, not
a separate authoring pass. So:

- Write the narrative as an **ordered sequence of beats — one sentence (or tight
  pair of sentences) per beat**, in the order they happen on screen.
- **Each beat names the persona acting in it.** ("Dimagi opens the map…", "the LLO
  cleans it…") — that named persona becomes the scene's `persona`.
- The **number of beats equals the number of scenes**, in the same order. A reader
  should be able to split the narrative into sentences and recover the scene list.
- Keep the language concrete enough that each sentence already describes an
  observable moment — because in Step 4 that sentence *becomes* the scene's claim.

If a sentence is too vague to become a scene, sharpen it here in the narrative
rather than inventing scene detail later. The narrative and the scenes must never
drift apart.

### Step 4 — Decompose the narrative into story-beat scenes

Now split the narrative into its beats — **one scene per narrative sentence/beat,
in order**. This is a decomposition of the text you already wrote, not new
invention. Scenes are numbered from **1** (the first beat the viewer sees), and
scene N+1 must follow from scene N: a viewer watching 1 → 2 → … → last should feel
one continuous demo.

For each scene, take it **directly from its narrative sentence**:
- `persona` ← the persona that sentence named.
- `narrative` ← that sentence kept **in the persona's voice** — the story beat the
  reviewer reads (see "Two fields per scene" below). Write it for EVERY scene.
- `concept_claim` ← that sentence, tightened into one falsifiable assertion (see below).
- `title` ← a short story-beat label for that same moment.
- `show` ← the concrete browser actions that play out that moment.

**Two fields per scene, two jobs — don't conflate them:**

- **`narrative`** — the persona-voiced **story beat the reviewer reads** on the
  narrative-review surface. **Write it explicitly for every scene.** If you leave
  it empty, the surface falls back to the terse, third-person `concept_claim`,
  which reads as an abstract UI claim, not a story — the most common narrative
  miss. Apply the four "Narrative voice" rules from Step 3 to each beat (named
  persona as subject, grounded, active, show-don't-tell).
- **`concept_claim`** — the tightened, **falsifiable assertion the judge tests**.
  Third-person and terse is correct here; it is the build/eval contract, not the
  human-facing story.

Same moment, two registers:
- `narrative`: "David clicks the flagged surveyor's GPS and reads, survey by
  survey, how far each capture sat from the household it claimed."
- `concept_claim`: "clicking a quality cell shows the full per-record census for
  that surveyor, failing rows first."

Because the scenes are a literal decomposition, the scene count equals the beat
count and their order matches the narrative order. If you find yourself adding a
scene with no home in the narrative, or a narrative beat with no scene, fix the
narrative first (Step 3) so the two stay in lockstep.

**Scene `title` — the story beat (CRITICAL):**
- The title is a **moment in the demo the viewer watches**, phrased as a story beat
  (e.g. "Maya turns a district into a draft plan", "Now the harder case — the lake
  that isn't a settlement", "Sam hands the cleaned plan back").
- It is NOT a capability/feature name ("Area selection", "Plan generation",
  "Monitoring dashboard") and NOT a design-doc status annotation.  **Status tags
  like `(frontier)`, `(gap)`, `(the hero)`, `(built)`, `(WIP)` in the title are
  rejected by spec-qa** — build status lives in the why_brief spine + feature
  provenance, never in the story title.

**Canopy walkthrough keys (required by the walkthrough engine):**
- `persona` — must be a key in the `personas` dict (who is on screen this beat).
- `title` — the story-beat title (see above).
- `show` — concrete, imperative browser actions the walkthrough will execute
  (e.g. `"navigate to /audit/new, fill the 'observation' field, click Submit"`).
- `url` (strongly recommended) — the **canonical scene entry point**. The recorder
  auto-navigates to this URL at the top of the scene; you do NOT also write a leading
  `goto` action for the same path. A scene may omit `url:` to **continue on whatever
  page the previous scene's actions navigated to** — this is how a narrative can
  CREATE an entity in one scene (e.g. click "Create plan" → app routes to the new
  record) and operate on it in later scenes whose URL can't be known ahead of time.
  Give scene 1 a `url:`; leave it empty on continue-scenes.
- `actions` (optional but strongly recommended) — the **machine-executable** form
  of `show`: a list of cursor interactions the video recorder performs so the demo
  shows the feature being *used*, not just a page being panned. A scene with no
  `actions` records as a static scroll — which scores ~1/5 on "demonstrates using
  the features." Each action is `{kind, target?, value?, seconds?, note?}` where
  `kind` ∈ {goto, click, click_menu, fill, select, type, press, hover, scroll_to,
  scroll, wait_for, hold, draw, capture} and `target` is visible text or a CSS
  selector. For **`kind: capture`** (mint a `${var}` ON CAMERA so a fresh entity
  created mid-demo flows into later scenes — see "Capture + late binding" below),
  read an id off the live page into a variable later scenes resolve. For
  `kind: select` (native `<select>` controls — which `click` can't reliably open
  across platforms), `value` is the option's `value` attribute, OR a digit-only
  string interpreted as the 0-based `index`, OR the option's visible label —
  the recorder tries each in order. For **`kind: draw`** (drawing a polygon on a
  map/canvas — Mapbox GL Draw, Leaflet.draw, MapLibre, custom React-Konva /
  SVG drawing surfaces — which no labelled-element click can express),
  `target` is the map / canvas element and `points` is a list of `[fx, fy]`
  fractions (0-1) within its box; the cursor clicks each vertex then
  double-clicks to close. Set `tool` to the draw-tool button (e.g.
  `css:.mapbox-gl-draw_polygon`) and `draw` activates it first with a
  **coordinate mouse-click instead of `Locator.click()`**. Most canvas
  drawing libraries render their tool palette as tiny absolutely-positioned
  buttons that Playwright's actionability check rejects (the
  receiving-events probe times out on a 24×24 control that overlaps the
  canvas). Coordinate-clicking bypasses the check — we know the button is
  there because the spec author named it. Pattern applies to Mapbox GL
  Draw, Leaflet.draw, MapLibre, and any custom canvas / SVG drawing
  surface whose tool palette is built from small overlay buttons, e.g.
  ```yaml
  - { kind: draw, target: "css:#map", tool: "css:.mapbox-gl-draw_polygon", points: [[0.35,0.4],[0.6,0.4],[0.6,0.7],[0.35,0.7]] }
  - { kind: draw, target: "css:#leaflet-map", tool: "css:.leaflet-draw-draw-polygon", points: [[0.2,0.2],[0.8,0.2],[0.8,0.8],[0.2,0.8]] }
  - { kind: draw, target: "css:#custom-canvas", tool: "testid:rectangle-tool", points: [[0.1,0.1],[0.9,0.9]] }
  ```
  Write `actions` as the literal click-path
  that realizes `show`:
  ```yaml
      persona: maya
      title: "Maya tunes the plan"
      url: "/microplans/program/133/setup/"
      show: "exclude an invalid work area and watch the per-worker metrics update"
      actions:
        - { kind: wait_for, target: "PLAN METRICS" }
        - { kind: scroll_to, target: "Exclude" }
        - { kind: click, target: "Exclude", note: "drop an invalid area" }
        - { kind: wait_for, target: "Excluded 1" }
        - { kind: hold, seconds: 1.5 }
  ```
  Prefer real state-changing clicks (exclude → metrics move, submit → status flips)
  over hovers — a visible state change is what earns `feature_use` 5. The recorder
  (`scripts/walkthrough/_lib/recorder.py`) skips any action it can't resolve, so a
  stale target degrades that one step, never the whole render.

  **Anti-pattern — don't write both `url:` AND a leading `goto target: <same-url>`.**
  The recorder navigates from `scene.url` automatically at the top of every scene.
  A duplicate leading `goto` to the same path causes a visible page reload ~1-2s
  into every scene (the load you already paid for happens AGAIN). Pick one: `url:`
  is the declarative entry point — a `goto` action is only for navigating
  **mid-scene** to a DIFFERENT page. The recorder's `build_scenes_from_spec` strips
  redundant leading gotos as a safety net (canopy 0.2.151+), but the spec should
  still read as authored — don't rely on the strip.

  **Open with `wait_for` instead of an `initial_hold`.** When the scene starts on a
  page that takes a moment to render (any labs page that talks to the API), the
  first action should be `{kind: wait_for, target: <visible text or selector>}` —
  NOT a `hold seconds: 2.0` "let the page settle." Three reasons:
  - `wait_for` exits the instant the page is ready; `hold` always burns the full
    duration even if the page rendered in 100ms.
  - When the recorder sees a leading `wait_for` it also skips the default
    `initial_hold_ms` (800-2500ms) and `goto_settle_ms` (600-2000ms) blind pauses
    — the `wait_for` IS the settle, so the holds become pure dead air on top
    (canopy 0.2.151+).
  - Long bulk operations (a 30-90s background import) use the `seconds:` override
    on `wait_for`, not a `hold seconds: 90`: `{kind: wait_for, target: "Created 10
    of 10 plans", seconds: 120}`. Exits the instant the success text appears;
    never wastes a frame.

  **Quick reference for scene-start authoring:**
  ```yaml
  # ✓ good — declarative url + wait_for as settle
  - persona: maya
    title: "Maya opens the workspace"
    url: "/microplans/program/133/"
    show: "..."
    actions:
      - { kind: wait_for, target: "Microplan portfolio" }
      - { kind: scroll_to, target: "+ Bulk paste list" }

  # ✗ bad — duplicate goto + blind hold (the old pattern this PR retires)
  - persona: maya
    title: "Maya opens the workspace"
    show: "..."
    actions:
      - { kind: goto, target: "/microplans/program/133/" }
      - { kind: wait_for, target: "Microplan portfolio" }
      - { kind: hold, seconds: 2.0, note: "frame the empty portfolio" }
  ```

  **Target resolution syntax — prefer prefixes over bare CSS.** Every action's
  `target` field can use a prefix to control how the recorder resolves it.
  Bare strings use a heuristic (CSS-shaped → selector engine; English → visible-text
  ranking via Playwright's `get_by_role` / `get_by_text`), which is fine for most
  cases. When the heuristic gets it wrong, or you want to be explicit, use a
  prefix:

  | Prefix | Routes to | When to use |
  | --- | --- | --- |
  | `css:#cfg-strategy` | `page.locator(...)` | Explicit CSS selector. Use when bare target gets mis-heuristic'd. |
  | `testid:plan-picker` | `page.get_by_test_id(...)` | When the page exposes `data-testid` — the most refactor-resistant target type. |
  | `aria:Resolved wards` | `page.get_by_label(...)` (accessible-name semantics, NOT raw `aria-label`) | Picks up `aria-label`, `aria-labelledby`, `<label for>`, `<label>` wrapping. |
  | `role:button` or `role:button:Sign in` | `page.get_by_role(...)` (optional `name=...`, `exact=True`) | The role + name pattern is Playwright's recommended PRIMARY selector. |
  | `text:Resolved wards` | `page.get_by_text(...)` | Forces visible-text path. Use when the text starts with `#` / `.` etc. and would otherwise be heuristic-routed as a selector. |

  ```yaml
  actions:
    - { kind: click, target: "testid:bulk-paste-cta" }
    - { kind: click, target: "role:button:Sign in" }
    - { kind: wait_for, target: "Resolved wards" }   # bare text — heuristic routes correctly
  ```

  Anti-pattern call-out: don't write a fragile `:nth-of-type` CSS path when a
  `testid:` would survive a sidebar refactor. Same point applies for
  `nth-child` chains pointing at unstable structural positions — those targets
  silently break the day someone reorders a list, and the failure mode is
  "this action was skipped" buried in the run report, not a loud failure.

  **`must_succeed: true` for critical actions.** Default behavior: a failed
  action prints a warning and the recording continues — one bad step never
  aborts the render. This is right for the common case, but it hides cascade
  failures: if scene 2's "Create" button click silently misses, every later
  scene records against the wrong page state and the report says "60/61
  actions ok" while the whole demo is wrong.

  Opt in with `must_succeed: true` on actions whose failure makes the rest of
  the scene gibberish. Common candidates: the form-submit click that creates
  the entity later scenes operate on; the navigation that lands on the page
  later scenes screenshot.

  The recorder raises `ActionAssertError` instead of swallowing — the scene
  aborts loudly and the report flags it.

  ```yaml
  actions:
    - { kind: fill, target: "#ward-list", value: "Galinja\nMadobi" }
    - { kind: click, target: "Create 10 plans", must_succeed: true }
    - { kind: wait_for, target: "Created 10 of 10 plans", seconds: 120, must_succeed: true }
  ```

  When NOT to use it: `scroll_to`, `hold`, `hover` — these are pacing/framing
  actions whose failure doesn't change product state. A skipped `scroll_to`
  costs a smoother camera pan, not a wrong demo.

  **`click_menu` for the click that closes a dropdown.** `click_menu` clicks
  an item inside the currently-open dropdown / popover. Same target
  resolution as `click`, but the recorder uses a shorter
  `menu_click_settle_ms` (~700ms) because menus react faster than top-level
  buttons. Use it as the SECOND click in a two-click "open menu → pick
  item" sequence — the spec verb signals intent ("this click closes a menu,
  treat it as such") and gets the right pacing. The verb is also a hint
  for graders reading the spec: this beat shows a menu interaction, not
  two independent buttons.

  ```yaml
  # open the menu, then pick an item inside it
  - { kind: click, target: "Sort by" }              # opens the menu
  - { kind: click_menu, target: "Date created" }    # picks the item; menu closes
  ```

  When NOT to use it: don't use `click_menu` for the click that OPENS the
  menu — that's a regular `click`. The verb is for the click that
  closes/dismisses the menu, not the one that summons it.

  **`say:` names the field for the voiceover.** In a narrated render, every
  action that puts a field on camera becomes an action↔word mark, and the
  renderer time-warps the footage so the field lands on the word that names it.
  Candidate words come from `say:` first, then tokens in the field id, then
  tokens in `note:`.

  Set `say:` on every field the scene's `narrative:` actually names, and keep the
  narration's word order identical to the action order:

  ```yaml
  narrative: >-
    She sets the application deadline, the expected start and end dates, the
    estimated scale, and her contact email.
  actions:
    - { kind: fill, target: 'css:#id_application_deadline', value: '2026-09-30', say: deadline }
    - { kind: fill, target: 'css:#id_expected_start_date',  value: '2026-10-15', say: start }
    - { kind: fill, target: 'css:#id_expected_end_date',    value: '2026-12-31', say: end }
    - { kind: fill, target: 'css:#id_estimated_scale',      value: '~12,000 households', say: scale }
    - { kind: fill, target: 'css:#id_contact_email',        value: 'maya@…', say: email }
  ```

  Without `say:`, marks fall back to id and note tokens — and those collide. Two
  actions whose notes both contain "application" compete for one spoken word and
  the loser is dropped, which the timing eval reports as "narration names them in
  a different ORDER than the form lays them out" even when the order is right.
  One word per field, appearing exactly once in the narration, in action order.

  **`note:` is a persistent annotation, not a YAML comment.** `note:` is a
  human-readable annotation for one action. Unlike a YAML comment (`#`),
  it persists into the artifact: shown in the recorder's per-action log,
  included in the `--report` JSON, and read by judges as inline context
  for what the step demonstrates. Treat it as documentation that ships —
  not as a scratchpad comment.

  ```yaml
  # ✓ good — explains the WHY for future readers + the judges
  - { kind: click, target: "Create 10 plans", note: "fire the bulk POST for all 10 wards" }
  - { kind: select, target: "#lga-kanwa", value: "1", note: "pick Madobi LGA for Kanwa (Madobi is candidate 1, Zurmi is candidate 0)" }

  # ✗ unhelpful — explains nothing the action itself wouldn't tell you
  - { kind: click, target: "Submit", note: "click submit" }
  ```

  When to write a note: when the action's intent isn't obvious from the
  verb + target alone — disambiguation choices (which of two near-identical
  options was picked and why), ordering rationale (this click HAS to come
  before that wait), what the click triggers downstream (the bulk POST,
  the worker re-shuffle, the page redirect). A scratchpad-grade note ("click
  submit") is worse than no note — it adds noise without adding signal.

  **Don't `wait_for` on a transient intermediate state.** When a button's
  label flickers through `"Creating N plans…"` → `"Created N of N plans"`
  faster than the resolver can poll, a `wait_for` on the intermediate text
  races (~50% miss rate) and the run report's "failed" column fills with
  false positives. Worse, the spec author can't tell whether the failure
  means "something broke" or "this transient text was too fast."

  Pattern: wait only on TERMINAL states — the success card that doesn't
  replace itself, the toast that stays for 5 seconds, the page heading that
  sticks. Use `seconds:` to extend the timeout for long-running terminal
  waits.

  ```yaml
  # ✗ Anti-pattern — races on the intermediate flicker
  - { kind: click, target: "Create 10 plans" }
  - { kind: wait_for, target: "Creating 10 plan" }   # ← races, often "fails"
  - { kind: wait_for, target: "Created 10 of 10 plans" }

  # ✓ Pattern — wait only on the terminal success state
  - { kind: click, target: "Create 10 plans" }
  - { kind: wait_for, target: "Created 10 of 10 plans", seconds: 120 }
  ```

  Durable example: the connect-labs `microplans-10-wards` spec wrote a
  `wait_for` on `"Creating 10 plan"` (the spinner text). The label rendered
  for ~200ms before the success card replaced it; the run report logged it
  as a failure on roughly half the records, and the spec author kept asking
  "did the bulk create work or not?" because the recorder couldn't tell them.
  Dropping the intermediate wait and keeping only the terminal `"Created 10 of
  10 plans"` wait fixed both the false-positive rate and the diagnostic
  signal.

  **Never `wait_for` an element inside a collapsed/hidden container.** A
  selector `wait_for` waits for *visibility*, and an `<option>` inside a
  closed `<select>` (or any item inside a closed dropdown / `display:none`
  panel) is never visible — the wait burns its FULL timeout as a frozen
  frame on film and then "fails", even though the element exists and the
  data loaded long ago. Wait on the visible container via `:has()` instead:
  `css:select#run-picker:has(option[value='${run_id}'])`, never
  `css:select#run-picker option[value='${run_id}']`. (Cost on
  program-admin-report: a 20s frozen frame per take.)

  **Recording time & dead space — read the one authoritative section.** What
  films vs what doesn't (the spec's `setup:` command and the `prewarm: true`
  pass run OFF camera; every wait, hold, glide, and settle in `actions` is ON
  camera), every dead-space mechanism (prewarm for cold caches,
  `--skip-empty-scenes`, `--skip-same-url`, the automatic leading-`wait_for`
  hold skips, crossfade), and the dwell-knob hierarchy (`hold` actions >
  legacy `video_hold_seconds` > `final_hold_ms`) are documented ONCE in the
  **walkthrough SKILL § "Recording time & dead space"** — go there before
  tuning any timing. Spec-author takeaway: set `prewarm: true` on specs whose
  app has cold-start cost (first-hit page renders, remote image fetches), and
  express deliberate dwells as `hold` actions, not pacing-knob tweaks.

  **Per-scene viewport override.** Most specs render at one viewport (the
  spec-level `video_viewport_width` / `video_viewport_height`, defaults
  1280×720). When one scene needs a wider canvas (a dense plan-review page
  with a map + side metrics + a table that wraps awkwardly at 1280), set
  `viewport: {width, height}` on that scene only:

  ```yaml
  scenes:
    - title: "Dana drills into the plan map"
      url: "/microplans/program/133/plan/3536/review/"
      viewport: { width: 1440, height: 900 }
      actions: ...
  ```

  The recorder calls `page.set_viewport_size()` before the scene's goto and
  restores the spec-level default after `final_hold_ms`. Important
  constraint: **the recorded mp4 frame size stays fixed at the spec-level
  resolution** — Playwright's `record_video_size` is set at context creation
  and cannot change mid-stream. Per-scene viewport changes the page LAYOUT
  (CSS pixels) only; the wider logical viewport is letterboxed into the
  fixed mp4 frame. This is genuinely useful — the layout breathes — but
  don't expect the dense scene to be sharper in the video. For per-scene
  resolution, use multiple render passes + ffmpeg concat (out of scope for
  normal spec authoring).

  **Map / WebGL scenes (Mapbox, deck.gl, three.js, canvas) — they DO capture.**
  Headless rendering of WebGL maps works out of the box: the recorder launches
  Chromium with SwiftShader (`--enable-unsafe-swiftshader --use-angle=swiftshader
  --ignore-gpu-blocklist`), Chromium's CPU GL backend, so a Mapbox/WebGL canvas
  renders and composites into both the snapshot and the video. There is no special
  capture path or second browser needed. Two authoring rules make map scenes land:

  - **Give the map time to paint.** Tiles (especially high-zoom satellite) load over
    the network after the page is "ready", so open the scene with a `wait_for` on a
    stable on-map element (a legend row, an arm label, `View-only`) and then a
    `hold` of **~6–12s** before the snapshot — the snapshot fires at the scene's
    steady state, so the hold is the paint budget. Too short → a half-painted or
    blank-looking canvas. This is the #1 cause of a "blank map"; it is a *timing*
    problem, not a WebGL one.
  - **For a page that is a map + a long table (e.g. the plan-review page), set
    `full_page: false`** so the snapshot is just the viewport (the map is the hero).
    Full-page captures handle sticky headers automatically (scroll-to-top +
    restore at capture time, post-final-hold) — do NOT reach for
    `full_page: false` just because a page has a sticky navbar.
    Default full-page capture of such a page yields a 16,000px strip where the map
    is a ~4% sliver up top — it *looks* blank but the map rendered fine; it's just
    dwarfed. A full-viewport map page (e.g. a group overlay map) captures correctly
    either way, so `full_page` only matters for table-dominant pages.

  ```yaml
  - title: "Maya sees both arms on one map"
    url: "/microplans/program/133/group/3635/map/"
    full_page: false            # viewport capture — map is the hero, not a tall strip
    actions:
      - { kind: wait_for, target: "View-only" }
      - { kind: wait_for, target: "intervention" }   # an on-map legend label
      - { kind: hold, seconds: 10 }                   # let tiles paint before the snapshot
  ```

  To interact with the map (draw an area, click a feature), use the `draw` action
  (coordinate clicks on the canvas) documented in Step 4 — a labelled-element click
  can't address a point on a WebGL canvas.

**Narrative voice — the persona stays the subject (CRITICAL):**

The whole point of DDD narration is that a *person* uses the product. Across the
top `narrative`, every scene `title`, every `concept_claim`, and the prose of every
`show`, the **persona is the grammatical subject and the words are theirs** — what
they do, see, decide, and want. Write "Maya keeps Google and clicks Generate," not
"the panel lists the providers and a source is selected."

This is the structure that quietly erodes on rewrites. A scene edit triggered by a
shipped feature drifts into **system-as-subject** and **implementation jargon** —
"a comparability panel appears", "the system returns the counts", "the providers
Overture conflates", "PSU", "FeatureCollection". Each of those drops the person out
of the story and turns a demo into a tour of panels. Hold the line:

- **The persona acts; the UI is what they act on.** Subject = the persona, never "a
  panel", "the system", "the endpoint". ✓ "Maya opens the comparability panel" — ✗ "a
  comparability panel appears".
- **Use the persona's words, not the codebase's.** If the persona wouldn't say
  "Overture conflates", "PSU", or "FeatureCollection", keep it out of
  `show`/`concept_claim`. Engineering vocabulary lives in `features[].description` /
  `verify`, never in the story prose.
- **`show` is the persona's narrated moment; `actions` is the click-path.** Keep
  `show` readable as the person doing the thing; put literal selectors/clicks in
  `actions`.

Before / after — same beat, with and without the person:

```
✗ "The sampling panel lists the providers Overture conflates; after generating,
   each shows its building count."
✓ "Maya weighs her three building sources — Google, OpenStreetMap, Microsoft —
   keeps Google, the one her statistician expects, and after she generates sees how
   many buildings each found."
```

A spine item may span several beats, and a single beat may touch more than one
spine item — decompose by the *story*, then attach `provenance` to whichever spine
item the beat demonstrates.  Every spine item must be covered by at least one beat.

**DDD-specific keys (required by ddd-spec-qa gate):**
- `concept_claim` — one assertive sentence describing what the product does in this
  scene AND why it matters.  This claim must be:
  - **Non-empty** — never leave it blank or whitespace.
  - **Falsifiable** — a skeptical observer must be able to confirm or refute it by
    watching the walkthrough.  Do NOT use: "world-class", "seamless", "powerful",
    "robust", "best-in-class", "cutting-edge", or similar marketing language.
    DO write: a specific action and its observable result, optionally with a
    measurable outcome (e.g. "within 2 seconds", "without leaving the page").
  - **At least 5 words** — a claim shorter than 5 words is too vague to be testable.
    Subtle vacuousness (e.g. articulate-but-empty fluff) is caught later by the LLM
    concept judge (/ddd-concept-eval).
- `provenance` — the `SpineItem.id` this scene demonstrates (e.g. `"S1"`).  Must
  match an existing spine id in the linked why_brief.
- `design_intent` (optional but strongly recommended) — the design decision or
  hypothesis under test in this scene.  What are we betting on?
- **`features` (required by ddd-spec-qa — ≥1 per scene)** — a list of concrete
  buildable units.  Each feature must have:
  - `id` — a short unique slug (e.g. `"boundary-draw"`).
  - `description` — what to implement, in one sentence.  Must be concrete enough
    that an engineer can open a ticket from it.
  - `verify` — a **runnable validation** (≥3 words) — a real API assertion, UI
    state check, or test command.  Vague phrases like "check it works" will fail
    the `ddd-narrative-actionability-eval`.

**Good features vs vague features:**

```yaml
# GOOD — concrete buildable unit with a runnable verify
features:
  - id: task-filter-ui
    description: Status dropdown filter on the task list page (/tasks)
    verify: "Playwright: select 'Open' in Status dropdown, assert only open tasks visible"
  - id: task-filter-api
    description: "GET /tasks?status= filters tasks server-side by status enum"
    verify: "pytest: GET /tasks?status=open returns 200 with all tasks having status=open"

# BAD — vague, not runnable
features:
  - id: filtering
    description: Add filtering support
    verify: test it  # too short, not a real assertion
```

**`ddd-spec-qa` now requires ≥1 verifiable feature per scene.**  A scene with
`features: []` (or no `features` key) will fail spec-qa and block the gate.
The `ddd-narrative-actionability-eval` will additionally score whether the
narration (concept_claim + show) implies those features to a cold reader.

**Examples of falsifiable concept_claims:**
- "When a supervisor submits the audit form, the FLW receives a coaching task within 60 seconds"
- "Users can filter the task list by status and see only open tasks without a page reload"
- "The sampling engine selects buildings proportional to floor count and shows the sample on a map"

**Examples of non-falsifiable concept_claims (will fail ddd-spec-qa):**
- "A world-class seamless experience for field workers" — banned phrases (world-class, seamless)
- "Robust performance" — banned phrase, too short to be testable
- "Powerful filtering" — banned phrase

### Step 4b — Refreshing an existing narrative (the build changed)

Most narratives are not written once — they are **refreshed** when a feature ships,
a flow is reordered, or a beat goes stale. This is exactly where the person-based
structure decays: a refresh tempts you to reach into the affected scene and patch
its `show`/`concept_claim` to describe the new UI, and after a few such edits the
clean story has become a feature list. Do not patch scenes. Re-derive them:

1. **Edit the top `narrative` beat first** (Step 3). Fold the change into the one
   continuous story so it still reads as the persona's arc — same voice, persona as
   subject. If the change adds or moves a beat, add/move the *sentence* here first.
2. **Re-derive the affected scene from that beat** (Step 4): persona, title,
   concept_claim, show — re-written in the persona's voice, not edited toward a UI
   description. Keep `beats == scenes` and their order in lockstep with the narrative.
3. **Re-read the whole refreshed `narrative` aloud** (Step 3's test). If it now
   sounds like a tour of panels and endpoints rather than one person getting
   something done, you patched scenes instead of re-deriving them — go back to 1.

The "Narrative voice" rule in Step 4 applies in full to every refresh. A rewrite
that drops the persona as subject, or imports codebase jargon ("PSU", "the panel
appears", "Overture conflates") into `show`/`concept_claim`, is a regression even if
every gate still passes.

**Close the gaps the work resolved — in the why-brief, not the scene.** A beat
reads `new` for as long as any why-brief gap references it by `claim_ref`. So a
refresh that follows a build has to say so *there*, by setting `resolved` on the
gap to a short statement of what closed it:

```yaml
gaps:
  - id: G2
    type: CAPABILITY
    claim_ref: S1
    detail: There is no distribution calendar and no partner surface to render it on.
    proposed_action: Add a DistributionPlan and a partner surface that renders it.
    resolved: Shipped in models/demand.py + tab_partner.jsx (PR #1014).
```

The two obvious shortcuts both fail. **Deleting the gap** flips the badge but
throws away the record of why the work happened — the gap is the only place
that reasoning lives. **Editing the posted narration** on canopy-web does not
survive: the next `narrative post` recomputes every scene's status from the
why-brief and overwrites it.

Closing a gap says *the capability landed*. It does not ground the spine item —
that is a separate claim about evidence, and `spine[].status` carries it. Set
both when both are true.

### Step 5 — Assemble and write the spec file

Assemble the full spec: the cohesive `narrative` from Step 3 at the top, the
personas from Step 2, and the ordered story-beat scenes from Step 4.

```yaml
name: <feature slug>
narrative: >-
  <The cohesive demo narrative from Step 3 — a 3–6 sentence story that follows
  the personas through the workflow as one continuous arc. NOT a tagline, NOT a
  capability list.>
base_url: <live environment URL, e.g. https://labs.connect.dimagi.com>
auth:
  type: session   # or omit if the walkthrough handles auth via browser cookies
why_brief: why_brief.yaml   # relative path from the spec file to the why_brief
review_mode: autonomous       # optional — autonomous (default) | human. In human mode the
                              # orchestrator posts PRODUCT judge findings to the canopy-web
                              # product_findings review gate (one link, per-finding deck
                              # #scene-N + video #t=<seconds> evidence) and waits for the
                              # user's implement/skip/defer picks instead of auto-applying.
setup:                        # optional — only for demos backed by synthetic data
  command: "python scripts/walkthroughs/<demo>/regenerate.py"   # the synthetic generator
  outputs: "scripts/walkthroughs/<demo>/outputs.json"           # flat JSON {var: string|number}
  rerun: per_render           # per_render (default) | once
  timeout_seconds: 1200
personas:
  <persona_key>:
    name: ...
    role: ...
    color: ...
    intro: ...
scenes:                       # ordered story beats, numbered from 1 by position
  - persona: ...              # who is on screen this beat
    title: ...                # the story beat (a moment), NOT a capability name
    narrative: >-             # the persona-voiced beat the REVIEWER reads (write it!)
      <Named persona as subject, active voice, grounded, show-don't-tell.>
    show: ...
    concept_claim: ...        # tightened, falsifiable assertion the JUDGE tests
    provenance: ...
    design_intent: ...
    features:
      - id: <slug>
        description: <concrete buildable unit — what to implement>
        verify: <runnable validation — API assertion, UI state check, or test command>
```

Write the draft to `docs/walkthroughs/<narrative-slug>.yaml` (create the directory if it
doesn't exist).  The output file path is `<run_dir>/<narrative-slug>.yaml`.

**Data setup contract (`setup:` + `${var}`) — for demos backed by synthetic data:**

A spec must never assume the world is already in a recordable state. If the demo
runs on generated data, declare the **synthetic generator** in the `setup:` block
and reference every ID it mints as a `${var}` placeholder — in scene `url`s and in
action `target`/`value` fields:

```yaml
setup:
  command: "python scripts/walkthroughs/par/regenerate.py"
  outputs: "scripts/walkthroughs/par/outputs.json"   # e.g. {"run_id": 3721}
scenes:
  - title: "Dana opens this week's performance run"
    url: "/workflow/runs/${run_id}/"
```

- The command runs from the **git toplevel containing the spec file** (write it
  repo-root-relative, exactly as a human would run it); `outputs` resolves against
  the same root and must be a flat JSON object of string/number variables.
- **`rerun: per_render` (the default) is load-bearing for state-mutating demos.**
  If any scene creates real entities (an audit, a task), the recording itself
  changes the world — a re-render without a reseed films "View Audit" where the
  narrative says "Create Audit". Use `rerun: once` only for expensive, idempotent
  generators whose data survives re-renders.
- Substitution happens at render time and never mutates the spec file. An
  unresolved `${...}` is a hard error before recording starts, and **spec-qa
  rejects** a spec that uses `${...}` without declaring `setup.outputs`
  (declared-but-unused outputs are fine).
- Provenance: the recorder copies the resolved variables + command + exit code +
  duration into the RunReport, and writes `setup-vars.json` into the snapshots
  dir — the data a film was made on is part of the run's evidence chain.

Recorder mechanics (including the `--skip-setup` escape hatch and why mutating
demos must not use it) live in the walkthrough skill's "Data setup + `${var}`
substitution" section.

**Capture + late binding (`${var}` minted ON CAMERA) — for a fresh end-to-end
lifecycle each render:**

`setup.outputs` only knows ids minted *before* the render. When the demo's whole
point is to **create an entity on camera and then operate on it** — publish a
solicitation, then review and award the very record just created — a `capture`
action reads the new id off the live page mid-render and threads it into LATER
scenes. No fixed IDs, no per-render state resets.

```yaml
scenes:
  - title: "Author publishes the solicitation"
    url: "/solicitations/new/"
    actions:
      - { kind: fill, target: "Title", value: "Q3 outreach" }
      - { kind: click, target: "Publish" }       # creates the record → redirects to its page
      - kind: capture
        var: solicitation_id                       # binds ${solicitation_id} for LATER scenes
        source: url                                # read the current page URL
        pattern: '/solicitations/(\d+)/'            # capture GROUP 1 is the value (required for source: url)
  - title: "Reviewer awards it"
    url: "/solicitations/${solicitation_id}/review/"   # ← the freshly-minted id
    actions:
      - { kind: click, target: "Award ${solicitation_id}" }
```

- `source: element` reads from a DOM node instead — `target` (same syntax as
  `click`), `attr` (omit ⇒ element text), optional `pattern` (omit ⇒ whole
  trimmed attr/text).
- A scene's `url` resolves at scene START, so it can use a var captured in an
  EARLIER scene but **not** one its own actions capture. Capture in scene N, use
  it in scene N's later actions or scene N+1's url.
- `must_succeed` defaults TRUE for `capture` (a later `${var}` that never bound
  films a literal placeholder URL). A captured var beats a colliding setup
  output (captured wins; recorder warns).
- **spec-qa is order-aware:** a `${var}` is valid iff a setup output OR a
  `capture` in an EARLIER scene provides it. A var bound only by `capture` needs
  no `setup:` block at all; a var used before anything binds it is rejected.

Full recorder mechanics (capture sources, pattern semantics, run-report
debugging, pre-warm skipping capture-bound URLs) live in the walkthrough skill's
"Capture + late binding" section.

### Step 6 — Validate and loop

Run the structural validator (it lives in the canopy runtime):

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
# pass the file arg as an absolute path (resolved before the cd):
SPEC_ABS="$(realpath docs/walkthroughs/<narrative-slug>.yaml)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.validate unified_spec "$SPEC_ABS")
```

If it exits non-zero, read each problem and fix the spec.  Re-run until the
validator exits 0.  After 3 fix attempts, surface the remaining errors to the
user rather than looping further.

Common fixes:
- `scene references undefined persona` → add the persona to `personas` or fix the
  scene's `persona` key.
- `provenance ... does not match any SpineItem.id` → update `provenance` to match
  the correct spine id from the why_brief.
- `why_brief declared but not resolvable` → check the relative path from the spec
  file to the why_brief file.
- `base_url: field required` → add `base_url` at the top level.

**Important:** after the validate pass, also run ddd-spec-qa (SP2.2) to catch
non-falsifiable concept_claims before the concept judge runs:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC_ABS="$(realpath docs/walkthroughs/<narrative-slug>.yaml)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.spec_qa "$SPEC_ABS")
```

Fix any `concept_claim is not falsifiable` violations before proceeding.  spec-qa
also rejects **status tags in scene titles** (`title contains the status tag
'(frontier)'` etc.) — if you see this, the title is annotating build status
instead of telling a story beat. Retitle it as a moment in the demo and move the
status to the why_brief spine.

### Step 7 — Confirm the spec remains a runnable walkthrough

Before reporting success, verify the spec still satisfies the canopy walkthrough
engine's minimum requirements:
- `name`, `narrative`, `base_url`, `personas`, `scenes` are all present.
- Every scene has `persona`, `title`, `show`.
- The spec can be parsed by `scripts.ddd.validate unified_spec` (run via the resolved `DDD_REPO` pattern from Step 6).

Do NOT remove any of these keys even if they seem redundant with the DDD fields.
The unified spec must remain playable by `/canopy:walkthrough`.

### Step 8 — Report

After both validators pass, print:

```
DDD Unified Spec — <narrative-slug>
══════════════════════════════════════

  Spine items: N → M scenes
  Personas: <list of persona names>
  Narrative: <the cohesive narrative, first ~100 chars>...
  Scenes (the story beats, in order):
    [Scene 1] <persona> — <story-beat title> — <concept_claim (first 50 chars)>... (N features)
    [Scene 2] <persona> — <story-beat title> — ... (N features)

  Output: docs/walkthroughs/<narrative-slug>.yaml
  Validator (structural): PASS
  Validator (spec_qa):    PASS

Next steps:
  1. /ddd-narrative-actionability-eval — LLM-as-judge: can a cold reader derive the
     declared features from the narration alone? (gate — must pass before review)
  2. /ddd-narrative-review — get the user's explicit approve/redraft on the story
     arc (the concept gate) before any rendering or building.
  3. /ddd-run — render, judge, and converge.
```

If there are DECISION gaps from the why_brief, list them explicitly so the user
can make those decisions before the concept judge runs.

## Scene shape — one scene = one persona + one beat + one claim

The structural rule: each scene demonstrates ONE claim, owned by ONE persona,
in ONE narrative beat. The actionability eval scores per scene (cold-derives a
build plan from each scene's narration alone, then compares to that scene's
`features[]`), and the build phase tracks features per scene; a scene that
fragments across multiple claims fragments both.

**The split smell test:** if a scene's `features[]` map to multiple distinct
spine items — different provenances would honestly apply to different features
within the scene — that's a scene wanting to be N scenes. Split before
posting. Each resulting scene gets one provenance (the spine item its features
ground) and its own `narrative`/`show`/`concept_claim` for that single beat.

`scene.narrative` (the canonical per-scene text — what `apply_narrative_edits`
writes when the user edits a scene) MAY be one or more sentences. Prefer one
cohesive sentence; multi-sentence is fine when the beat genuinely is one
moment of the demo. But if you see multiple sentences each describing distinct
user actions with distinct backing capabilities, that's the split signal — not
a "leave it as a multi-sentence beat" signal.

## After retitling — sync the build_order

If you retitle a scene, regenerate any `build_order` entries that referenced
its old slug. The slug derives from the title (`_title_slug`); spec_qa will
reject any `build_order` entry whose slug doesn't match a current scene.

## YAML pitfalls when writing scene fields

Watch for these when writing `show`, `role`, `concept_claim`, `verify`, and the
top-level `narrative`:

- **Colons inside the value** (e.g. `Coverage: Balanced`) break naive
  single-quoted strings — YAML treats the second colon as a mapping marker.
  Use **double-quoted strings** (`"..."`) for any field whose value contains
  `: ` (colon-space), `{`, `}`, or unescaped apostrophes.
- **Apostrophes inside a YAML single-quoted block** must be doubled — write
  `Kim''s edits` for "Kim's edits" inside a `'...'` block. Inside `"..."`
  blocks, plain apostrophes are fine.
- **Curly braces** inside an unquoted value get parsed as a flow mapping —
  quote the whole value (e.g. `"...payload {a: b} for each..."`).

When in doubt, double-quote.
