---
name: ddd-upload
description: |
  Upload a converged DDD run's artifacts to canopy-web as ONE navigable package:
  upload the hero video, build a self-contained HTML docs page (hero video +
  capabilities + why + how for a prospective feature user), run the
  external_release review gate, then publish to canopy-web — where the video,
  deck, narrative, and links group under the run and are navigable at
  /ddd/<narrative-slug>/<run_id>. Returns the run PACKAGE URL, not a loose artifact link.
  Phase transitions from "converged" → "uploaded" on publish; no-op on hold.
  STUCK runs (didn't converge) upload a REVIEW package too — pass --stuck to skip the
  external_release gate and leave the run iterable, so there is ALWAYS a navigable
  /ddd/<slug>/<run_id> to inspect per-scene and decide next steps.
  Use when asked to "upload this run", "publish the run", "upload the run package", or
  when the loop gets stuck (stop_unclear / stop_max_iter / stop_concept_change / stop_partial).
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue — do not block on the upgrade.

# DDD Upload

Terminal step of the DDD loop: once a run has converged, UPLOAD its artifacts to
canopy-web so they package together under the run. The result is a single
navigable view — **`/ddd/<narrative-slug>/<run_id>`** — that links the run's hero
video, docs/deck, narrative, and companion links. The skill returns **that
package URL**, not a loose `/walkthrough/<artifact-id>` single-artifact link.

## How packaging works (why you get one navigable view, not loose links)

canopy-web groups artifacts by the `run_id`/`narrative_slug`/`role` fields sent on
every upload, and assembles them into a run package:

- **video** ← the hero video upload (`role=hero_video`)
- **deck** ← the docs HTML page this skill builds (`role=docs`)
- **narrative** ← the `ReviewRequest` posted by the earlier
  `ddd-narrative-review` gate, which stamps `run_state.narrative_review_id`.
  The upload **refuses to publish** a run that has no narrative (see Step 0.5),
  so the package's narrative slot is never empty — a run reaches publish only
  after its narrative gate has run.
- **links** ← companion links unioned across the run's artifacts.

The package + previous-runs navigation live at `/ddd/<narrative-slug>` (narrative
landing) and `/ddd/<narrative-slug>/<run_id>` (the run package).

## Stuck runs upload a review package too (`--stuck`)

A run that does NOT converge but gets **stuck** (the orchestrator's
`stop_unclear` / `stop_max_iter` / `stop_concept_change` / `stop_partial`) should
STILL produce a navigable package — that's exactly when you want to open
`/ddd/<slug>/<run_id>`, poke each scene's deep-link, and decide what to do next.
A non-converged run must never leave the user with no package, or with only a
loose `/walkthrough/<artifact-id>` link.

Pass **`--stuck`** (CLI) / `release=False` (`upload_run`). In this mode the script:

- uploads the hero video (`role=hero_video`) and the docs page (`role=docs`) so the
  package assembles and is navigable per-scene, **but**
- **skips the `external_release` gate** — a stuck run is an internal inspection
  package, not a public release, and
- **leaves `phase` unchanged** (does NOT set `uploaded`), so the run stays
  iterable: fix the stuck findings, re-render, and re-upload the same run.

The narrative + partial guards (Steps 0 / 0.5) still apply — a review package must
still have a narrative and (for a clean package) cover the full spec.

**Do NOT** fall back to `scripts/walkthrough-share/upload.py` to hand-make a link
for a stuck run — that produces a loose, mis-roled `/walkthrough/` artifact that masquerades
as the run's package. Always go through this skill so the link is the real
`/ddd/<slug>/<run_id>` package.

## Inputs

- **`run_id`** — an existing converged run identifier.  The run directory must
  already exist at `<ddd_dir>/runs/<run_id>/` and contain:
  - `run_state.yaml` (phase should be `converged`)
  - `unified_spec.yaml`
  - `why_brief.yaml`
- **`video_path`** — local filesystem path to the hero video `.mp4` produced by
  the converged walkthrough render step.

## What the docs page (deck) contains

The generated HTML is a **self-contained, no-external-deps documentation page**
aimed at a **prospective user of the feature** — not an internal reviewer, not a
developer.  Sections, in order:

1. **Hero video** at the top: the converged walkthrough recording embedded as a
   `<video>` (direct .mp4 / data URI, or a canopy-web `/walkthrough/` share URL
   rewritten to its `/content` byte stream) or `<iframe>` (genuinely external
   embeds only).
2. **What you can do** — one bullet per scene's `concept_claim`.  Capabilities.
3. **Why it works this way** — the `why_brief.problem` context followed by each
   `spine[].claim` + `spine[].rationale`.  Grounded reasoning.
4. **How to use it** — numbered steps from each scene's `show` field, in scene
   order.  Instructional.

All user-supplied text is HTML-escaped (no XSS).

> **Remotion glossy render is explicitly deferred.**  The current docs page is
> plain HTML — no Remotion, no ace-web, no MP4 generation from slides.  That
> upgrade is planned but out of scope here.

## Procedure

### Step 0 — Pre-flight: refuse partial runs

Read `<run_dir>/run_state.yaml`. If `scene_filter` is set (i.e. the
converged run came from `/canopy:ddd-run --scene <selector>`), STOP and
tell the user:

> "Run `<run_id>` was a partial render — it covered scenes <scenes_run>
> out of <spec_total>. Uploading requires a full-spec run so the
> published package reflects the whole feature, not just one scene.
> Re-run `/canopy:ddd-run <new_run_id> <unified_spec> <why_brief>`
> WITHOUT `--scene`, then upload that."

Do NOT attempt to assemble a docs page from a partial run, even if the
filtered scope converged — the "What you can do" section is built per
scene, and a partial run would publish a docs page missing capabilities
the spec promises.

### Step 0.5 — Pre-flight: sync, then refuse runs with no narrative

**First, sync the narrative (backstop, no pause).** A hand-driven upload can run
on a spec whose narrative was edited since the last version was posted —
attaching the run to a stale story — including edits the user made on the **web**
review surface that were never pulled down. `pull` fetches canopy-web's current
narrative into the generated lock, so the published package always points at the
current narrative:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC_ABS="$(realpath "<spec_path>")"   # the run's unified_spec.yaml
(cd "$DDD_REPO" && uv run python -m scripts.ddd.narrative pull "<narrative-slug>" "$(dirname "$SPEC_ABS")")
```

- `{"action": "pulled", "version": N, "lock_path": ...}` — the lock now holds
  canopy-web's current narrative. Continue.
- There is **no conflict case**. canopy-web owns the story and the lock is a
  generated cache of it, so a pull can only ever fast-forward. (Pre-L1 this step
  could exit 2 on a divergence between two writers of one file; that failure mode
  no longer exists.)

Then check the run has a narrative — otherwise the package renders as **"no
narrative"** in canopy-web. Check it deterministically:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
(cd "$DDD_REPO" && uv run python -m scripts.ddd.narrative status "<run_id>")
```

This prints a status JSON and **exits non-zero when the run has no narrative**
(neither a stamped `narrative_review_id` nor a narrative version on canopy-web
for the run's `narrative_slug`). If it exits non-zero, STOP and tell the user:

> "Run `<run_id>` has no narrative on canopy-web (its `narrative_slug` is
> `<narrative-slug>`). Publishing would show as 'no narrative'. Run
> `/canopy:ddd-narrative-review <run_id>` first — that posts and locks the
> narrative and stamps the run — then re-run the upload. This usually means the
> feature slug was renamed mid-flow and the narrative was posted under the old
> slug."

This is belt-and-suspenders: the upload script (`scripts.ddd.upload`) enforces
the same guard and raises `NarrativeMissingError` rather than publish a
narrative-less run. The escape hatch `DDD_ALLOW_NO_NARRATIVE=1` exists for
emergencies only — do not set it to work around a genuinely missing narrative.

### Step 1 — Run the upload script

Run the upload script (it lives in the canopy runtime):

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
# pass the video_path as an absolute path (resolved before the cd):
VIDEO_ABS="$(realpath <video_path>)"
# Converged run → public release. external_release is a HUMAN CHECKPOINT (external
# publishing is outward-facing, so it is one of DDD's two deliberate approval gates).
# The human's in-CHAT "publish it" does NOT resolve this gate — the gate's approval
# channel is a separate canopy-web UI click (or --release-approved). Run this bare
# form non-interactively (an agent / CI) and it will NOT publish: it posts the review,
# prints the URL, and HOLDS (returns without publishing). It no longer hangs — but it
# also doesn't publish, so the bare form is rarely what you want:
(cd "$DDD_REPO" && uv run python -m scripts.ddd.upload <run_id> --video "$VIDEO_ABS")
# Converged run + the human approved release in-session ("publish it" / "externally
# publish") → THIS is the form to use. It resolves the gate WITH that approval instead
# of waiting on a UI click. NOT a bypass: the review is still created and submitted
# (status resolved, attributed to the token's user, with a delegated-approval note),
# so the publish stays auditable:
(cd "$DDD_REPO" && uv run python -m scripts.ddd.upload <run_id> --video "$VIDEO_ABS" --release-approved)
# STUCK run → review package (skips the gate, leaves the run iterable):
(cd "$DDD_REPO" && uv run python -m scripts.ddd.upload <run_id> --video "$VIDEO_ABS" --stuck)
```

**RULE — a human's in-session approval IS the gate approval, so pass `--release-approved`.**
When a human told you to publish in the session (e.g. "publish it", "externally publish"),
that *is* the external_release sign-off — pass `--release-approved` so it resolves through an
authorized, attributed channel. Do NOT run the bare release form expecting it to publish: the
external_release gate is a **separate approval channel** (a canopy-web UI click) that the
human's in-chat approval does not satisfy — the two are disconnected, which is why a bare
publish "unexpectedly asks for approval." Run bare + non-interactively, it now prints the
review URL and **holds** (nothing published) rather than hanging — a clear signal to re-run
with `--release-approved` (or resolve the review in the UI). Only omit `--release-approved`
when no human has approved and you deliberately want to post the gate for later UI resolution.

The script orchestrates all steps internally:

1. Loads `run_state.yaml` + `unified_spec.yaml` + `why_brief.yaml` from the run dir.
2. Uploads the hero video to canopy-web (`role=hero_video`) → `video_url`.
3. Builds the docs HTML via `build_docs_page(spec, why_brief, video_url)`.
4. Opens the **external_release** review gate (see below).
5. On `"publish"`: uploads the HTML (`role=docs`), sets `phase = "uploaded"`,
   saves, and returns the run **package** URL `/ddd/<narrative-slug>/<run_id>`.
6. On `"hold"`: returns without publishing the deck; phase stays unchanged.

### Step 2 — External release gate

The gate presents a `ReviewRequest` with:

```
gate: "external_release"
decisions:
  - id: publish
    prompt: "Publish this run package for users?"
    options: [publish, hold]
    recommended: publish
    class: external_release
```

The review is posted to canopy-web and the script waits for human resolution.

**Outcomes:**

| Decision | Effect |
|----------|--------|
| `publish` | Deck HTML is uploaded, `phase = "uploaded"`, **package URL** is returned |
| `hold`    | No deck upload, phase unchanged, script exits with empty string |

If the human chose `hold`, tell the user:

```
Upload held — the run package was not published.
Review the hero video and the docs HTML preview, make any corrections, then
re-run /canopy:ddd-upload when ready.
```

### Step 3 — Report

On successful publish:

```
DDD Upload — <run_id>
══════════════════════════════════════
  Run dir: <run_dir>

  Package:  <package_url>     ← /ddd/<narrative-slug>/<run_id> (navigable: video · deck · narrative · links)

  phase → uploaded
```

Tell the user: "Run uploaded. Share `<package_url>` — it's the navigable
package (video, deck, narrative, and links) for this run."

## Output artefacts

| Artefact | Notes |
|----------|-------|
| `run_state.yaml` (phase=uploaded) | Written on successful publish only |
| Run package on canopy-web | `/ddd/<narrative-slug>/<run_id>` — groups video + deck + narrative + links |
| Docs page (deck) on canopy-web | Self-contained HTML; grouped under the run via `role=docs` |
| Hero video on canopy-web | Uploaded first (`role=hero_video`); embedded in the deck and the package |
