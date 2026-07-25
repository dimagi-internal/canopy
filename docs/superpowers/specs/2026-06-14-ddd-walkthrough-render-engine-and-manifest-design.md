# DDD ↔ Walkthrough: one render engine, one manifest

> **Status: shipped — historical record, not current-state.**

**Date:** 2026-06-14
**Status:** Design approved (scope: "everything" + unify both renderers); ready for implementation plan.
**Repo:** `canopy` (`~/emdash-projects/canopy`). No labs changes.

## Problem

`scripts/walkthrough/` (the renderer) and `scripts/ddd/` (the DDD lifecycle) are
supposed to layer cleanly — walkthrough renders a spec; ddd reuses that render and
adds judging/convergence/upload. In practice the boundary has fractured into **two
renderers and three builders of the same "what was rendered" data**, with no shared
contract:

1. **The `walkthrough` skill** (`skills/walkthrough/SKILL.md`) — the original flow.
   The agent hand-builds `/tmp/walkthrough-run-data.json` *inline from markdown
   instructions*, runs `generate_presentation.py` for the deck, and records the
   video as a **separate pass AFTER scoring**. Deck screenshots and the mp4 are two
   different captures of the same scenes → they can desync.
2. **`record_video.py`** — a newer, code-driven renderer. Reads the spec, drives the
   product once, writes `mp4` + snapshots + `run-report.json` (an action log). It
   *consumes* `walkthrough-run-data.json` via `--input` but **never produces it**.
3. **`ddd`** straddles both: `ddd-run` renders via `record_video.py`, then
   `upload.py`'s `_build_deck_run_data()` **rebuilds** the deck data from captured
   PNGs — a third implementation, and the buggy one (hardcodes `run_dir/scene_N.png`
   while frames land in `run_dir/snapshots/`, finds zero, silently skips).

Every symptom we've hit traces to this missing boundary:

- **Empty walkthrough decks** on every `record_video`-driven (ddd) run — the deck's
  input sidecar is orphaned (nothing writes it) and the rebuild fallback looks in
  the wrong directory and fails silently (`except … print(..., file=sys.stderr)`).
- **`${…}` in External-Systems links** — `upload._external_links_from_spec()` reads
  raw `scene.url` (`${wk4_url}`) and only prepends the host; it never substitutes,
  because the substitution map lived only in the recorder and was thrown away.
- **The live audit URL missing from the package** — it's a *runtime* URL the render
  navigated to (created on camera), never present in any `scene.url` template, so a
  spec-derived link list can't see it. The recorder doesn't track visited URLs.
- **`narrative status` crashes** — `_review_id_from_url` is defined in `upload.py`
  but referenced-but-not-imported in `narrative.py` (`NameError`).
- **cwd/repo-root gymnastics** — `_resolve_ddd_dir()` shells `git` against the
  current cwd, so importing `scripts.ddd` (needs cwd=canopy) vs resolving a run
  (needs cwd=target-repo) forced chdir + `PYTHONPATH` workarounds all session.
- **SKILL ↔ code logic duplication** — `ddd-run` Step 5 re-implements
  convergence/auto_iterate already in `run_pipeline.py`; `scenes_run`/`scene_filter`
  are hand-stamped in markdown instead of by `assemble_run_state`.

The deeper read: `ddd` reaches across the boundary and **reimplements**
`walkthrough`'s job instead of **consuming** its output. There is no seam.

## Goals

1. **One render engine, one capture pass** producing `{mp4, snapshots, deck,
   manifest}` together — deck generated *from* the manifest, same capture as the
   video (no second pass, no desync).
2. **One manifest** = the single boundary object. The renderer's public output; the
   only thing ddd (and the walkthrough skill) consume to know "what the render did."
3. **Both consumers migrated onto it**: the standalone `walkthrough` skill and `ddd`
   call the same engine and read the same manifest. `ddd` deletes its rebuild and
   spec-link derivation; the walkthrough skill stops hand-authoring run-data JSON.
4. **Loud, not silent** — a missing/short deck or manifest is an error surfaced in
   `run-report.json` and a non-zero signal, never a swallowed exception.
5. Fix the independent clarity defects that compound the above: the `narrative`
   `NameError`, duplicated auth/url helpers, cwd coupling, SKILL↔code duplication,
   and scattered gate/finding/severity vocabulary.

## Non-goals

- The **review-resolve API** (post-but-not-resolve asymmetry) — it needs a
  canopy-web server change, not these scripts. Tracked separately; this spec only
  leaves a client `resolve()` stub + a note so the follow-up is a thin wire-up.
- Renaming the manifest file. It stays `walkthrough-run-data.json` (the de-facto
  contract name `generate_presentation`, `walkthrough-eval`, `defect-creator`, and
  `record_video --input` already use). New fields are added as a **superset**;
  renaming would touch more files for cosmetic gain (YAGNI).
- Reworking the judges' rubrics or the DDD phase semantics. We document the phase
  milestones; we do not add a phase state machine.

## Architecture

### The seam: the render manifest (`walkthrough-run-data.json`, superset)

A single JSON the **engine writes** and **everyone reads**. Superset of today's
shape so `generate_presentation` and the eval fixtures keep working:

```yaml
# top-level
name: <spec.name>
narrative: <spec.narrative>
generated_at: <ISO8601>          # stamped by caller, not Date.now in-script
base_url: <resolved base>
scenes_run: [1,2,3,…]            # 1-based spec indices actually rendered
scene_filter: <selector|null>    # the raw --scene selector, or null for full
substitution_vars: { wk4_url: "/labs/workflow/…", … }   # the realized ${…} map
personas: { amani: {name, role, color, intro}, … }
slides:
  - type: scene                  # or "title"
    scene_index: 3               # 1-based spec index (stable across partial runs)
    scene_total: 15
    title: …
    narration: …
    persona_key: amani
    # --- capture facts (engine-owned) ---
    url_resolved: "https://labs…/labs/workflow/3149/run/?run_id=4318&…"  # ${…} substituted
    urls_visited:                # ordered, deduped; INCLUDES runtime-created URLs
      - "https://labs…/labs/workflow/3149/run/?run_id=4318&…"
      - "https://labs…/audit/4317/bulk/?opportunity_id=10000"   # ← the on-camera audit
    screenshot_path: "snapshots/scene_3.png"     # run-dir-relative
    page_text_path: "snapshots/scene_3_page_text.json"
    screenshot_b64: <base64>     # kept for generate_presentation back-compat (from screenshot_path)
    mp4_start_offset: 42.5       # seconds into the clip — enables #t= deep-links
    ok: true                     # all must_succeed actions in this scene passed
    # --- judgment overlay (NOT engine-owned; merged later) ---
    ai_evaluation: null          # {score, dimensions, …} filled by a scoring pass
```

**Ownership split is the whole point:**
- **Capture facts** (`url_resolved`, `urls_visited`, paths, `mp4_start_offset`,
  `ok`, screenshots) — written by the engine. Pure record of what happened.
- **Judgment overlay** (`ai_evaluation`) — written by a *separate* scoring step
  (the walkthrough skill's `visual-judge` pass, or ddd's judges). The engine never
  scores; consumers merge scores into the manifest they were handed.

### The single render engine (evolve `record_video.py`)

`record_video.py` becomes the one capture pass. New responsibilities (it already
holds the data; it just discards it today):

1. **Track `urls_visited` per scene.** The recorder currently never reads
   `page.url`. Add a per-scene navigation collector (e.g. a `page.on("framenavigated")`
   subscription, or snapshot `page.url` after every action) that records the ordered,
   deduped set of URLs the page actually showed during the scene — this is what
   captures the dynamically-created audit URL.
2. **Record `mp4_start_offset` per scene** from the scene-timing it already tracks
   for the clip.
3. **Emit the manifest** (`walkthrough-run-data.json`) at end of render: per-scene
   `url_resolved` (already resolved during the run), `urls_visited`, relative
   `screenshot_path`/`page_text_path`, `screenshot_b64` (read from the PNG it just
   wrote), `mp4_start_offset`, `ok` (from the report), plus top-level
   `substitution_vars` (from setup outputs), `scenes_run`, `scene_filter`,
   `personas`, `name`, `narrative`, `base_url`. `ai_evaluation` defaults to null.
4. Keep writing `run-report.json` (action telemetry — a *separate* concern from the
   scene manifest; do not merge them).
5. **Delete the dead `--input` reconstruction path** (or repoint it to read the
   manifest the engine now produces, for partial re-renders).

`generated_at`/timestamps are passed in by the caller (scripts must not call
`Date.now`/`datetime.now` where it would break determinism — stamp after the run).

### Deck = one builder, from the manifest

`generate_presentation.py` reads the manifest (it already wants this shape). Delete
`upload._build_deck_run_data()` (the buggy third builder). Deck production happens in
**one** place driven by the manifest, used identically by both consumers.

### Consumer 1 — the `walkthrough` skill becomes a thin wrapper

New flow: **render (engine → manifest) → score (visual-judge per scene, merge
`ai_evaluation` into the manifest) → deck (from the manifest)**. It stops
hand-authoring `/tmp/walkthrough-run-data.json` in markdown, and the
**after-scoring separate video pass is eliminated** (scoring reads the captured
frames from the single pass). `walkthrough-eval` and `walkthrough-defect-creator`
keep reading `walkthrough-run-data.json` unchanged (superset shape is back-compat).

### Consumer 2 — `ddd` becomes a pure consumer

- `ddd-run` renders via the same engine; the manifest lands in the run dir.
- **`upload.py` reads the manifest**: deck slides from it (no rebuild); External-
  Systems links from `url_resolved` + `urls_visited` (delete
  `_external_links_from_spec` → fixes `${…}` *and* surfaces the audit URL).
- **`assemble_run_state`** reads `scenes_run`/`scene_filter` from the manifest
  (the SKILL stops hand-stamping them).
- **Deck/link steps assert** the manifest exists and `len(slides) == len(scenes_run)`;
  a mismatch raises / records a red line in `run-report.json` — no silent skip.

### Independent clarity fixes (folded in)

- **③ Shared helpers + `NameError`.** New `scripts/ddd/auth.py`
  (`DEFAULT_API`, `TOKEN_FILE`, `resolve_base_url`, `resolve_token`), imported by
  `review.py` + `upload.py`. Move `_review_id_from_url` (+ its regex) into
  `review.py`; import it in `upload.py` and `narrative.py` → fixes the
  `narrative status` crash. Regression test.
- **④ cwd decoupling.** `_resolve_ddd_dir(repo_root: Path | None = None)`;
  `load`/`save`/`escalation._state_file` accept an optional `ddd_dir`; honor a
  `DDD_DIR` env / `--ddd-dir` CLI flag at boundaries. Document the contract in the
  `runstate` module docstring. Ends the chdir/`PYTHONPATH` dance.
- **② SKILL ↔ code single-source.** Move `ddd-run` Step 5's inline convergence/
  auto_iterate logic into `run_pipeline.compute_auto_iterate(state, concept_v,
  user_v, findings) -> (action, reason)`. The SKILL *calls* it and stamps the
  result; the markdown stops re-implementing the decision tree (and the `HARD_CAP`
  constant).
- **Vocab/shape.** `Gate` enum (`CONCEPT_CHANGE`, `PRODUCT_FINDINGS`,
  `EXTERNAL_RELEASE`) in `schemas/models.py` replacing string literals; a shared
  `Finding` pydantic model (`scene, dimension, route, fix_kind, severity, detail,
  fix_recommendation`) used by the judges + `findings_review`, with **one**
  `derive_severity` (the judge sets it; `findings_review` stops re-deriving — fixes
  the concept-eval-vs-findings_review divergence). Document which `phase` values are
  code-set (`judged`, `uploaded`) vs orchestrator-only milestones. Short
  run-vs-iteration glossary at the top of `agents/ddd.md` + the SKILL headers.

## What this fixes (traceable to today's failures)

| Failure | Fix |
| --- | --- |
| Empty walkthrough deck on ddd runs | Engine emits the manifest; one deck builder reads it; deck step asserts not skips |
| `${…}` in External-Systems links | Links read `url_resolved` from the manifest, not raw `scene.url` |
| Live audit URL missing from package | Engine records `urls_visited`; links include it |
| `narrative status` NameError | `_review_id_from_url` shared + imported |
| chdir/PYTHONPATH gymnastics | `_resolve_ddd_dir(repo_root)` + `ddd_dir` overrides |
| Two renderers / 3 run-data builders | One engine, one manifest, one deck builder, two thin consumers |
| Deck/video desync | Single capture pass; scoring overlays captured frames |
| SKILL↔code drift | `compute_auto_iterate` single source; manifest fills `scenes_run` |

## Testing

- **Manifest golden test:** engine run (against a fixture spec / fake page) writes a
  manifest with resolved `url_resolved` (no `${`), `urls_visited` including a
  runtime-navigated URL, relative paths that exist, `mp4_start_offset`, `ok`,
  `scenes_run`/`scene_filter`.
- **Deck-from-manifest:** `generate_presentation` over the golden manifest yields one
  slide per `scenes_run` entry; assert no slide is missing a screenshot.
- **Upload links:** `upload` link derivation over the manifest produces fully-resolved
  URLs (none containing `${`) and includes the `urls_visited` audit URL; assert the
  old `_external_links_from_spec` is gone.
- **Loud failure:** a manifest with `slides=[]` (or count ≠ `scenes_run`) makes the
  deck/link step raise / write a red line to `run-report.json` — not skip silently.
- **auth module:** move existing `_resolve_token`/`_resolve_base_url` tests onto
  `scripts/ddd/auth.py`; assert `review.py` + `upload.py` import from it.
- **`narrative status` regression:** runs without `NameError`.
- **`_resolve_ddd_dir(repo_root=…)`:** resolves the right `.canopy/ddd` from an
  arbitrary cwd; `load`/`save` honor an explicit `ddd_dir`.
- **`compute_auto_iterate`:** single-source unit test covering done / concept_change
  / unclear / stalled / continue; assert the SKILL no longer contains the logic
  (doc references the function).
- **`Gate` enum / `Finding` model:** validation + that judges/findings_review use
  them; one `derive_severity`.
- **Back-compat:** `walkthrough-eval` reads the superset manifest unchanged.

## Rollout (one plan, ordered)

1. **Manifest + engine** — add nav-tracking, mp4 offsets, manifest emission to
   `record_video.py`; superset schema. (Backbone; unblocks everything.)
2. **Deck unification** — `generate_presentation` reads the manifest; delete
   `_build_deck_run_data`.
3. **ddd consumers** — `upload.py` reads manifest for deck + links (delete
   `_external_links_from_spec`); `assemble_run_state` reads `scenes_run`/`scene_filter`;
   deck/link steps assert-not-skip.
4. **walkthrough skill migration** — thin wrapper over engine + manifest + score
   merge; drop the after-scoring video pass; verify `walkthrough-eval` /
   `defect-creator` still pass.
5. **③ auth module + NameError**, **④ cwd decoupling**, **② SKILL↔code single source**.
6. **Vocab/shape** — `Gate` enum, `Finding` model, unified `derive_severity`, phase
   doc, glossary.
7. **Note ⑤** — leave a client `resolve()` stub + a follow-up note for the
   canopy-web review-resolve API.

## Risks

- **Nav-tracking fidelity.** Capturing `urls_visited` must catch client-side
  `window.location` redirects (e.g. the audit→workflow redirect) and SPA route
  changes, not just full navigations. Mitigation: subscribe to `framenavigated` AND
  snapshot `page.url` after each action; dedupe in order; unit-test against a
  redirecting fixture.
- **Superset back-compat.** `generate_presentation` and the eval fixtures must keep
  working on the superset. Mitigation: keep every key they read today
  (`slides[].screenshot_b64`, `ai_evaluation`, `personas`, `name`, `narrative`,
  `generated_at`); only ADD keys. Golden test over an eval fixture.
- **Scoring-merge contract.** Two scorers (walkthrough skill vs ddd judges) write
  `ai_evaluation` into the manifest. Keep the field shape identical (the
  `Finding`/verdict model) so the deck renders either source uniformly.
- **Walkthrough-skill blast radius.** Migrating the skill touches the standalone
  demo flow + its evals. Mitigation: land 1–3 (engine/deck/ddd) first and verify ddd
  green; migrate the skill (step 4) only after, with the eval suite as the gate.

## Follow-up: canopy-web review-resolve API

The canopy-side review client (`scripts/ddd/review.py`) can POST a `ReviewRequest`
and POLL it to resolution (`post_review_request` / `await_resolution`), but it
**cannot resolve a gate from code**: canopy-web exposes `/api/reviews/<id>/` as
**GET/DELETE only**, and gate resolution (the implement/skip/defer decision, the
approve/redraft, the publish/hold) happens **in the canopy-web UI**.

`scripts/ddd/review.py::resolve_review` is a deliberate stub — it raises
`NotImplementedError` to document the gap rather than silently no-op. An automated
flow that needs to resolve a gate without a human at the UI (e.g. an unattended
upload that should auto-approve `external_release` under a policy, or a CI run that
pre-resolves `product_findings`) requires a **server-side resolve endpoint** on
canopy-web — a `PATCH /api/reviews/<id>/` (or a dedicated `…/resolve/` action) that
accepts a `response_json` and flips `status` to `resolved`. This is a **cross-repo
follow-up** (canopy-web change, then wire `resolve_review` to call it). Until then,
the contract is: canopy POSTs and polls; a human resolves in the UI.
