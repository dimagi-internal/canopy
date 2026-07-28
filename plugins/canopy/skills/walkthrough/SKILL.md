---
name: walkthrough
description: |
  Execute a demo walkthrough spec against a live app and generate a stakeholder-ready
  HTML slideshow with screenshots, AI quality scores, and run-to-run comparison.
  Core run procedure only — for improve/adversarial/eval modes, use the walkthrough agent.
  Use when asked to "run the walkthrough", "demo prep", or "walkthrough <name>".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# /walkthrough: Demo-Driven Development

Execute a YAML demo spec against a live app using a headless browser. Generate a
stakeholder-ready HTML presentation with screenshots, narrative, and AI quality
evaluations.

## Modes

- `/walkthrough <name>` — Execute `docs/walkthroughs/<name>.yaml`
- `/walkthrough generate` — Interactively create a new walkthrough spec
- `/walkthrough` (no args) — List available specs in `docs/walkthroughs/`

All run modes accept `--scene <selector>` to render only a subset of
scenes. See **Scene filter** below.

For orchestrated improvement cycles, adversarial reviews, and eval tracking,
use the walkthrough **agent** (invoked via `/walkthrough improve`, `/walkthrough adversarial`,
or `/walkthrough eval`).

## Scene filter (`--scene`)

Use this when you've just shipped a change that affects one scene and want
to re-grade THAT scene under the canonical 5-dimension rubric, instead of
re-running the whole spec.

**Why it exists:** without it, single-scene iteration drifts off the DDD
path — people screenshot and "verify" manually, skipping the rubric. The
filter lets the canonical pipeline run end-to-end on a subset, so the
judge, the deck, and the eval history stay consistent.

### Selector syntax

| Form | Meaning |
|------|---------|
| `--scene 2` | Just scene 2 (1-based index) |
| `--scene 2,4,5` | Scenes 2, 4, and 5 |
| `--scene 2-4` | Inclusive range — scenes 2, 3, 4 |
| `--scene name-match` | Case-insensitive substring match against scene `title` or `spine_id` |

If the selector matches **zero** scenes, STOP and tell the user:

> "Selector `<sel>` matched no scenes in <spec>. Spec has scenes: 1=<t1>,
> 2=<t2>, ... Pick a different selector or omit `--scene` to run all."

If the selector matches **all** scenes, treat it the same as no filter and
say so once: "selector matched all N scenes — equivalent to a full run."

### Behavior contract

1. **Preserve original scene indices.** A single-scene run for spec scene
   2 still labels the output "Scene 2 of N" in narration and the deck —
   NOT "Scene 1 of 1". This keeps comparison against full-spec runs
   honest (a scene-2 score from a partial run is directly comparable to
   a scene-2 score from a full run).

   **Same rule in the engine-produced manifest.** The engine writes
   `scene_index` as the ORIGINAL spec index, not the position within the
   filtered set, and you preserve that when you merge per-scene scores
   into `/tmp/walkthrough-run-data.json`. A `--scene 3` partial run
   produces `scene_index: 3` in the slide entry — that's what makes
   comparisons against a full-spec run honest. Flattening it to 1-of-N
   (1, 2, 3 inside a 3-scene partial) breaks all the cross-run analytics
   that key on the original index. Example shape (the manifest the engine
   emits — see the Score the captured frames section):

   ```json
   {
     "slides": [
       { "type": "scene", "scene_index": 3, "scene_total": 5, "..." }
     ],
     "scenes_run": [3],
     "scene_filter": "3"
   }
   ```

   `scene_index: 3` ALWAYS reflects the spec position; `scenes_run: [3]`
   and `scene_filter: "3"` tell consumers what was rendered. Don't
   conflate them.

2. **The engine tags the manifest.** `record_video.py` writes two fields
   to `/tmp/walkthrough-run-data.json`:
   - `scenes_run: [2]` — the list of scene_index values actually rendered
   - `scene_filter: "2"` — the raw selector string from `--scene`

   Downstream tooling (deck generator, eval history, `ddd-run`
   convergence reporter) reads these to distinguish partial runs from
   full runs. Pass `--scene` to `record_video.py` so the single capture
   pass renders exactly the filtered subset and tags the manifest.

3. **Run the full rubric.** The 5-dimension rubric still applies per
   scene. `--scene` only changes WHICH scenes get rendered, not how
   they're judged. Do not skip the blocking rule, do not weaken
   `canopy:visual-judge`, do not skip the cross-walkthrough sanity floor
   (it just operates on the filtered set).

4. **Single-scene caveats.** With one scene rendered, the "cross-scene
   sanity floor" loses some statistical power — log "scene-filter mode:
   sanity floor weak (n=1)" in the summary but still apply it.

5. **Improve mode.** `/walkthrough improve <name> --scene 2` iterates
   until scene 2 passes 4+ on all dimensions, then stops. The composite
   in the final report should say "(scene 2 graded; scenes 1,3,4,5 not
   re-run this iteration)".

6. **Eval mode.** `/walkthrough eval <name> --scene 2` writes to
   `screenshots/walkthroughs/<name>/runs/YYYY-MM-DD-vNNN-scene2/` (note
   the `-scene<sel>` suffix on the run dir). Does NOT overwrite the
   full-spec baseline. `--update-baseline` with `--scene` is rejected —
   baselines must be full-spec runs, not partials.

### Parsing the selector

Use this Python snippet (or equivalent) once at the start of the run:

```python
def select_scenes(spec_scenes, selector):
    """Return list of (orig_index, scene_dict) tuples to actually run.
    orig_index is 1-based and preserved through the rest of the pipeline.
    """
    indexed = list(enumerate(spec_scenes, start=1))
    if not selector:
        return indexed
    sel = selector.strip()
    # 2-4 (range)
    if "-" in sel and all(p.strip().isdigit() for p in sel.split("-", 1)):
        a, b = (int(p) for p in sel.split("-", 1))
        return [(i, s) for i, s in indexed if a <= i <= b]
    # 2,4,5 (list)
    if "," in sel and all(p.strip().isdigit() for p in sel.split(",")):
        wanted = {int(p) for p in sel.split(",")}
        return [(i, s) for i, s in indexed if i in wanted]
    # single index
    if sel.isdigit():
        n = int(sel)
        return [(i, s) for i, s in indexed if i == n]
    # title / spine_id substring (case-insensitive)
    needle = sel.lower()
    return [
        (i, s) for i, s in indexed
        if needle in (s.get("title", "") or "").lower()
        or needle in (s.get("spine_id", "") or "").lower()
    ]
```

The rest of the procedure (auth, pre-flight, scene execution, judging,
deck generation, video recording) is identical — it just iterates over
the filtered subset instead of `spec.scenes` directly.

## YAML Spec Format

Walkthrough specs live in `docs/walkthroughs/<name>.yaml`.

> **DDD narratives are stored as a PAIR.** Once a narrative lives on canopy-web,
> its spec is split in two: `<slug>.recipe.yaml` (git — how to film it: `show`,
> `url`, `viewport`, `actions`, `pace`, `concept_claim`) and a generated
> `<slug>.narrative.lock.json` (the story, fetched with
> `python -m scripts.ddd.narrative pull <slug> <dir>`). The loader composes them,
> so every command below accepts either a single unified `.yaml` or a
> `.recipe.yaml`. Never hand-edit a lock — `scripts.ddd.check_locks` fails the
> build if you do. Plain (non-DDD) walkthroughs stay single-file.
>
> **`pull` fetches the story half only.** canopy-web has no copy of the recipe
> and cannot produce one, so a narrative authored on another machine (or by
> another agent) lands here with a lock, a why-brief, and nothing to render.
> `pull` reports `"renderable": false` and warns on stderr when no
> `<slug>.recipe.yaml` sits beside the lock — write one before trying to render.


```yaml
name: "Demo Name"
narrative: "One-line thesis for the demo"
base_url: "http://localhost:8000"

# Silent video recording (optional — see "Record Video" section)
record_video: true              # default: false
video_pace: fast                # fast | medium | slow (default: fast)
video_viewport_width: 1280      # default: 1280
video_viewport_height: 720      # default: 720
prewarm: true                   # default: false — visit every unique scene URL once
                                # OFF camera before filming, so cold caches don't
                                # freeze-frame on film (see "Recording time & dead space")

# Review mode (optional, DDD-only) — autonomous (default) | human. Human mode
# routes PRODUCT judge findings to the product_findings review gate instead of
# auto-applying them; ignored by plain /canopy:walkthrough runs.
review_mode: autonomous

# Auth (optional — omit for public pages)
auth:
  type: url                    # "url" or "command"
  url: "/auth/login?token=dev" # for type: url — navigate here to authenticate

# For command-based auth:
# auth:
#   type: command
#   check: "python manage.py check_token"    # command to verify auth
#   login: "python manage.py get_token"      # interactive command (user runs with !)
#   inject_url: "/auth/set-token?token={token}"  # URL to inject the token

# Data setup (optional — the synthetic generator that puts the world in a
# recordable state; see "Data setup + ${var} substitution" under Record Video)
setup:
  command: "python scripts/walkthroughs/par/regenerate.py"  # runs from the SPEC's git repo root
  outputs: "scripts/walkthroughs/par/outputs.json"          # flat JSON {var: string|number}
  rerun: per_render             # per_render (default — required for state-mutating
                                # demos) | once (skip when outputs file already exists)
  timeout_seconds: 1200         # abort the render if setup runs longer

personas:
  alice:
    name: "Alice Smith"
    role: "Program Manager, Acme Corp"
    color: "#2563eb"
    intro: "Alice manages field programs and needs fast reporting."

scenes:
  - id: dashboard-overview     # REQUIRED — stable, permanent scene identity.
                               # Lowercase alphanumerics + single hyphens. Author
                               # it ONCE and never change it: it is the key the
                               # render recipe and the story are joined on, and
                               # the key canopy-web's version diff pairs on.
                               # Renaming an id is deleting a scene and adding
                               # another, and reads that way everywhere. NEVER
                               # derive it from the title — titles get reworded
                               # by reviewers, and that used to silently delete
                               # the scene's `show` selector.
    persona: alice
    title: "Dashboard overview"
    show: "Main dashboard with KPIs loaded"
    impressive_because: "Data loads in real-time, charts are interactive"
    ai_quality: "KPI descriptions should be specific to the program, not generic"  # optional
    video_hold_seconds: 8         # optional, legacy — end-of-scene hold override; prefer a
                                  # `hold` action (see "Recording time & dead space")
    pace: flow                    # optional — teach (default; full read-time) | flow
                                  # (this beat is just continuity → compressed holds +
                                  # faster cursor; pair with terse/no narration)
    viewport: { width: 1440, height: 900 }  # optional — per-scene viewport override
                                            # (this scene only; other scenes stay at the
                                            # spec-level video_viewport_width/height)
```

## Setup

### 1. Find the browse binary and set state file

```bash
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
B=""
[ -n "$_ROOT" ] && [ -x "$_ROOT/.claude/skills/gstack/browse/dist/browse" ] && B="$_ROOT/.claude/skills/gstack/browse/dist/browse"
[ -z "$B" ] && B=~/.claude/skills/gstack/browse/dist/browse
if [ -x "$B" ]; then
  echo "READY: $B"
else
  echo "NEEDS_SETUP: run 'cd ~/.claude/skills/browse && ./setup'"
fi
```

**CRITICAL:** Set the browse state file to avoid lock conflicts and stale sessions.
Pick a fixed path for this walkthrough and use it for ALL browse commands in this run:
```bash
export BROWSE_STATE_FILE=/tmp/walkthrough-browse-<name>.json
```
Replace `<name>` with the walkthrough spec name (e.g., `baobab-demo`). Do NOT use `$$`
(shell PID) — it changes across Bash calls. Without this, the browse server will fail
with "Another instance is starting" if another session has used browse recently.

**After starting browse, verify it's pointing at the right app:**
```bash
$B goto <base_url>
$B text
```
If the page content is from a different app (e.g., you see "Fund Dashboard" when you
expected "Programs"), the browse session is stale. Kill it and restart with the state
file set.

### 2. Read the walkthrough spec

```bash
cat docs/walkthroughs/<name>.yaml
```

Parse the YAML to extract: `name`, `narrative`, `base_url`, `auth`, `personas`, `scenes`.

### 3. Check for previous run

```bash
SIDECAR="screenshots/walkthroughs/<name>.json"
[ -f "$SIDECAR" ] && cat "$SIDECAR" || echo "NO_PREVIOUS_RUN"
```

If a previous run exists, keep its data for the summary slide comparison.

### 4. Create the output directory

**CRITICAL:** Use a per-walkthrough snapshots directory, not a shared global one.
Without this, screenshots from different projects or previous runs bleed through.

```bash
mkdir -p screenshots/walkthroughs/<name>
```

The render engine writes each scene's `scene_{scene_index}.png` +
`scene_{scene_index}_page_text.json` into this dir via its `--snapshots`
flag (**Render once via the engine**); the scoring step reads them back.
Never point `--snapshots` at a bare `/tmp/walkthrough-screenshots/` — that's
shared across all sessions and projects.

### 5. Authenticate

Handle auth based on the spec's `auth` block:

**If `auth.type` is `url`:**
```bash
$B goto <base_url><auth.url>
$B text
```
Verify the response indicates success (check for error messages).

**If `auth.type` is `command`:**
1. Run the check command to see if auth is already valid:
   ```bash
   <auth.check>
   ```
2. If expired or missing, tell the user to run the login command interactively:
   > "Your auth is expired. Please run this to re-authenticate:"
   > `! <auth.login>`
3. Wait for the user to confirm, then inject the token via `auth.inject_url`.

**If no `auth` block:** Skip authentication (public pages).

## Pre-flight Check

Before executing any scenes, verify the target app is healthy:

```bash
$B goto <base_url>
$B wait --networkidle
```

Check for:
- **CSS loading:** Does the page look styled? Check for 404s on CSS/JS bundles in
  console output. If assets are missing (common with worktree servers that haven't
  run a build step), STOP and tell the user: "The server at {base_url} is missing
  CSS assets — the app looks unstyled. Run the build step first."
- **Correct app:** Does the page content match what you expect? If you see content
  from a different app, the browse session is stale.
- **Server responding:** If the page is blank or shows a connection error, the server
  isn't running.

Do NOT proceed to scene capture if the pre-flight fails. Bad captures waste time
and the user will catch it before you do.

### Target identity: announce and don't substitute

Every spec has an implicit or explicit **target** — the specific slug, id, entity,
or URL path its scenes are written against (e.g., a named opportunity, a project
id, a user account). Before Scene 1:

1. **State the target in one line.** Name the identifier and what's visible about
   its state right now:
   > "Target: `<identifier>`, <N>/<M> preconditions met (e.g. 1/19 skills complete).
   > Spec asserts: `<key ai_quality or show claim>`."
2. **If the visible state falsifies the spec's premise, STOP.** Don't guess, don't
   improvise a save, don't pivot. Ask the user:
   > "Spec targets `<X>` and asserts `<Y>`, but the live state shows `<Z>`. Options:
   > (1) fix the data, (2) switch target (requires spec edit), (3) truncate to the
   > scenes that do work, (4) abort. Which?"

**You may NOT silently substitute a different target.** If scenes 2–5 can't run
against `<X>` and you find `<X2>` looks more populated, that is not authorization
to switch — it's a signal to ask. Silent substitution breaks the narrative premise
and the user will catch it.

## The flow: render once → score → deck

This skill runs on the **same render engine + manifest that DDD uses** —
there is ONE renderer, not two. The three steps are:

1. **Render once** (`record_video.py --manifest …`) — a single capture
   pass drives the product, writes the mp4 + per-scene screenshots, AND
   emits the manifest (`walkthrough-run-data.json`). See **Render once via
   the engine** below.
2. **Score the captured frames** — for each scene, run
   `canopy:visual-judge` against that scene's engine-captured screenshot +
   page text, and MERGE the verdict into the manifest by setting
   `slides[i]["ai_evaluation"]`. See **Score the captured frames** below.
3. **Deck from the manifest** — `generate_presentation.py` reads the
   scored manifest. See **Generate Presentation** below.

There is NO separate, hand-authored run-data JSON and NO after-scoring
video pass: the video, the screenshots, and the deck all come from the
SAME capture. The engine produces the manifest score-free; scoring is an
overlay merged on top.

## Render once via the engine

Run `record_video.py` once. This single pass drives the product (following
each scene's `url` + `actions`), films the mp4, writes a per-scene
screenshot + page-text JSON into the `--snapshots` dir, and emits the
manifest (`walkthrough-run-data.json`) — the canonical record of what was
rendered. See **Record Video** below for the full invocation (cookies,
`--spec`, `--output`, `--snapshots`, `--scene`, setup/prewarm flags) and
for the `actions` / timing authoring contract.

The manifest the engine writes is the canonical run-data: per scene it
carries `scene_index` / `scene_total` / `title` / `narration` / `persona_key`
/ resolved `url` / `urls_visited` / `screenshot_b64` / `screenshot_path` /
`page_text_path` / `mp4_start_offset`, plus top-level `name` / `narrative` /
`personas` / `scenes_run` / `scene_filter` / `duration_seconds`. Every
scene's `ai_evaluation` is `null` on emission — you fill it in during
scoring (next section). You do NOT hand-author this file.

## Score the captured frames

For each `(orig_index, scene)` in the rendered scene list (the manifest's
`slides` of `type: "scene"`; with `--scene` this is already the filtered
subset — see **Scene filter**):

### Scene Scoring Pattern

1. **Announce the scene** to the user using the **original spec index**:
   "Scene {orig_index}/{spec_total}: {title} (as {persona_name})"

   With a scene filter, mention it once at the top:
   > "Scene filter active: `--scene <sel>` → scored {len(filtered)} of {spec_total} scenes (indices: {orig_indices})."

2. **Load the engine's captured frame.** The render pass already wrote the
   screenshot for this scene to the `--snapshots` dir
   (`scene_{scene_index}.png`) and the page text to
   `scene_{scene_index}_page_text.json`; the manifest slide's
   `screenshot_path` / `page_text_path` point at them. You do NOT
   re-navigate or re-screenshot — the engine drove the product and froze
   the frame already. (The same frame is the one the mp4 holds on.)

   **If a captured screenshot is absurdly tall (10,000+ pixels):** This is a
   BUG in the app, not a capture problem. An infinitely growing element
   (e.g., Chart.js canvas with `maintainAspectRatio: false` in an
   unconstrained container) is making the page impossibly tall. Flag it as a
   **[CODE]** issue with Demo Readiness ≤ 2 and use the blocking rule — do
   NOT score it 4/5.

3. **Show the screenshot to the user** using the Read tool on the PNG file.

4. **Evaluate the scene via `canopy:visual-judge`.**

   The engine already captured the page text — read it from the snapshots
   dir (the manifest slide's `page_text_path`) as the anchor for
   verbatim-quote scoring:

   ```bash
   PAGE_TEXT=$(cat <snapshots_dir>/scene_{scene_index}_page_text.json)
   ```

   Dispatch the visual judge with walkthrough's rubric and the
   scene's narrative anchors (point `screenshot_path` at the engine's
   captured PNG — the slide's `screenshot_path`):

   ```
   Skill('canopy:visual-judge', args={
     screenshot_path: "<snapshots_dir>/scene_{scene_index}.png",
     page_text:       <PAGE_TEXT>,
     rubric:          <load skills/walkthrough/rubric.yaml verbatim>,
     context: {
       audience: { name: "skeptical CEO of a Fortune 500", decision: "deciding whether to adopt your product" },
       competitors: ["Linear", "Notion", "Slack", "Vercel", "Height", "Superhuman"],
       projector_test_phrasing: "Would you put this EXACT slide on a projector at an all-hands tomorrow morning, to an audience including your most demanding stakeholder, without ANY verbal caveats?",
       narrative_anchors: [<scene.impressive_because>, <scene.ai_quality>],
       blocking_rules: ["demo_readiness_low", "narrative_falsified"],
     },
   })
   ```

   The judge runs the Tough Judge methodology
   (adversarial listing → score-from-3 default → projector test → cross-check)
   and returns a verdict object with per-dimension scores + adversarial
   listing + projector test result + fix recommendation. Walkthrough's
   5 dimensions live in `rubric.yaml` (Content / App Page / Screenshot
   / Slide / Demo Readiness). The methodology was extracted from this
   skill into `canopy:visual-judge` in v0.2.79; per-rubric scoring
   conventions (start-from-3, weakest-link overall, projector-gate)
   are preserved verbatim.

   **Blocking-rule handling.** If the verdict comes back with
   `verdict: "blocked"` (either Demo Readiness ≤ 2, or scene-1/2
   narrative falsified), STOP the walkthrough IMMEDIATELY and tell
   the user:

   > "Scene {n} scored {dim.score}/5 on {dim.label} — this would
   > hurt the demo.
   > Page: {full URL that was loaded for this scene}
   > The issue is: {verdict.fix_recommendation}.
   > Should I fix this now before continuing, or skip this scene?"

   Always include the full URL so the user can open the page directly
   before deciding. Do NOT silently log a 2/5 and keep going.

   **Cross-walkthrough sanity floor.** After all scenes complete (or
   the loop blocks): if the average overall_score across scenes is
   > 4.0, you are almost certainly scoring too generously. Re-run
   any scene whose adversarial listing was thin and revise downward.
   This sanity rule applies at the WALKTHROUGH level (across scenes)
   and is enforced HERE, not in canopy:visual-judge (which is per-
   screenshot).

   **NEVER fabricate scores.** Don't construct a verdict by hand —
   always invoke `canopy:visual-judge` so the methodology stays
   consistent across rubrics + future evals that consume the same
   judge.

5. **Merge the verdict into the manifest.** Scoring is an overlay on the
   engine-produced manifest, not a rebuild of it. Load
   `/tmp/walkthrough-run-data.json`, find this scene's slide (by
   `scene_index`), and set its `ai_evaluation` from the judge verdict:

   ```python
   import json
   p = "/tmp/walkthrough-run-data.json"
   m = json.load(open(p))
   for s in m["slides"]:
       if s.get("type") == "scene" and s["scene_index"] == <orig_index>:
           s["ai_evaluation"] = {
               "score": <LOWEST of the 5 dimension scores — weakest-link>,
               "max_score": 5,
               "commentary": "Overall: 3/5 (weakest: Content). A: Content 3/5 — generic. "
                             "B: App Page 4/5. C: Screenshot 4/5. D: Slide 4/5. E: Demo Ready 3/5.",
           }
   json.dump(m, open(p, "w"), indent=2)
   ```

   `ai_evaluation.score` MUST be the LOWEST of the 5 dimension scores
   (weakest-link), and the commentary MUST list all 5 dimension scores in
   the format shown. Leave every other key the engine wrote untouched —
   you are only filling in the `ai_evaluation` the engine emitted as
   `null`. Do NOT add or rewrite `screenshot_b64`, `url`, `mp4_start_offset`,
   `scenes_run`, etc.; the engine is the source of truth for those.

### Fixing Data Issues

When a scene fails due to bad demo data (`[DATA]` issues — duplicates, placeholder names,
missing records, wrong values), **do not try to fix data through the browser**. The browser
is for observing and verifying, not for clicking edit/delete links or submitting forms.

Instead, step into the codebase:

1. **Understand the data layer.** Read the app's models, views, and API endpoints to
   understand how the data is structured and how it's created/updated/deleted. Use Grep
   and Read — not the browser — to find the relevant code.

2. **Check for existing tools.** Look for:
   - Management commands (e.g., `python manage.py seed_data`, `create_demo_data`)
   - Fixture files or seed scripts
   - REST/GraphQL API endpoints for CRUD operations
   - Available MCP tools that interact with the app's data layer

3. **Fix through the proper interface.** Use the app's own APIs, management commands,
   or ORM-level scripts to create, update, or delete data. This is faster, more reliable,
   and less error-prone than navigating forms in a headless browser.

4. **Re-render to verify the fix.** After making the data fix, re-run the
   render pass (it reseeds and re-captures — see **Render once via the
   engine**) and re-score the affected scene against the fresh capture.
   Do not patch the screenshot by hand in browse — the engine owns capture.

The system IS allowed to mutate data — on localhost or production — but it should do so
by understanding the codebase's data APIs, not by fumbling through UI forms.

**Record issues.** If a scene came back wrong (page error, empty state,
unresolved load), note it as an issue with severity (error/warning) and
description. A failed `must_succeed` action aborts the render loudly and
the run report flags it; a non-critical miss is logged and the capture
continues — partial decks are better than no deck.

**Flag test data problems.** When reviewing each captured frame, check for
signs that test/sample data doesn't look realistic:
- Organization names like "Unknown Organization" or "None None"
- Placeholder usernames like "test-user" or blank names
- Empty states that should have data (charts with "no data", maps with no markers)
- Duplicate entries (same person/org appearing multiple times with identical content)
- IDs or slugs showing instead of human-readable names
If found, note it as an issue so the user knows the demo won't look right
with this data — and fix the data + re-render rather than shipping it.

### The manifest shape (for reference)

You do NOT hand-author this file — the engine writes it (**Render once via
the engine**) and you overlay `ai_evaluation` during scoring. This is the
shape the deck and downstream consumers read:

```json
{
  "name": "<from spec>",
  "narrative": "<from spec>",
  "generated_at": "<ISO timestamp, engine-stamped>",
  "duration_seconds": 180,
  "personas": "<from spec — the full personas dict>",
  "scenes_run": [2],
  "scene_filter": "2",
  "slides": [
    { "type": "title" },
    { "type": "persona_intro", "persona_key": "<first persona>" },
    {
      "type": "scene",
      "scene_index": 2,
      "scene_total": "<total scenes IN THE SPEC, not the filtered count>",
      "persona_key": "<persona>",
      "title": "<scene title>",
      "narration": "<impressive_because / show from spec>",
      "url": "<full resolved URL that was rendered>",
      "urls_visited": ["<every URL this scene navigated through>"],
      "screenshot_path": "<snapshots-relative PNG path>",
      "page_text_path": "<snapshots-relative page-text JSON path>",
      "screenshot_b64": "<base64 encoded PNG>",
      "mp4_start_offset": 12.4,
      "ai_evaluation": {
        "score": 3,
        "max_score": 5,
        "commentary": "Overall: 3/5 (weakest: Content). A: Content 3/5 — generic. B: App Page 4/5 — clean. C: Screenshot 4/5 — good. D: Slide 4/5 — clear. E: Demo Ready 3/5 — needs polish."
      }
    },
    {
      "type": "summary",
      "scenes_completed": "<count>",
      "scenes_total": "<total>",
      "ai_scores": [{ "feature": "<title>", "score": 3, "max_score": 5 }],
      "issues": [{ "scene": 1, "severity": "warning", "description": "..." }],
      "previous_run": "<previous sidecar JSON or null>"
    }
  ]
}
```

The engine emits each scene's `ai_evaluation` as `null`; you fill it in
during scoring (**Score the captured frames**). `ai_evaluation.score`
must be the LOWEST of the 5 dimension scores (weakest-link), and the
commentary must include all 5 dimension scores in the format shown above.

**Scene-filter fields (engine-written).** `scenes_run` is a JSON array of
the original 1-based spec indices that were actually rendered (e.g. `[2]`
for a single-scene run; `[1, 2, 3, 4, 5]` for a full run). `scene_filter`
is the raw selector string from `--scene`, or `null` for a full run. Both
fields are always present (the engine writes `scenes_run: [1, 2, ..., N]`
and `scene_filter: null` on a full run) so consumers can check shape
unconditionally.

**`walkthrough-eval` and `walkthrough-defect-creator` consume this SAME
`walkthrough-run-data.json`** — a superset of the old hand-authored shape
with the same keys — so they keep working unchanged against the
engine-produced manifest.

**`url` and `screenshot_b64` are engine-written.** The render pass records
each scene's resolved `url` (and `urls_visited`) and embeds the base64 PNG
as `screenshot_b64` — these render as a context row under the slide title.
You don't gather them by hand; they arrive in the manifest. (The deck also
reads an optional `logged_in_as` per scene if present — the engine doesn't
set it, so add it only if you want the "Logged in as …" context line.)

**Framing slides (title / persona_intro / summary) you DO add.** The engine
emits only `type: "scene"` slides. Before generating the deck, insert into
the manifest's `slides`: a leading `{ "type": "title" }`, a
`{ "type": "persona_intro", "persona_key": <key> }` before the first scene
of each new persona, and a trailing `{ "type": "summary", ... }` (scene
counts, `ai_scores`, `issues`, `previous_run`). `generate_presentation.py`
renders these slide types; without them the deck loses its title card,
persona intros, and scorecard.

## Generate Presentation

The deck comes from the same engine-produced manifest you just scored — the
input is `/tmp/walkthrough-run-data.json` (with merged `ai_evaluation` and
the framing slides added), NOT a hand-authored file. Resolve the generator
(a project-local copy wins if present; otherwise the canopy runtime,
resolved via `scripts/canopy-runtime.sh`) and run it:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
GEN="tools/walkthrough/generate_presentation.py"
[ -f "$GEN" ] || GEN="$CANOPY_ROOT/scripts/walkthrough/generate_presentation.py"
uv run --project "$CANOPY_ROOT" python "$GEN" --input /tmp/walkthrough-run-data.json --output screenshots/walkthroughs/<name>.html
```

Then open the result:
```bash
open screenshots/walkthroughs/<name>.html
```

## Record Video — the single capture pass (step 1)

This is the render engine — and it is the FIRST step of the flow, not an
after-pass. `record_video.py` drives the product through each scene's
`url` + `actions` in a Playwright Chromium context with `record_video`
enabled, and in one pass produces **all** of:

- the silent mp4 (`--output`),
- the per-scene screenshots + page-text JSON (`--snapshots`), and
- the manifest `walkthrough-run-data.json` (`--manifest`) — the canonical
  run-data you score against and deck from.

There is no separate after-scoring video pass: the video, the screenshots
the judge scores, and the deck all come from this SAME capture, so they
can never desync. The spec's `record_video: true` no longer gates whether
the engine runs (it always does — it's how the manifest is produced); it
signals that the mp4 is a deliverable to surface to the user alongside the
deck. The webm is converted to mp4 via ffmpeg.

The rest of this section is the authoring contract for what the capture
does — scene `actions`, the timing model, and the `setup:` / prewarm
flags — followed by the **Run** invocation.

### Interactive recording — scene `actions` (cursor + clicks)

The recorder injects a **synthetic cursor** (`_lib/cursor_overlay.js`) on every
context, so the mouse is visible and clicks draw a ripple. A scene with no
`actions` falls back to a scroll-pan (a static page tour). A scene that declares
`actions` is **driven** — the recorder glides the cursor and performs each step,
so the video shows the feature being *used*, not just displayed. This is what
lifts the `feature_use` score off the floor — a demo where nothing is clicked
reads as a slideshow.

Declare `actions` per scene in the spec (see `ddd-spec` for authoring + the
`Action` schema in `scripts/narrative/models.py`). Verbs: `goto`, `click`,
`click_menu`, `fill`, `select`, `type`, `press`, `hover`, `scroll_to`,
`scroll`, `wait_for`, `hold`, `draw`, `map_click`, `capture`. Each action is
`{kind, target?, value?, seconds?, note?}`; `target` is visible text OR a CSS
selector. For `kind: capture` (mint a `${var}` on camera — see the **Capture +
late binding** section below), read an id off the page (`source: url` or
`source: element`) into a variable that LATER scenes/actions resolve. For
`kind: select` (native `<select>` controls — which `click`
can't reliably open across platforms), `value` is the option's `value`
attribute / a digit-only string as the 0-based index / the visible label —
recorder tries each in order. For `kind: draw` (drawing a polygon on a
map / canvas — see the `draw` section below), `target` is the canvas
element, `points` is a list of `[fx, fy]` fractions (0-1) within its box,
and `tool` is the draw-tool button to activate first. For `kind: map_click`
(click a NAMED map feature — see the `map_click` section below), `target` is the
feature's `name` (e.g. a ward) and the app's own click handler does the rest.
Example:

```yaml
scenes:
  - persona: maya
    title: "Maya tunes the plan"
    url: "/microplans/program/133/setup/"
    show: "exclude an invalid work area and watch the metrics update"
    actions:
      - { kind: wait_for, target: "PLAN METRICS" }
      - { kind: scroll_to, target: "Exclude" }
      - { kind: click, target: "Exclude", note: "drop an invalid area" }
      - { kind: wait_for, target: "Excluded 1" }
      - { kind: hold, seconds: 1.5 }
```

A bad/missing action target is logged and skipped — never fatal. The primitives
live in `scripts/walkthrough/_lib/recorder.py` (`execute_action` dispatcher).

**Anti-pattern — don't write both `url:` AND a leading `goto target: <same-url>`.**
The recorder navigates from `scene.url` automatically at the top of every scene. A
duplicate leading `goto` to the same path causes a visible page reload ~1-2s into
every scene (the page already loaded once, and now it loads again). Pick one:
`url:` is the declarative entry point — a `goto` action is only for navigating
**mid-scene** to a DIFFERENT page. The recorder strips redundant leading gotos as a
safety net (canopy 0.2.151+) but the spec should still read as authored — don't
lean on the strip.

**Open with `wait_for`, not a `hold`.** When the scene starts on a page that takes
a moment to render, the first action should be `{kind: wait_for, target: <text or
selector>}`. `wait_for` exits the instant the page is ready; `hold` always burns
its full duration. As a bonus, a leading `wait_for` tells the recorder to skip its
default `initial_hold_ms` + `goto_settle_ms` blind pauses (canopy 0.2.151+) — the
wait_for IS the settle, so the holds are pure dead air on top.

**Long waits use `wait_for seconds:`, not `hold seconds:`.** For waits that can
exceed the default 12s (a 30-90s bulk import, a slow background job), use the
`seconds:` override on `wait_for`:
```yaml
- { kind: wait_for, target: "Created 10 of 10 plans", seconds: 120 }
```
NOT `{kind: hold, seconds: 90}` — `wait_for` exits the instant the success text
appears; `hold` always burns the full 90s even if the job finished in 15.

**Don't `wait_for` on a transient intermediate state.** When a button's label
flickers through `"Creating N plans…"` → `"Created N of N plans"` faster than
the resolver can poll, a `wait_for` on the intermediate text races (~50% miss
rate) and the run report's "failed" column fills with false positives. Worse,
the spec author can't tell whether the failure means "something broke" or
"this transient text was too fast."

Pattern: wait only on TERMINAL states — the success card that doesn't replace
itself, the toast that stays for 5 seconds, the page heading that sticks. Use
`seconds:` to extend the timeout for long-running terminal waits.

```yaml
# ✗ Anti-pattern — races on the intermediate flicker
- { kind: click, target: "Create 10 plans" }
- { kind: wait_for, target: "Creating 10 plan" }   # ← races, often "fails"
- { kind: wait_for, target: "Created 10 of 10 plans" }

# ✓ Pattern — wait only on the terminal success state
- { kind: click, target: "Create 10 plans" }
- { kind: wait_for, target: "Created 10 of 10 plans", seconds: 120 }
```

**Never `wait_for` an element inside a collapsed/hidden container — wait on
the visible container via `:has()`.** `wait_for` with a selector waits for the
element to be *visible*. An `<option>` inside a closed `<select>` is never
visible (same for items inside a closed dropdown/accordion/`display:none`
panel), so the wait burns its FULL timeout as a frozen frame on film and then
reports failure — even though the element exists and the data loaded long ago.
Wait on the visible container, asserting the child exists inside it:

```yaml
# ✗ Anti-pattern — option is invisible inside a closed select; burns 20s of film
- { kind: wait_for, target: "css:select#run-picker option[value='3721']", seconds: 20 }

# ✓ Pattern — the select is visible; :has() asserts the option arrived
- { kind: wait_for, target: "css:select#run-picker:has(option[value='3721'])", seconds: 20 }
```

(Root-caused on program-admin-report: a 20s `wait_for` on a bare `option`
contributed the single largest block of frozen-frame dead space in the film.)

**Target resolution syntax — prefer prefixes over bare CSS.** Every action's
`target` field can use a prefix to control how the recorder resolves it. Bare
strings use a heuristic (CSS-shaped → selector engine; English → visible-text
ranking via Playwright `get_by_role` / `get_by_text`), which is fine for most
cases. When the heuristic gets it wrong, or you want to be explicit, use a
prefix:

| Prefix | Routes to | When to use |
| --- | --- | --- |
| `css:#cfg-strategy` | `page.locator(...)` | Explicit CSS selector. Use when bare target gets mis-heuristic'd. |
| `testid:plan-picker` | `page.get_by_test_id(...)` | When the page exposes `data-testid` — the most refactor-resistant target type. |
| `aria:Resolved wards` | `page.get_by_label(...)` (accessible-name semantics, NOT raw `aria-label`) | Picks up `aria-label`, `aria-labelledby`, `<label for>`, `<label>` wrapping. |
| `role:button` or `role:button:Sign in` | `page.get_by_role(...)` (optional `name=...`, `exact=True`) | Playwright's recommended PRIMARY selector. |
| `text:Resolved wards` | `page.get_by_text(...)` | Forces visible-text path. Use when the text starts with `#` / `.` etc. and would otherwise be heuristic-routed as a selector. |

```yaml
actions:
  - { kind: click, target: "testid:bulk-paste-cta" }
  - { kind: click, target: "role:button:Sign in" }
  - { kind: wait_for, target: "Resolved wards" }   # bare text — heuristic routes correctly
```

Anti-pattern: don't write a fragile `:nth-of-type` CSS path when a `testid:`
would survive a sidebar refactor. Same for `nth-child` chains pointing at
unstable structural positions — those targets silently break the day someone
reorders a list, and the failure mode is "this action was skipped" buried in
the run report, not a loud failure.

**`must_succeed: true` for critical actions.** Default behavior: a failed
action prints a warning and the recording continues — one bad step never
aborts the render. This is right for the common case, but it hides cascade
failures: if scene 2's "Create" button click silently misses, every later
scene records against the wrong page state and the report says "60/61 actions
ok" while the whole demo is wrong.

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

**`timeout_ms` for slow server-side actions.** Playwright's `locator.click`
waits for any navigation the click schedules INSIDE the same timeout, so a
`must_succeed` publish/submit click whose POST does real server work (minting
records before the redirect) can abort an otherwise healthy render at the
recorder's global 6s interaction timeout. Add a per-action `timeout_ms` to
loosen it for that one action (it takes `max(timeout_ms, preset)` — an
override can never tighten below the pace preset):

```yaml
actions:
  - { kind: click, target: 'css:button[type=submit]:has-text("Create Solicitation")',
      must_succeed: true, timeout_ms: 30000 }
```

Use it on the handful of navigating, state-minting clicks (publish, submit,
award); leave ordinary clicks on the default so a genuinely-broken target
still fails fast.

**`click_menu` for the click that closes a dropdown.** `click_menu` clicks an
item inside the currently-open dropdown / popover. Same target resolution as
`click`, but the recorder uses a shorter `menu_click_settle_ms` (~700ms)
because menus react faster than top-level buttons. Use it as the SECOND click
in a two-click "open menu → pick item" sequence — the spec verb signals
intent ("this click closes a menu, treat it as such") and gets the right
pacing. The verb is also a hint for graders reading the spec: this beat
shows a menu interaction, not two independent buttons.

```yaml
# open the menu, then pick an item inside it
- { kind: click, target: "Sort by" }              # opens the menu
- { kind: click_menu, target: "Date created" }    # picks the item; menu closes
```

When NOT to use it: don't use `click_menu` for the click that OPENS the menu
— that's a regular `click`. The verb is for the click that closes/dismisses
the menu, not the one that summons it.

**`note:` is a persistent annotation, not a YAML comment.** `note:` is a
human-readable annotation for one action. Unlike a YAML comment (`#`), it
persists into the artifact: shown in the recorder's per-action log, included
in the `--report` JSON, and read by judges as inline context for what the
step demonstrates. Treat it as documentation that ships — not as a scratchpad
comment.

```yaml
# ✓ good — explains the WHY for future readers + the judges
- { kind: click, target: "Create 10 plans", note: "fire the bulk POST for all 10 wards" }
- { kind: select, target: "#lga-kanwa", value: "1", note: "pick Madobi LGA for Kanwa (Madobi is candidate 1, Zurmi is candidate 0)" }

# ✗ unhelpful — explains nothing the action itself wouldn't tell you
- { kind: click, target: "Submit", note: "click submit" }
```

When to write a note: when the action's intent isn't obvious from the verb +
target alone — disambiguation choices (which of two near-identical options
was picked and why), ordering rationale (this click HAS to come before that
wait), what the click triggers downstream (the bulk POST, the worker
re-shuffle, the page redirect). A scratchpad-grade note ("click submit") is
worse than no note — it adds noise without adding signal.

**`kind: draw` for canvas / map drawing surfaces.** When the persona is
sketching a polygon on a map (Mapbox GL Draw, Leaflet.draw, MapLibre) or any
custom canvas / SVG drawing surface, no labelled-element click can express
the gesture. `kind: draw` records it: `target` is the canvas element,
`points` is a list of `[fx, fy]` fractions (0-1) within its box (the cursor
clicks each vertex, then double-clicks to close), and `tool` is the
draw-tool button to activate first.

The `tool` field activates a draw tool via a **coordinate mouse-click
instead of `Locator.click()`**. Most map / canvas drawing libraries render
their tool palette as tiny absolutely-positioned buttons that Playwright's
actionability check rejects (the receiving-events probe times out on a
24×24 control that overlaps the canvas). Coordinate-clicking bypasses the
check — we know the button is there because the spec author named it.
Pattern applies to Mapbox GL Draw, Leaflet.draw, MapLibre, and any custom
canvas / SVG drawing surface whose tool palette is built from small overlay
buttons.

```yaml
- { kind: draw, target: "css:#map", tool: "css:.mapbox-gl-draw_polygon", points: [[0.35,0.4],[0.6,0.4],[0.6,0.7],[0.35,0.7]] }
- { kind: draw, target: "css:#leaflet-map", tool: "css:.leaflet-draw-draw-polygon", points: [[0.2,0.2],[0.8,0.2],[0.8,0.8],[0.2,0.8]] }
- { kind: draw, target: "css:#custom-canvas", tool: "testid:rectangle-tool", points: [[0.1,0.1],[0.9,0.9]] }
```

**`kind: map_click` for clicking a NAMED map feature.** When the persona clicks a
specific *labelled* polygon on the main map — a ward, district, or any feature the
app's own `map.on('click', LAYER, …)` handler turns into a selection — `draw` is the
wrong verb (that sketches a new shape) and `click` can't reach it (a polygon is a
feature inside the canvas, not a DOM node). `map_click` clicks it by name: it finds
the Mapbox map in the page (`window.__review.map` for the microplans editor, else any
map-shaped global), looks up the feature whose `name` property equals `target` on the
boundary FILL layer (falling back to the SOURCE when the feature is loaded but not
currently painted), computes a point guaranteed to lie **inside** the polygon (a
concave-safe interior point, not a naive centroid that can fall outside an L-shaped
ward), `map.project()`s it to screen pixels, and dispatches a **real** cursor click
there — so the app's click handler fires and the boundary is added, exactly as if a
person clicked the ward. `target` is the feature name (`${var}` substitution applies);
`layer` / `source` override the microplans defaults (`mp-admin-fill` / `mp-admin`) for
other maps. Set `must_succeed: true` when the rest of the scene depends on the
boundary being added — the recorder then aborts cleanly if the named feature can't be
resolved, instead of silently clicking empty canvas.

```yaml
# microplans editor: click the Attakar ward polygon to add it as the intervention arm
- { kind: click, target: "css:#btn-area-admin", note: "ensure the Boundaries layer is on" }
- { kind: map_click, target: "Attakar", must_succeed: true, note: "click the ward on the map → auto-adds" }
# another map: override layer/source for a non-microplans Mapbox map
- { kind: map_click, target: "District 7", layer: "districts-fill", source: "districts" }
```

**Per-scene viewport override.** Most specs render at one viewport (the
spec-level `video_viewport_width` / `video_viewport_height`, defaults
1280×720). When one scene needs a wider canvas (a dense plan-review page with
a map + side metrics + a table that wraps awkwardly at 1280), set
`viewport: {width, height}` on that scene only — it appears in the spec
example at the top of this file. The recorder calls `page.set_viewport_size()`
before the scene's goto and restores the spec-level default after
`final_hold_ms`. Important constraint: **the recorded mp4 frame size stays
fixed at the spec-level resolution** — Playwright's `record_video_size` is
set at context creation and cannot change mid-stream. Per-scene viewport
changes the page LAYOUT (CSS pixels) only; the wider logical viewport is
letterboxed into the fixed mp4 frame. This is genuinely useful — the layout
breathes — but don't expect the dense scene to be sharper in the video. For
per-scene resolution, use multiple render passes + ffmpeg concat (out of
scope for normal authoring).

**Create-and-carry-through flows.** A scene may omit its `url` to *continue on the
page the previous scene's actions navigated to*. This is how a narrative can CREATE
an entity in one scene (e.g. click "Create plan" → the app routes to the new
record's page) and operate on it in later scenes whose URL can't be known ahead of
time. Give scene 1 a `url` (the entry point); leave `url` empty on continue-scenes
and drive them with `actions` (use a `goto` action to jump to a known static page
like a workspace, and click-by-name to return to the created record). In a
continue-scene a leading `goto` IS meaningful (it's a deliberate page change, not
a redundancy) — the strip rule only fires when both `url:` AND a leading `goto` to
the same path are set.

### Recording time & dead space (the timing model — AUTHORITATIVE)

This is the one map of every mechanism that decides what ends up on film and
for how long. The recorder films **one continuous take**: every millisecond
between the recorded page opening and the last scene's final hold is footage.
Dead space — a frozen frame while something loads, a blind hold stacked on a
real readiness signal — is therefore an *authoring + flags* problem, and every
fix lives in this section. (Code-side map: the module comment in
`scripts/walkthrough/_lib/config.py` points back here.)

**What films vs what doesn't:**

| Phase | On camera? | Notes |
| --- | --- | --- |
| `setup:` command (synthetic generator) | **No** | Runs before any browser opens. |
| Pre-warm pass (`prewarm: true` / `--prewarm`) | **No** | Separate non-recorded context; runs after setup + auth, before the recorded context exists. |
| URL auth nav (`auth: type: url`) | **Yes** | Happens on the recorded page before scene 1 (counts toward scene 1's start offset). Cookie/storage-state auth is free — seeded at context creation. |
| Scene navs + `goto_settle_ms`, `initial_hold_ms`, action glides/dwells/settles, `wait_for` polling time, `hold` actions, end-of-scene hold | **Yes** | Every wait an author declares (or a default implies) is film. |
| Snapshot scroll-to-top bounce | **Yes** | Fires at the scene cut, masked by the crossfade. |

**Every dead-space mechanism and when to use it:**

| Mechanism | Dead space it removes | When to use |
| --- | --- | --- |
| `prewarm: true` (spec) / `--prewarm` · `--no-prewarm` (CLI; CLI wins) | Cold-cache waits filmed as frozen frames — a 15s first-hit page render, a 7.5s remote-image cold fetch. The pass visits each **unique resolved** scene URL once off camera (continuation scenes skipped, `${var}` already substituted; capture-bound URLs that still hold an unresolved `${var}` are skipped — their id is minted on camera later), with the same cookies/storage_state as the recording. Best-effort: a failing page is logged + recorded in the report's `prewarm` provenance (`{pages, duration_seconds, failures}`), never fatal. | Any spec whose app has cold-start cost (cold page caches, remote images, slow first render). Costs a few off-camera seconds; harmless otherwise. |
| Leading `wait_for` (automatic) | The recorder skips `goto_settle_ms` + `initial_hold_ms` when a scene's first action is `wait_for` — the wait IS the settle; blind holds on top are pure dead air. | Every scene that opens on a page that takes a moment. This is the default authoring pattern. |
| No-nav scene (omit `url:`) (automatic) | `initial_hold_ms` skipped — there's no page-load to settle for. | Continuation scenes. |
| `--skip-same-url` | Re-navigation between same-URL scenes, which films a reload AND wipes JS state. | Specs with continue-on-page flows. |
| `--skip-empty-scenes` | The minimum dwell on a static page for narrative-only scenes (no `actions`). The deck still shows them as title-card slides. | Specs with an action-less narrative back half. |
| Redundant-goto strip (automatic) | The visible reload ~1-2s into a scene authored with both `url:` and a leading `goto` to the same path. | Safety net — still author one or the other, not both. |
| Crossfade (on by default; `video_recorder_config: {crossfade: false}`) | The white navigation flash between scenes. | Leave on; disable only when debugging raw page state. |

**Pacing presets** (`video_pace: fast | medium | slow`, default `fast`) set the
global tempo — scene holds, cursor glide dwells, per-keystroke typing delay,
post-click settles:

| Pace   | Initial hold | Final hold | Typing delay | Post-click settle |
| ------ | ------------ | ---------- | ------------ | ----------------- |
| fast   | 0.8s         | 0.5s       | 20ms/key     | 0.4s              |
| medium | 1.5s         | 1.0s       | 45ms/key     | 0.9s              |
| slow   | 2.5s         | 1.5s       | 80ms/key     | 1.4s              |

Any individual knob can be overridden via `video_recorder_config: {<field>: <ms>}`
(see `scripts/walkthrough/_lib/config.py` for the full field list).

**Per-scene `pace: teach | flow`** is a tempo modifier layered on top of the
global preset, set on an individual scene (not the whole video like `video_pace`):

- **`teach`** (default — absent `pace:` is `teach`) — explain the mechanic. Full
  read-time pacing, because the viewer is meeting this UI/concept for the first
  time. Identical to behavior before this field existed; every existing spec is
  all-teach and records byte-for-byte the same.
- **`flow`** — the feature is already established and this beat just shows
  **continuity** (navigate there, glance, move on). The recorder compresses the
  scene: blind holds/settles clamped to a ~600ms ceiling, the post-nav settle
  cut to ~200ms (the crossfade already hides the flash), explicit `hold` actions
  capped at ~700ms, and the cursor ~1.8x faster. Pair it with terse or no
  narration — a flow scene is a transition, not a lesson.

Use `flow` for the connective beats of a long demo (returning to a list,
hopping to an already-shown screen) and `teach` (or just omit it) for the beats
that introduce something. The compression is per-scene — a `flow` scene never
affects the `teach` scene before or after it. The pace→durations resolution
lives in `apply_scene_pace` (`scripts/walkthrough/_lib/config.py`), and the
named ceiling/speedup constants (`FLOW_HOLD_CEILING_MS`, `FLOW_GOTO_SETTLE_MS`,
`HOLD_ACTION_FLOW_CEILING_MS`, `FLOW_CURSOR_SPEEDUP`) are documented there.

**The dwell hierarchy — which knob to reach for when you want the viewer to
sit with a screen.** Three knobs hold a frame on purpose; use them in this
precedence order:

1. **`hold` actions** (recommended) — explicit mid-scene dwells, placed exactly
   where the moment is: `{ kind: hold, seconds: 3, note: "let the KPI sink in" }`.
   This is the cinematic-dwell tool; everything else is plumbing.
2. **`video_hold_seconds: N`** (legacy per-scene override) — replaces the
   end-of-scene hold (`final_hold_ms`) for that one scene. Kept for existing
   specs; prefer a `hold` action, which says *where* in the scene the dwell
   belongs. (History: this knob was silently dead between the orchestrator
   refactor and 0.2.192 — it is consumed again, with exactly this meaning.)
3. **`final_hold_ms`** — the global end-of-scene floor from the pace preset /
   `video_recorder_config`. The baseline breath between scenes, not a styling
   tool.

Two knobs that look like dwells but aren't: `min_hold_ms` only floors the
*reported* per-scene seconds (the "~Ns of footage" accounting) — it does not
pad the film; `scroll_speed_px_s` is dead (unconsumed since the static-scene
scroll-pan fallback was removed) and retained only for back-compat.

### Data setup + `${var}` substitution (specs with a `setup:` block)

A spec backed by synthetic data declares its **synthetic generator** in the
`setup:` block (see YAML Spec Format). The recorder then runs
`setup.command` **before** opening any browser, parses the flat JSON at
`setup.outputs` (string/number values), and resolves `${var}` placeholders in
each scene's `url` and every action's `target` / `value` — at render time,
never mutating the spec file on disk. So a spec writes
`url: "/workflow/runs/${run_id}/"` instead of hardcoding an ID that goes
stale on every reseed.

- **`rerun: per_render` (the default) reruns the generator before EVERY
  render.** This is required for demos that MUTATE state during recording —
  e.g. a manager-flow scene that creates a real audit + task. Re-rendering
  without a reseed films the wrong UI ("View Audit" instead of "Create
  Audit"). `rerun: once` skips the command when the outputs file already
  exists — only for expensive, idempotent generators whose data survives
  re-renders.
- **cwd contract:** the command runs from the git toplevel containing the
  *spec file* (`git rev-parse --show-toplevel` from the spec's directory;
  the spec's own directory outside a git repo) — write it repo-root-relative,
  exactly as a human would run it. `outputs` is resolved against the same
  root.
- **Failures abort the render loudly** — nonzero exit, timeout
  (`timeout_seconds`, default 1200), a missing/malformed outputs file, or an
  unresolved `${...}` placeholder (the error lists the missing variable and
  the available keys). A `${...}` placeholder in a spec with **no** `setup:`
  block is also a hard error: nothing could ever resolve it.
- **`--skip-setup` escape hatch:** skips the command but still loads the
  outputs file — for fast re-renders when you KNOW the data is fresh.
  **State-mutating demos must not use it** (their recording changes the
  world; every render needs a reseed).
- **Provenance:** the resolved variables + command + exit code + duration are
  copied into the RunReport (`--report`), and with `--snapshots` a
  `setup-vars.json` is written into the snapshots dir — the data a film was
  made on is part of the run's evidence chain.

### Capture + late binding (`${var}` minted ON CAMERA)

`setup.outputs` only knows ids minted **before** the render starts. To film a
**fresh end-to-end lifecycle each render** — create an entity ON CAMERA, then
use its real id in LATER scenes, with no fixed IDs and no per-render state
resets — use a `capture` action. It reads an id off the live page mid-render
into a `${var}` that every LATER scene/action resolves.

```yaml
scenes:
  - title: "Author publishes a solicitation"
    url: "/solicitations/new/"
    actions:
      - { kind: fill, target: "Title", value: "Q3 outreach" }
      - { kind: click, target: "Publish", note: "creates the record → redirects to its page" }
      - kind: capture
        var: solicitation_id           # binds ${solicitation_id} for ALL later scenes
        source: url                     # read the current page URL
        pattern: '/solicitations/(\d+)/'  # capture GROUP 1 is the value (REQUIRED for source: url)
        # must_succeed defaults TRUE for capture — a later ${var} that never bound
        # would film a literal "/solicitations/${solicitation_id}/" URL.
  - title: "Reviewer opens it"
    url: "/solicitations/${solicitation_id}/review/"   # ← resolves to the freshly-minted id
    actions:
      - { kind: click, target: "Award ${solicitation_id}" }   # also late-bound
```

**`source: element`** reads from a DOM node instead of the URL:

```yaml
      - kind: capture
        var: response_id
        source: element
        target: 'css:tr:first-child a.view'   # same target syntax as click
        attr: href                            # read this attribute; omit attr ⇒ read element text
        pattern: 'response/(\d+)/'            # OPTIONAL here; omit ⇒ whole trimmed attr/text
```

Semantics:

- **Late binding.** A scene's `url` and each action's `target` / `value` are
  resolved **lazily, right before they execute**, against a live `vars` map
  seeded from `setup.outputs` and extended by each `capture`. So a var captured
  in scene 1 flows into scene 5. (Up-front substitution still resolves every
  var known at setup time, so pre-warm + early scenes are unaffected.)
- **A scene's `url` can't use its OWN capture** — the url resolves at scene
  start, before any of that scene's actions run. Capture in scene N, use the
  var in scene N's later actions or in scene N+1's url.
- **Value handling.** The captured value is trimmed. `source: url` REQUIRES a
  `pattern` (group 1). For `source: element`, `pattern` is optional — with it,
  group 1; without it, the whole trimmed attr/text. A pattern with no group, a
  non-match, or an empty result is a capture FAILURE.
- **`must_succeed` defaults TRUE for capture** (override per-action to make it
  best-effort). A failed required capture aborts the render loudly.
- **Override rule.** A captured var overrides nothing from `setup.outputs`
  unless names collide — then the captured value wins and the recorder warns
  (the on-camera value is the fresher truth).
- **Validation is order-aware** (`ddd-spec-qa`): a `${var}` is valid iff a
  setup output OR a `capture` in an EARLIER scene provides it. A var referenced
  before anything binds it is a hard error.
- **Pre-warm skips capture-bound URLs** — a URL still holding a `${var}` (its id
  isn't minted until later) can't be warmed, so it's skipped (films cold, fine).
- **Run report.** Each capture is recorded (`kind=capture, var, ok, value`) and
  printed in the run summary, so a failed/empty capture is debuggable.

### Run

> **Dependency note.** The engine (`record_video.py`) needs **playwright +
> a chromium download** — a heavy dependency that `/canopy:setup` does
> **not** install. Because the engine is now the single source of the
> manifest + screenshots (not just the mp4), this dependency is required
> for the whole flow, not only for video. On a portable install without
> it, the script exits with the exact install command. Run it once against
> the canopy runtime (resolved via `scripts/canopy-runtime.sh`):
> `pip install 'playwright>=1.40' && python -m
> playwright install chromium` (or `pip install -e
> '<canopy-runtime>[browser]'`). The deck generator
> (`generate_presentation.py`) itself stays pure stdlib.

Export live browse cookies so the engine inherits the auth you established
during pre-flight (cookie-based auth seeds at context creation; specs with
URL/command auth let the engine handle login itself), then invoke the
script. This single call writes the mp4 (`--output`), the per-scene
screenshots + page-text JSON (`--snapshots`), and the manifest
(`--manifest`):

```bash
$B cookies > /tmp/walkthrough-cookies-<name>.json

_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
REC="$CANOPY_ROOT/scripts/walkthrough/record_video.py"

uv run --project "$CANOPY_ROOT" python "$REC" \
  --spec docs/walkthroughs/<name>.yaml \
  --output screenshots/walkthroughs/<name>.mp4 \
  --snapshots screenshots/walkthroughs/<name>/ \
  --manifest /tmp/walkthrough-run-data.json \
  --cookies /tmp/walkthrough-cookies-<name>.json
```

Add `--scene <selector>` to render (and tag the manifest for) only a
subset — the engine preserves original spec indices (see **Scene filter**).
The manifest's per-scene `screenshot_path` / `page_text_path` point into
the `--snapshots` dir; the scoring step reads those frames, and **Score the
captured frames** merges each scene's `ai_evaluation` back into the
manifest before the deck is generated.

**Pre-warm:** the recorder honors the spec's `prewarm:` value automatically;
`--prewarm` / `--no-prewarm` override it per invocation (CLI wins). See
"Recording time & dead space" above for semantics + provenance.

**Auth alternative — `--storage-state`:** if the `browse cookies` export isn't
available or isn't sticking across contexts, pass a Playwright `storage_state`
JSON instead: `--storage-state /tmp/state-<name>.json`. It's applied at context
creation (so it carries localStorage/origins, not just cookies) and seeds the
session before the first scene navigates. Mutually exclusive with `--cookies`
(storage_state wins); when set, the spec's URL/command auth fallback is skipped.

Requires `playwright>=1.40` with Chromium installed (`pip install
'playwright>=1.40' && python -m playwright install chromium`, or
`pip install -e '<canopy>[browser]'`) and `ffmpeg` on PATH. The script
exits with a clear error if either is missing.

Report the mp4 path to the user alongside the HTML deck path. The video
is silent by design — narration / captions are expected to be added by
post-processing tooling outside this skill.

## Verify Deck (MANDATORY — do not skip)

After generating the HTML deck, you MUST verify your own output before presenting
it to the user. The deck may contain problems invisible during scoring:

1. **Open the deck in browse:**
   ```bash
   $B goto file://$PWD/screenshots/walkthroughs/<name>.html
   ```

2. **Navigate to each scene slide** and take a viewport screenshot. For each slide:
   - Read the screenshot with the Read tool
   - Does the embedded screenshot show the right page? (not a different app, not a stale capture)
   - Is the content styled? (not raw unstyled HTML from a server missing CSS)
   - Are there loading spinners, black screens, or blank areas?
   - Does the score shown match what the screenshot actually looks like?

3. **Flag mismatches.** If any slide's screenshot doesn't match the score you gave it
   during scoring, update the score and note the discrepancy. Common problems:
   - Capture from wrong server (worktree without built CSS)
   - Capture before page finished loading (spinners visible)
   - Capture shows a different page than expected
   - Screenshot is absurdly tall or blank

4. **Report to the user** with confidence level:
   - "Deck verified — all slides match their scores" (if everything checks out)
   - "Deck has issues — slides {n, m} need re-rendering: {reasons}" (if problems found)

**Do NOT tell the user the deck is ready without verifying it yourself.**
The user should never be the first person to catch a bad screenshot.

## Offer to Share

After the deck is verified, **ask once** whether to upload it to canopy-web for
sharing. Don't auto-upload — the user picks per run, and many runs are dev
churn that shouldn't get a share URL.

Prompt the user with the deck path and two options. If they want to share,
invoke `/canopy:walkthrough-share <path>` (the slash command — not bash).
Pass `--public` if they want a shareable link, otherwise leave it private
(dimagi-only). If `screenshots/walkthroughs/<name>.mp4` was also produced,
the user can run `/canopy:walkthrough-share` on that path too — videos are a
separate upload.

If the upload skill fails because the upload token isn't configured, surface
the error verbatim and point the user at `/canopy:walkthrough-share`'s
"First-time setup" section. Don't try to debug auth from inside this skill.

## Generate Mode

When invoked as `/walkthrough generate`:

1. Ask the user what feature or demo they want to walk through.
2. Check for existing design docs:
   ```bash
   grep -rl "Demo Narrative\|walkthrough\|demo" docs/plans/ docs/designs/ 2>/dev/null
   ```
3. If found, use it as the starting point. If not, ask the user to describe the scenes.
4. For each scene, ask: What persona? What should be shown? What makes it impressive?
   Does it have AI output to evaluate?
5. Write the YAML to `docs/walkthroughs/<name>.yaml`.
6. Offer to execute it immediately.

## Efficient Reruns

When rerunning after fixes, don't re-render all scenes:

- **Selective re-render:** If 2 of 8 scenes need fixing after code changes, re-render
  only those with `--scene 4,7` (one capture pass over the subset). The engine
  preserves original spec indices, so the re-rendered slides stay comparable to the
  full run.
- **Merge, don't rebuild:** Re-scoring a re-rendered scene only overwrites that
  scene's `ai_evaluation` in the manifest — the engine owns every other key, so a
  partial re-render + merge keeps the rest of the manifest intact.
- **Incremental fixes:** Fix the lowest-scoring scenes first. Each fix-and-re-render
  cycle should target the biggest Demo Readiness blockers.
