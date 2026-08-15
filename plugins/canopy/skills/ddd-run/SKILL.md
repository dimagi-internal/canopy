---
name: ddd-run
description: |
  Render + dual-verdict run (SP4). Orchestrates the full render-then-judge
  sequence for a DDD run: gates on ddd-spec-qa, invokes canopy:walkthrough to
  render the unified_spec into per-scene screenshots + captured page text, then
  dispatches the concept judge (ddd-concept-eval → verdict-concept.yaml +
  design_findings.json) and user-artifact judge (canopy:visual-judge with
  audience="feature user" → verdict-user.yaml) in parallel. Assembles both
  verdicts into run_state.yaml via run_pipeline.assemble_run_state, reports
  convergence via run_pipeline.compute_convergence, and prints the two
  overall_scores + top findings.
  Use when asked to "run the ddd walkthrough", "render and judge", or "run SP4".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# DDD Run — Render + Dual-Verdict

Drives the full render-then-judge sequence for a single DDD iteration:
gate → render → judge (concept + user-artifact in parallel) → assemble → report.

## Inputs

- **`run_id`** — an existing run identifier from `scripts.ddd.runstate.new_run`.
  The run directory must already exist at `<ddd_dir>/runs/<run_id>/`.
- **`unified_spec`** — path to the spec.  This IS a runnable canopy walkthrough
  spec — the render step drives it directly via `canopy:walkthrough`.  Accepts a
  legacy unified `<slug>.yaml` **or** a `<slug>.recipe.yaml`, which the loader
  composes with its `<slug>.narrative.lock.json` before rendering.

  **Before rendering a migrated narrative, check the lock is intact:**
  ```bash
  python -m scripts.ddd.check_locks docs/walkthroughs
  ```
  A failure means a lock was hand-edited or a scene exists on one side only.
  Fix it with `narrative pull <slug> docs/walkthroughs` — never by editing the
  lock.
- **`why_brief`** — path to `why_brief.yaml` (needed by the concept judge for
  provenance cross-checks).
- **`--scene <selector>`** *(optional)* — render only a subset of scenes.
  Same selector grammar as `/canopy:walkthrough --scene`. Use when the
  iteration that just landed only touched one scene's feature and a
  full-spec run would mostly re-render unchanged scenes. The full
  dual-judge rubric still applies — only the render step is filtered.
  When set, `run_state.yaml` carries `scenes_run` (the original 1-based
  spec indices actually rendered) and `scene_filter` (the raw selector)
  so convergence reports and upload checks can tell partial runs from
  full ones. **Upload (`/canopy:ddd-upload`) requires a full run** —
  a partial run cannot be uploaded as a feature package.

## Procedure

### Step 1 — Gate: spec QA

Before rendering, verify the spec is structurally sound (the script lives in the
canopy runtime):

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
# pass the file arg as an absolute path (resolved before the cd):
UNIFIED_SPEC_ABS="$(realpath <unified_spec>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.spec_qa "$UNIFIED_SPEC_ABS")
```

If the exit code is non-zero (verdict: fail), stop immediately and tell the user:

```
ddd-run: BLOCKED — ddd-spec-qa must pass before rendering.
  blocking_reason: <spec_qa blocking_reason>
  Fix the structural issues, re-run /canopy:ddd-spec-qa, then retry /canopy:ddd-run.
```

Do NOT render a spec that fails the QA gate.

### Step 1a — Preflight: resolve every selector before recording

A render is ninety seconds of browser plus a reseed plus an encode, and a scene
whose target does not resolve still produces a screenshot — a plausible-looking
frame the judges then score, reporting findings about the wrong thing. The first
real render of a four-scene narrative went 15/22 actions ok, and five of the
seven failures were knowable without recording anything.

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC_ABS="$(realpath <unified_spec>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.recipe_preflight "$SPEC_ABS")
```

Exit 1 means one or more targets will not resolve against the live app. **Fix
the recipe and re-run preflight — do not render.** It walks scenes in order in
one browser and applies state-changing actions as it goes, so it catches the
class the first loop lost most time to: a tab switch a later scene never undid,
and a modal left open whose backdrop blocked everything after it.

### Step 1b — Refresh the narrative from canopy-web (one-way; no pause)

canopy-web owns the story. Before rendering, refresh the generated narrative
lock so the run films the user's current narration:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC_ABS="$(realpath <unified_spec>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.narrative pull "<narrative-slug>" "$(dirname "$SPEC_ABS")")
```

Returns `{"action": "pulled", "version": N, "lock_path": ..., "scenes": n}`.

> **There is no conflict case, and no `--force`.** The story has exactly one
> writer (canopy-web) and the lock is a generated cache of it, so a pull can only
> fast-forward. This replaced the old reconcile-both-directions command, which
> existed solely because two writers shared one file — deleted along with the
> content hash, the merge, and its conflict outcome. Do not hand-edit a
> `.narrative.lock.json`;
> `scripts.ddd.check_locks` fails the build if a recipe field appears in one.

**A narrative that does not exist on canopy-web yet** (a first-ever draft) has
nothing to pull — post it via `/canopy:ddd-narrative-review` first, then split the
draft into `<slug>.recipe.yaml` + its lock. See that skill's "After the gate
resolves".

The only durable human approval in the routine loop remains the
**`external_release`** gate at upload.

### Step 2 — Render: invoke the canopy walkthrough engine

Invoke `canopy:walkthrough` (or the equivalent Skill tool call) against
`<unified_spec>` to drive the live product and produce:

- `scene_<N>.png` — per-scene screenshot for each scene in the spec.
- `scene_<N>_page_text.json` — captured page text (`$B text` output) per scene.
- The walkthrough JSON sidecar into the run dir.

If invoked with `--scene <selector>`, pass it through verbatim to
`canopy:walkthrough` so only the matching scenes get rendered. The skill
preserves original scene_index in filenames (so `--scene 2` produces
`scene_2.png` and `scene_2_page_text.json`, not `scene_1.png`), keeping
artifact paths stable across partial and full runs.

All output lands in the run directory (`<ddd_dir>/runs/<run_id>/`).

> **Live labs note:** For live Connect Labs features, the render step also
> requires the connect-labs recorder rig, a freshness guard (confirming the
> deployed code is current), and a seeded demo.  Those are wired in the
> rooftop run — not here.  For dry runs and unit tests, the render step is
> exercised separately.

> **Multi-persona specs (`auth.type: form`):** the recorder signs each persona in
> off camera before recording and swaps identity per scene from the scene's
> `persona:` field — nothing to pass on the CLI. A switch forces that scene's
> navigation even under `--skip-same-url`, so a scene that changes persona cannot
> also be a continuation scene. See walkthrough SKILL § "Off-camera personas".

#### Recording CLI flag matrix

The `canopy:walkthrough` skill drives `scripts/walkthrough/record_video.py`
under the hood. Several CLI flags are author-controlled and matter for a DDD
run specifically — the defaults are tuned for one-off `/canopy:walkthrough`
calls, not for the dual-judge pipeline. When invoking the recorder, pass the
flags below.

| Flag | When to pass | Why |
| --- | --- | --- |
| `--cookies <path>` | Always when the spec's `auth: type: session` | Without this the recorder hits the login wall. |
| `--snapshots <dir>` | **Always** for DDD runs | Concept-eval needs per-scene PNG + page_text JSON inputs. The visual-judge can't dual-judge without them. |
| `--snapshot-empty-scenes` | Almost never | Empty scenes (narrative-only back-halves) have no meaningful state to snapshot. |
| `--report <path>` | Always | The accumulator JSON tells you which actions silently failed (and which `must_succeed` ones aborted). |
| `--manifest <path>` | **Always** for DDD runs | Writes the canonical render manifest (`walkthrough-run-data.json`) — the single artifact the deck (`generate_presentation`), the external-systems links, and `assemble_run_state` read. Without it `ddd-upload` raises `DeckMissingError`. Point it at `<run_dir>/walkthrough-run-data.json`. (Emitted even on a partial render, so a failed scene still leaves an inspectable manifest of what rendered.) |
| `--skip-empty-scenes` | When the spec has narrative-only back-half scenes (no `actions`) | The mp4 doesn't waste `min_hold_ms` on identical static pages. Deck slides still cover them. |
| `--skip-same-url` | When the spec uses continue-scene patterns (scenes that operate on the previous scene's URL) | Avoids re-navs that wipe JS state between scenes. |
| `--capture-action-frames` | **Always** for DDD runs | For each scene with an effecting action, also writes `scene_<N>_before.png` (the action loop's starting line). The dual-judge passes the `{before, after}` pair to `canopy:visual-judge` so it can judge the state CHANGE, not just the end-frame — closing the single-still-frame blind spot. Single-frame scenes (no effecting action) are unaffected. |
| `--input <run.json>` | Only for `--scene` partial runs (when reusing a previous walkthrough's capture set) | Without this, the spec is the only source of truth. |
| `--skip-setup` | **Never in the iterate loop** | Specs with a `setup:` block run their synthetic generator before every render (`rerun: per_render`) — that reseed is load-bearing for state-mutating demos (a scene that creates an audit must find no audit on the next take). `--skip-setup` is a human escape hatch for one-off re-renders on known-fresh, non-mutating data; the orchestrator must not pass it. |
| `--prewarm` / `--no-prewarm` | Usually neither — the spec's `prewarm:` value is the right default and the recorder honors it automatically (CLI overrides per invocation, CLI wins) | The pre-warm pass visits each unique resolved scene URL once in a NON-recorded context before filming, so cold caches (first-hit page renders, remote image fetches) are paid off camera instead of as frozen frames mid-scene. Best-effort: failures land in `run-report.json` (`prewarm` key: `{pages, duration_seconds, failures}`), never abort the render. Full model: walkthrough SKILL § "Recording time & dead space". |

**DDD orchestrator default flag set** — what `/canopy:ddd-run` should pass to
`record_video.py`:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
REC="$DDD_REPO/scripts/walkthrough/record_video.py"
uv run --project "$DDD_REPO" python "$REC" \
  --spec "<unified_spec>" \
  --output "<run_dir>/iter${state.iteration}_clip.mp4" \
  --cookies "<session-cookies>" \
  --snapshots "<run_dir>/snapshots/" \
  --report "<run_dir>/run-report.json" \
  --manifest "<run_dir>/walkthrough-run-data.json" \
  --skip-empty-scenes \
  --skip-same-url \
  --capture-action-frames \
  --ddd-orchestrated
```

`--manifest` WRITES the manifest (the deck/links/run-state all read it; upload
raises `DeckMissingError` without it). Do NOT pass `--input` on a first render —
`--input` only *consumes* an existing capture and is for `--scene` partial re-runs
that reuse a prior walkthrough's capture set (see the flag matrix above).

`--ddd-orchestrated` is **required** here: the recorder refuses to write into a
`<runs-root>/<run_id>/` directory without it (the hand-drive guard). That
guard exists because rendering a run by hand — calling `record_video.py` or
dispatching judge sub-agents directly instead of going through THIS skill —
leaves `run_state.yaml` with no assembled verdict, so the run looks stale/done,
can't be resumed cleanly, and `ddd-upload` has nothing converged to publish.
`/canopy:ddd-run` is the only path that should pass this flag.

**Data setup runs inside the recorder — don't pre-run it here.** When the spec
declares a `setup:` block (the data-setup contract — see ddd-spec Step 5), the
recorder itself runs the synthetic generator before opening any browser,
resolves `${var}` placeholders in scene URLs / action targets from its outputs
JSON, and aborts loudly on failure. The orchestrator's job is only to NOT
defeat it: never pass `--skip-setup` in the iterate loop (re-renders of
state-mutating demos need the reseed), and treat a setup failure as a blocked
render, not a graded run. Provenance lands in `run-report.json` (`setup` key:
command, exit code, duration, resolved variables) and in
`<run_dir>/snapshots/setup-vars.json` — part of the run's evidence chain.

**Late binding (`capture`) needs nothing from the orchestrator.** A spec may
mint a `${var}` ON CAMERA with a `capture` action (create an entity mid-render,
read its id off the page, use it in later scenes — a fresh lifecycle each render
with no fixed IDs). The recorder resolves those vars lazily at runtime; pre-warm
skips capture-bound URLs automatically. Each capture is recorded in
`run-report.json` (`kind=capture, var, ok, value`) and printed in the run
summary — a failed required capture aborts the render like any `must_succeed`
action, so treat it as a blocked render. See the walkthrough SKILL § "Capture +
late binding".

After the recorder exits, scan `<run_dir>/run-report.json` for non-zero
`failed` counts before letting the judges run. A scene whose
`must_succeed: true` action failed will surface here as an `ok: false`
result with a clear `error_kind` — that's the orchestrator's signal to
either retry the render or treat the scene as blocked, not to grade a
half-rendered scene.

### Step 2b — Upload artifacts to canopy-web (auto, every iteration)

Immediately after render, BEFORE the judges run, generate the per-iteration
HTML deck and upload it to canopy-web so every downstream consumer has a
hosted URL to reference. This step exists so the orchestrator never has to
do a one-off upload at surface-time, and so surfaced findings can include
hosted links the user can open from any device (per the pause-policy
artifact-link contract in `agents/ddd.md`).

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
# Generate the deck for this iteration. Same generator
# /canopy:walkthrough uses; scene_index + scene_total + scenes_run +
# scene_filter all flow through and the generator emits id="scene-<N>"
# anchors on every scene slide for deep-linking.
GEN="$DDD_REPO/scripts/walkthrough/generate_presentation.py"
ITER_DECK="<run_dir>/iter${state.iteration}_deck.html"
uv run --project "$DDD_REPO" python "$GEN" \
  --input "<run_dir>/walkthrough-run-data.json" \
  --output "$ITER_DECK"

# Upload to canopy-web via /canopy:walkthrough-share's upload script.
# --public mints a share token so the URL works for anyone with the link
# (the user reading the surfaced finding on their phone). If you need
# dimagi-OAuth-gated only, drop --public.
#
# --run-id / --feature / --role group this artifact under its DDD run so it
# packages in canopy-web's /ddd views. Pass state.run_id and state.feature; the
# per-iteration deck is role=deck, the clip is role=clip.
UPLOAD="$DDD_REPO/scripts/walkthrough-share/upload.py"
DECK_URL=$(uv run --project "$DDD_REPO" python "$UPLOAD" "$ITER_DECK" \
  --title "<unified_spec.name> iter${state.iteration}" \
  --run-id "<state.run_id>" --feature "<state.feature>" --role deck \
  --public 2>&1 | grep -oE 'https://[^ ]*' | tail -1)
```

If the recorded mp4 exists in the run dir, upload that too — and attach the
**companion links** the `/w/<id>` viewer renders so someone watching the clip
can act on it: jump back to the narrative, open the still-frame deck, and click
into the app pages the demo visited.

- `--narrative-url` — the narrative-review URL the gate stamped on
  `state.narrative_review_url` ("Back to the narrative").
- `--companion-url "$DECK_URL"` — the still-frame deck uploaded just above
  (labelled "Still-frame walkthrough" automatically for a video).
- `--spec "<unified_spec>"` — derives one "Explore in the app" reference link
  per scene `url` (label = scene title, deduped). The clip's destinations,
  clickable and live.

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
UPLOAD="$DDD_REPO/scripts/walkthrough-share/upload.py"
ITER_CLIP="<run_dir>/iter${state.iteration}_clip.mp4"  # if recorded
if [ -f "$ITER_CLIP" ]; then
  NARRATIVE_URL=$(PYTHONPATH="$DDD_REPO" uv run --project "$DDD_REPO" python -c "from scripts.ddd.runstate import load; print(load('$run_id').narrative_review_url or '')")
  CLIP_ARGS=( "$ITER_CLIP" --public
    --title "<unified_spec.name> iter${state.iteration} (video)"
    --run-id "<state.run_id>" --feature "<state.feature>" --role clip
    --spec "<unified_spec>" )
  [ -n "$DECK_URL" ] && CLIP_ARGS+=( --companion-url "$DECK_URL" )
  [ -n "$NARRATIVE_URL" ] && CLIP_ARGS+=( --narrative-url "$NARRATIVE_URL" )
  CLIP_URL=$(uv run --project "$DDD_REPO" python "$UPLOAD" "${CLIP_ARGS[@]}" 2>&1 | grep -oE 'https://[^ ]*' | tail -1)
fi
```

Stamp the URLs onto `run_state.yaml` so every downstream step (judges,
surfaced findings, the digest) can read them without re-running the upload:

```python
from scripts.ddd.runstate import load, save
state = load(run_id)
state.iteration_decks[state.iteration] = DECK_URL
if CLIP_URL:
    state.iteration_clips[state.iteration] = CLIP_URL
save(state)
```

**On upload failure:** log the failure to `<run_dir>/upload-errors.md` with
the iteration number and reason, leave the `iteration_decks` entry unset,
and CONTINUE to the judge step. The judges still score from the local
PNGs; the orchestrator's surface step will then fall back to a verbal
description per the artifact-link-or-verbal-description rule in
`agents/ddd.md`. **NEVER** fall back to `file://` paths.

**Deep-linking a scene:** consumers append `#scene-<N>` to the hosted
deck URL, where N is the original spec scene index. Example:
`<DECK_URL>#scene-2`. The deck generator emits stable anchors.

### Step 2c — Render pacing audit (deterministic, runs BEFORE the judges)

The LLM visual-judge scores one frozen frame per scene — it is structurally
**blind to time**: it cannot see a 4-second held-frame stall, a loading spinner
shown on camera, or a `wait_for` that timed out. Those are deterministic and
measurable from the final mp4 + the run-report, with no LLM and no variance. Run
the pacing audit on the rendered clip before dispatching the judges:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
(cd "$DDD_REPO" && uv run python -m scripts.ddd.render_pacing_audit \
  "<run_dir>/iter${state.iteration}_clip.mp4" "<run_dir>/run-report.json" "<unified_spec.name>")
```

It classifies the video's silent budget and prints a timestamped issue list:

- **RECORDING BUG** — a `must_succeed`/any action `ok:false` or a `wait_for`
  timeout in the run-report. **This BLOCKS the grade** — do NOT judge a broken
  take; treat the scene as blocked and re-render (or fix the spec) first. (Step 2
  already scans `failed` counts; this names the offending action.)
- **VIEWING ISSUE · dead-air** — a frozen + silent stretch mid-video > 1.5s (a
  held-frame stall the cap missed). Carry each as a render finding with a
  `#t=<int_seconds>` deep-link into the UPLOADED clip (Step 2b), so the reviewer
  jumps straight to the stall.
- **VIEWING ISSUE · silent-motion** — silent + moving footage > 3s (loading shown
  on camera, or narration too sparse for the on-screen activity).
- Intro/outro cards are auto-excluded (static+silent by design).

These complement the visual-judge: it scores whether each FRAME is good; the
pacing audit scores whether the TIME is well-used. Fold the dead-air /
silent-motion regions into the findings the orchestrator surfaces (same
`#t=`/`#scene-<N>` deep-link contract). Two runs are directly comparable — the
audit is how you tell a real regression from take-to-take noise.

### Step 2d — Deterministic lenses (run BEFORE the judges)

Three checks that cost milliseconds and repeatedly consumed whole judge rounds
when they did not exist. Run them on the fresh capture; fold their findings in
with the judges'.

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || exit 1
RUN_DIR="<run_dir>"; SPEC_ABS="$(realpath <unified_spec>)"

# 1. Did this iteration break something the last one had working?
(cd "$DDD_REPO" && uv run python -m scripts.ddd.regression_guard "$RUN_DIR")

# 2. Does the world read as observed, or as generated?
(cd "$DDD_REPO" && uv run python -m scripts.ddd.data_fidelity "$RUN_DIR/snapshots")

# 3. Is every number the narration speaks actually on the screen?
(cd "$DDD_REPO" && uv run python -m scripts.ddd.narrated_numbers "$RUN_DIR" "$SPEC_ABS")
```

- **regression_guard** — a previously-passing action now failing is a hard
  fail; a score that moved is reported, not gated (a judge is not
  deterministic). This exists because a correct fix in one iteration removed
  the control the next scene clicked, and nothing noticed.
- **data_fidelity** — identical derived columns, duplicate rows, round-robin
  ids, implausibly flat ratios. This was the most-repeated judge finding of the
  first narrative, arriving from a different angle every round.
- **narrated_numbers** — "Kukawa is eleven days out" over a screen that says
  nine. Survived three iterations because only a judge who happened to check
  would catch it.

### Step 3 — Judge (parallel dispatch)

Dispatch **both judges simultaneously** — they are independent and can run in
parallel:

**3a. Concept judge** — invoke `ddd-concept-eval` (via Skill tool or
`/canopy:ddd-concept-eval`) with:
- `run_dir`: `<ddd_dir>/runs/<run_id>/`
- `unified_spec_path`: `<unified_spec>`
- `why_brief_path`: `<why_brief>`

Outputs: `verdict-concept.yaml` + `design_findings.json` inside the run dir.

**3c. Arc judge** — invoke `ddd-arc-eval` (via Skill tool). Unlike the other
two this is ONE dispatch over ALL scenes in order, because it is the only lens
that can see repetition, a sagging middle, or a payoff that lands before its
setup. Every per-scene judge is structurally blind to those. Outputs
`verdict-arc.yaml` + `arc_findings.json`, and it is `gate: gating` — a
narrative that passes every per-scene judge and has no arc is not converged.

**3b. User-artifact judge** — invoke `canopy:visual-judge` (via Skill tool)
over the rendered screenshots + page text, with `audience="feature user"`.

**Build the per-scene `action_trace` first** so the judge can tell a scene that
filled+submitted a form from one that only HOVERED (same end-frame, different
act). Read `run-report.json` and slice its `actions` by `scene_index`:

```python
import json
from scripts.walkthrough._lib.results import action_trace_by_scene

report = json.load(open(f"{run_dir}/run-report.json"))
traces = action_trace_by_scene(report)   # {scene_index: [{kind, target, ok, must_succeed, note}, ...]}
# For scene N: trace = traces.get(N, [])  — empty for narrative-only scenes.
```

Pass that scene's `action_trace` AND its full `narrative` into the
visual-judge `context` (both optional — a scene with no actions passes
`action_trace: []`, and the judge then behaves exactly as before).

When the render used `--capture-action-frames`, a `scene_<N>_before.png` exists
beside the canonical `scene_<N>.png` for each effecting scene. Pass it as the
`frames` before/after pair so the judge can also score the CHANGE the actions
produced, not just the end-frame (omit `frames` for a scene with no
before-frame — single-still behavior):

```python
import os
before = f'{run_dir}/scene_{N}_before.png'
frames = {'before': before, 'after': f'{run_dir}/scene_{N}.png'} if os.path.exists(before) else None

Skill('canopy:visual-judge', args={
    'screenshot_path': '<run_dir>/scene_<N>.png',   # per scene; always the after-frame
    **({'frames': frames} if frames else {}),
    'page_text': '<captured page text from scene_<N>_page_text.json>',
    'rubric': {
        'name': 'user-artifact',
        'default_score': 3,
        'overall_rule': 'lowest',
        'dimensions': [
            {
                'id': 'task_completion',
                'label': 'Task completion',
                'weight': 0.40,
                'anchor': {
                    '5': 'Feature user can complete the target task without help, first try.',
                    '4': 'Task completable; one minor friction point. Name it.',
                    '3': 'Task completable with some trial-and-error. (DEFAULT)',
                    '2': 'Task requires assistance or a workaround.',
                    '1': 'Task cannot be completed — blocker present.',
                },
                'deduction_rules': [
                    'Broken flow that stops task mid-way: max 1',
                    'Required field unlabelled or missing: max 2',
                    # Action-fidelity (only bites when context.action_trace is present):
                    'Scene narration asserts an effecting action (create / fill out / submit / select / award / publish) but action_trace contains no fill/click/select/type/press/draw that effects it (only hover/scroll/wait): max 2 — the task is CLAIMED, not SHOWN.',
                    'Any action_trace entry has ok:false (the demo action failed or timed out): max 2.',
                ],
            },
            {
                'id': 'clarity',
                'label': 'UI clarity for target user',
                'weight': 0.35,
                'anchor': {
                    '5': 'Every label, CTA, and state self-explains to a non-technical user.',
                    '4': 'Clear; one label or affordance could be sharper. Name it.',
                    '3': 'Understandable with a moment of thought. (DEFAULT)',
                    '2': 'At least one element confuses the target user.',
                    '1': 'Core action is hidden or mislabelled.',
                },
                'deduction_rules': [
                    'Jargon visible to non-technical users: max 2',
                ],
            },
            {
                'id': 'trust',
                'label': 'Trust / data confidence',
                'weight': 0.25,
                'anchor': {
                    '5': 'Numbers, sources, and recency are unambiguous; user trusts the output.',
                    '4': 'High trust; one data-provenance signal missing. Name it.',
                    '3': 'Reasonable trust; user may wonder about freshness. (DEFAULT)',
                    '2': 'Data looks stale or sourcing is unclear.',
                    '1': 'Outputs appear fabricated or internally inconsistent.',
                },
                'deduction_rules': [
                    'ENTITY-LEVEL placeholder / test data visible (test-user, Untitled, lorem, impossible values): max 2',
                    'EXEMPT — never a trust deduction: an explicit TOP-LEVEL synthetic/demo-environment marker (a program picker reading "Labs Synthetic", a demo banner, a sandbox label). Honest framing that the demo is synthetic is intended and desirable — showing real program data would be worse. Only the entity-level records (names, orgs, dates, amounts) must look real.',
                ],
            },
        ],
    },
    'context': {
        'audience': {
            'name': 'feature user',
            'decision': 'deciding whether this feature solves their day-to-day problem',
        },
        'domain': '<unified_spec.name>',
        # Action-aware judging — the scene's FULL narration + what the demo
        # actually did. The visual-judge compares claim↔action and applies the
        # task_completion action-fidelity deductions above. Pass [] for a
        # narrative-only scene (no actions) — the judge then behaves as before.
        'narrative': '<scene.narrative>',
        'action_trace': traces.get(<N>, []),   # from action_trace_by_scene above
    },
})
```

Collect the verdict object and write it as `verdict-user.yaml` in the run dir,
stamping the unified verdict metadata (canopy#265 item 1) at the top:

```yaml
kind: user_artifact
gate: gating              # participates in render-loop convergence
live_state_verified: true # visual-judge scores live per-scene screenshots
calibration: provisional
```

**Per-finding `fix_kind` on user-artifact findings.** For each dimension that
scored ≤ 3, the visual-judge verdict carries a `fix_recommendation`. Add a
`fix_kind` tag alongside it so the orchestrator can decide auto-apply vs ask
without re-parsing prose. Same vocabulary as `ddd-concept-eval`:

- `mechanical` — your recommendation names ONE concrete change (add a picker,
  rename a label, fix a URL). The orchestrator can apply via Edit + PR + deploy.
- `options` — recommendation lists 2+ paths and you couldn't pick. Smell:
  contains "Alternative:", "or", "could also". Requires user pick.
- `redesign` — underlying idea needs rethinking; no single fix would address it.

When in doubt, prefer `options` over `mechanical` — a wrongly auto-applied
finding is worse than one extra prompt.

The user-artifact verdict YAML carries these inside per-dimension entries:

```yaml
dimensions:
  task_completion:
    score: 2
    weight: 0.40
    justification: "Ambiguous rows offer only Skip — no inline disambiguation..."
    fix_recommendation: "Add an inline LGA picker per ambiguous row, populated from candidate list."
    fix_kind: mechanical
```

For dimensions that scored ≥ 4 (no finding emitted), `fix_recommendation` and
`fix_kind` are omitted.

### Step 4 — Assemble + convergence

Call `run_pipeline.assemble_run_state` to merge both verdict paths and findings
into `run_state.yaml`. Load the gating pair through the unified loader
(`scripts.ddd.verdicts.load_verdict` — it stamps `kind`/`gate`/
`live_state_verified` from `KIND_DEFAULTS` when the emitter didn't), then
discover any **extra out-of-chain verdicts** present in the run dir —
`verdict-timing.json` / `verdict-video.json` / `verdict-why.yaml` /
`verdict-actionability.yaml` — so the four advisory verdicts flow through the
same schema (canopy#273 item 1):

```python
import json
from scripts.ddd.run_pipeline import assemble_run_state, compute_convergence
from scripts.ddd.runstate import load, save
from scripts.ddd.verdicts import discover_extra_verdicts, load_verdict

# The render manifest is the single source of truth for what was rendered.
manifest = json.load(open(f"{run_dir}/walkthrough-run-data.json"))

# Gating pair — through the unified loader, never a bare yaml.safe_load.
concept_verdict = load_verdict(f"{run_dir}/verdict-concept.yaml")
user_verdict = load_verdict(f"{run_dir}/verdict-user.yaml")

# Any of verdict-timing.json / verdict-video.json / verdict-why.yaml /
# verdict-actionability.yaml present in the run dir loads through the same
# schema (kind/gate/live_state_verified stamped; the out-of-chain score cap
# enforced at the schema layer). n/a artifacts are skipped automatically.
extra_verdicts, extra_paths = discover_extra_verdicts(run_dir)

state = load(run_id)
state = assemble_run_state(
    state,
    concept_verdict=concept_verdict,
    user_verdict=user_verdict,
    findings=<merged findings from design_findings.json>,
    concept_path="<run_dir>/verdict-concept.yaml",
    user_path="<run_dir>/verdict-user.yaml",
    manifest=manifest,   # fills state.scenes_run / state.scene_filter from the manifest
    extra_verdict_paths=extra_paths,   # recorded in state.verdicts alongside the pair
)
save(state)

converged = compute_convergence(concept_verdict, user_verdict, extra=extra_verdicts)
```

Advisory verdicts (timing / video / why / actionability) are recorded and
reported but never block convergence — each verdict's `gate` field decides its
participation, so a future gating judge is data, not new assembler code.

`scenes_run` / `scene_filter` are now populated by `assemble_run_state` from the
manifest — do NOT hand-stamp them. (The manifest is produced by the render step:
`record_video.py --manifest <run_dir>/walkthrough-run-data.json`.)

**Upload gate.** `state.scene_filter is not None` means this is a
partial run — `/canopy:ddd-upload` MUST refuse to upload it. A
feature is only uploadable when convergence has been demonstrated
against the full spec.

### Step 5 — Report + auto_iterate signal

Before printing the report, compute the **auto_iterate signal** so the
orchestrator (and the human reader) knows whether the next iteration can
proceed without user input.

```python
from scripts.ddd.run_pipeline import compute_auto_iterate

# Single source of truth — do NOT re-implement the decision tree here.
# Before deciding anything it NORMALIZES the findings through
# scripts.ddd.finding_class: an ACCURACY finding (the narration asserts something
# the artifact itself contradicts) is forced to fix_kind=mechanical, because the
# artifact is the authority and the correct assertion is readable off it. Only
# STRATEGY findings (the artifact is wrong, not the words) can open the concept
# gate. The normalized findings are written back to state.findings.
#
# Then it gates on PROGRESS (not a raw iteration cap): it appends this iteration's
# gating score to state.score_history and its finding fingerprints to
# state.finding_fingerprints, and returns (action, reason) over:
#   converged                                  -> stop_done / stop_partial
#   a STRATEGY CONCEPT/redesign finding        -> stop_concept_change
#   score stalled/regressed (noise-banded)     -> stop_max_iter
#   identical findings + no real score move    -> stop_max_iter  (plateau)
#   hard-cap backstop                          -> stop_max_iter
#   any options/redesign left                  -> stop_unclear
#   else (mechanical + still progressing)      -> continue
# It reads user_verdict.dimensions for per-dimension fix_kinds and honors
# state.scene_filter for the partial case, and stamps state.terminal_status.
auto_iterate_next_action, reason = compute_auto_iterate(
    state, concept_verdict, user_verdict, findings, converged=converged,
    # unattended defaults to gates.is_unattended() — with no human present a
    # stuck stop is TERMINAL (report + --stuck package), never a wait.
)
```

**Two things the score alone cannot tell you, both now folded in:**

- **The noise band.** Per-cell judge variance is ±1 on byte-identical frames, so a
  move smaller than `denoise.NOISE_BAND` (0.5) is not evidence of improvement OR
  of regression. Reading noise as either is how a round of real fixes got recorded
  as a stall (ace#1393).
- **The finding plateau.** Two iterations producing an identical
  finding-fingerprint set with no real score move means the loop is re-deriving,
  not progressing. Unlike the score, this signal does not wobble: the judge's
  number for a cell moves; the defect it names does not.

> **Why this is a function call, not inline logic.** The trajectory-gating decision
> (progress-aware loop, not a raw `MAX_ITERATIONS=3` count) lives in ONE place —
> `run_pipeline.compute_auto_iterate`, which is unit-tested. Re-implementing it in
> this SKILL would drift from the code. A raw count stopped good runs mid-progress
> and was blind to regressions; trajectory-gating keeps looping while the score
> improves and stops the instant it stalls or regresses.

Stamp `auto_iterate_next_action` and `auto_iterate_reason` onto run_state
(both are optional fields on RunState) and `save(state)`. `compute_auto_iterate`
also stamps **`state.terminal_status`** — `converged_clean` |
`converged_with_open_questions` | `stopped_not_converged` | `diverging` |
`running`. Report it: "converged, good", "converged, still failing" and
"diverging" are genuinely different endings and must never print the same. Then print the
summary. **Every verdict line MUST be rendered via
`run_pipeline.format_verdict_line`** — a capped verdict (the out-of-chain cap
fired because `live_state_verified: false`) renders as
`4.0/5 (pass — capped from 4.8, not live-state verified)`, never as a bare
`4.0/5` indistinguishable from an honest 4.0 (canopy#273 item 3):

```python
from scripts.ddd.run_pipeline import format_verdict_line

concept_line = format_verdict_line(concept_verdict)
user_line = format_verdict_line(user_verdict)
advisory_lines = {k: format_verdict_line(v) for k, v in extra_verdicts.items()}
```

```
DDD Run — <run_id>
══════════════════════════════════════
  Spec: <unified_spec>
  Scope: --scene <selector> → rendered scenes <indices> of <total>      # only if filtered
  Run dir: <run_dir>
  Hosted deck (iter <N>): <DECK_URL>                                    # NEW — from Step 2b
  Hosted clip (iter <N>): <CLIP_URL>                                    # NEW — only if recorded

  Concept judge:       <concept_line>
  User-artifact judge: <user_line>
  Advisory:            <kind>: <advisory_lines[kind]>                   # one line per extra verdict, if any

  Convergence (filtered): YES | NO  (threshold: 4.0)                    # "filtered" tag if partial

  Auto-iterate: <action>  (<reason>)                                    # NEW
  Termination:  <terminal_status>                                       # NEW

  Top findings (<N> total):
    [PRODUCT mechanical]  Scene N: <detail>     <DECK_URL>#scene-<N>    # deep-link per finding
    [CONCEPT options]     Scene M: <detail>     <DECK_URL>#scene-<M>
    [RESEARCH mechanical] Scene K: <detail>     <DECK_URL>#scene-<K>
    [DEFER ]              Scene J: <detail>     <DECK_URL>#scene-<J>
    ...

  run_state.yaml updated → phase: judged
```

Each finding's deep-link uses the hosted deck URL from Step 2b plus the
`#scene-<N>` anchor for the scene the finding is about. If Step 2b's
upload failed (no entry in `state.iteration_decks[state.iteration]`),
drop the deep-link column and print a one-line note above the findings
table explaining the upload failure — never substitute a `file://` path.

The per-finding tag in the listing is `[<route> <fix_kind>]` so the reader
can see at a glance which findings the orchestrator will auto-apply
(`mechanical`) vs which need attention (`options`/`redesign`/DEFER).

**Tail messages by `auto_iterate_next_action`:**

- `stop_done` — "Both judges passed. Run is converged — proceed to promotion."
- `stop_partial` — "Filtered scope passed. Drop `--scene` and re-fire for promotion."
- `stop_concept_change` — "Strategy finding present (the artifact, not the wording, is wrong) — surface to user via canopy-web review surface."
- `stop_unclear` — "Findings with `options`/`redesign` fix_kind block auto-iteration. List them and ask the user to pick or redesign."
- `stop_max_iter` — "Loop stopped making progress (stall, plateau, or backstop). Stop and surface remaining findings."
- `continue` — "Orchestrator can apply mechanical fixes per finding and re-fire `/canopy:ddd-run` on the same scope."

**Unattended runs never wait on a stuck stop.** With no human in the loop
(`gates.is_unattended()`), every stuck stop is terminal: upload the `--stuck`
package, report `terminal_status` plus the open strategy findings, and finish.
Resolve any gate through `scripts.ddd.gates.resolve(...)`, which returns the
gate's declared no-human default (`concept_change` → `defer`,
`external_release` → `hold`) instead of polling for a click nobody will make.

### Step 5b — review_mode gate (human mode posts a findings review instead of auto-applying)

The spec's optional **`review_mode`** key (`UnifiedSpec.review_mode`,
default `autonomous`) decides what happens to PRODUCT findings after the
report. Check it before acting on `auto_iterate_next_action`:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
UNIFIED_SPEC_ABS="$(realpath <unified_spec>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.findings_review mode "$UNIFIED_SPEC_ABS")
# prints: autonomous | human
```

- **`autonomous`** (default): proceed exactly as the tail messages above say —
  `continue` auto-applies mechanical PRODUCT findings and re-fires.
- **`human`**: do NOT auto-apply ANY PRODUCT finding (mechanical included).
  When the run did not converge and PRODUCT findings exist, post the
  **product-findings review** instead and stop the loop until it resolves:

  ```bash
  _CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
  DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
  (cd "$DDD_REPO" && uv run python -m scripts.ddd.findings_review post "<run_id>")
  ```

  It clusters PRODUCT findings (concept `design_findings.json` + user-artifact
  dimension findings), attaches per-cluster evidence deep-links — the Step 2b
  deck URL with `#scene-<N>` and the clip URL with `#t=<seconds>` (the scene's
  start offset from `run-report.json`'s per-scene timings) — posts ONE
  `gate: product_findings` review, and stamps `findings_review_id` /
  `findings_review_url` onto run_state. Then present the single review URL +
  a compact summary table in chat and follow
  `skills/ddd-findings-review/SKILL.md` (await → `apply` → route the
  implement/skip/defer selection). CONCEPT / RESEARCH / DEFER findings keep
  their standard routing in either mode.

### Always link findings when you surface them to the user — gate or no gate

The `findings_review post` machinery above ONLY fires for `review_mode: human`
AND ONLY clusters PRODUCT findings. But the rule it encodes is universal:
**any time you present findings, judge verdicts, or a "fix vs pause" decision to
the user — including in autonomous mode, when the blocker is a CONCEPT / USER
`visual_polish` score the gate skips, or when you are just reporting in chat —
give per-finding deep-links the user can click to SEE the finding. Never a
link-less prose / markdown table of findings.** A findings table the user cannot
click into is the exact failure this gate exists to prevent (no hand-written
chat tables, no scrubbing the clip).

When the auto-gate does not post, build the links by hand:

- **Video moment** — `<canopy-base>/w/<id>#t=<int_seconds>`, the scene's start
  offset in the UPLOADED video. Compute it from the cumulative `seconds` of the
  explainer beats in `/tmp/ddd/vo-<slug>/explainer_spec.yaml` — NOT the raw
  clip's `run-report` offsets, which differ after de-dwelling + VO. For a moment
  that happens late in a long `pace: teach` beat (e.g. a popover that opens near
  the end), offset INTO the beat; don't just link its start.
- **Deck still** — `<DECK_URL>#scene-<N>` when a Step 2b deck exists.
- Plus the `/ddd/<slug>` run page for the whole thing.

## Output files

| File | Producer | Notes |
|------|----------|-------|
| `<run_dir>/verdict-concept.yaml` | ddd-concept-eval | Concept judge verdict |
| `<run_dir>/design_findings.json` | ddd-concept-eval | Tagged design findings |
| `<run_dir>/verdict-user.yaml` | canopy:visual-judge (user-artifact) | User-artifact judge verdict |
| `<run_dir>/run_state.yaml` | assemble_run_state + save | phase=judged, verdict paths (gating pair + any extras), findings |
| `<run_dir>/verdict-timing.json` / `verdict-video.json` / `verdict-why.yaml` / `verdict-actionability.yaml` | other DDD skills (optional) | Advisory verdicts — discovered by Step 4 when present, recorded + reported, never convergence-blocking |
