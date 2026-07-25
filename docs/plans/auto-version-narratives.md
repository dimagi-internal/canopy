# Auto-version narratives on any change

> **Status: shipped — auto-versioning entry point lives in `scripts/ddd/narrative.py`. Historical record, not current-state.**

## Rule

Any change to a spec's **narrative content** automatically posts a new narrative
version and attaches the run to it. No significance judgment, no per-edit human
pause. Human approval stays at **`external_release`** only.

## What already exists (reuse, don't build)

The hard parts are already in `scripts/ddd/narrative.py`:

- **`narrative_content_hash(spec)`** — hashes exactly the narrative fields:
  `name`, `narrative` (overview), `personas`, `build_order`, and per-scene
  `_NARRATIVE_SCENE_FIELDS = (title, persona, provenance, concept_claim, features)`.
  It **excludes the render recipe** (`show`/`url`/`actions`/`design_intent`/
  `viewport`/timing). So editing a selector or a wait never counts as a narrative
  change — the boundary we want is already drawn.
- The spec records **`narrative_synced_version`** + that hash (`mark_synced`).
- **`decide_narrative_sync(...)`** already classifies the state from
  `local_changed` (hash mismatch) + version skew. A local narrative edit →
  `local_changed=True` → today returns **`refuse_local_newer`** ("push an update
  instead of overwriting").
- A post path publishes a version to canopy-web; `get_narrative` /
  `narrative_version_exists` read it back.

## The change (small)

1. **Auto-push instead of refuse.** In the preflight, when
   `narrative_content_hash(current_spec) != synced_hash`:
   - post a new version (regenerate the `story` from the current narrative fields),
   - `mark_synced` (record new version + hash on the spec),
   - stamp the run's `narrative_review_id` to the new version,
   - continue — **no pause**.

   This replaces the `refuse_local_newer` dead-end for the routine case. Keep
   **`refuse_conflict`** (web *also* advanced underneath you) as a real,
   surfaced conflict — auto-posting there would clobber someone else's version.

2. **Decouple posting from the approve gate.** `ddd-narrative-review`'s
   approve/redraft pause becomes opt-in (first-ever narrative for a slug, or an
   explicit `--review` flag). Routine auto-posts skip it.

3. **Human gate stays at `external_release`** (unchanged). The publish boundary
   is where a person sees a changed story before a package goes out.

## Where it hooks

- **`ddd-run` Step 1 preflight** (preferred) — version *before* render+judge so
  the verdict attaches to the version it actually rendered.
- **`ddd-upload` Step 0.5** — backstop, next to the existing narrative-missing
  guard, so a hand-driven upload still can't attach a run to a stale version.

## One boundary to confirm

The hash treats per-scene `concept_claim` (the load-bearing claim) as narrative
but **not** the scene's descriptive `narrative:`/`show:` prose (those ride with
the recipe). That's a sensible line. If you want prose tweaks to bump the version
too, add `narrative`/`show` to `_NARRATIVE_SCENE_FIELDS`.

## Net

~1 function flips (refuse → auto-post), 1 gate decoupled to opt-in, 0 new
infra. The hash, the field boundary, and the sync-state record already exist.

## Consequence (accepted)

More versions — every narrative edit mints one. Versions are cheap; the version
list becomes the literal changelog of the narrative, and a run can never point at
a stale story.
