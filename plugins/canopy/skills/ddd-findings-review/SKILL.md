---
name: ddd-findings-review
description: |
  Product-findings review gate (product_findings) — a first-class RUN-CHILD
  review for runs in review_mode: human. After /canopy:ddd-run judges an
  iteration, posts ALL PRODUCT findings to the canopy-web review surface as ONE
  run-child review (NOT a narrative version — it carries no narrative_slug).
  Findings are clustered (scene + dimension); every cluster carries inline
  evidence — a downscaled JPEG thumbnail of each scene's screenshot, plus the
  deck #scene-<N> anchor and the integer video offset (start_seconds) — so the
  user reviews from a single link with no scrubbing. The user picks
  implement / skip / defer per cluster; the apply subcommand parses
  response_json.decisions into the implement set the orchestrator applies.
  Nothing is auto-applied in human mode.
  Use when asked to "review the findings", "findings gate", "post the
  product findings", or after ddd-run judges an iteration whose spec sets
  review_mode: human.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# DDD Product-Findings Review Gate

This gate exists for **`review_mode: human`** — narratives whose spec opts out
of autonomous fix application. In autonomous mode (the default) the
orchestrator auto-applies PRODUCT findings with `fix_kind: mechanical` and
loops. In human mode, the user picks which PRODUCT findings to implement —
and they should be able to do that from **one link**, with each finding's
evidence (an inline screenshot AND the exact video moment) right there on the
card. No hand-written chat tables, no scrubbing the clip to find the scene.

## This is a RUN-CHILD, not a narrative version

The posted review is filed under the **run** (gate `product_findings`), not
under the narrative timeline. The request_json carries **no `narrative_slug`** —
canopy-web pins `narrative_slug=None, version=0` for this gate. The canopy
poster (`scripts/ddd/findings_review.py`) deliberately does not send a
narrative_slug; do not add one back. The left nav lists it under the run as
"Findings review · needs input", distinguished by `gate`.

## Why this gate exists

Judge findings reference scenes ("Scene 3: the table header uses jargon").
Reviewing them used to mean: open the deck, scroll to the scene, open the
video, drag the playhead around until the scene appears. This gate formalizes
the fix: the recorder stamps per-scene `start_seconds` into the run report and
captures a per-scene screenshot, and each posted finding cluster carries an
inline `thumb` (the downscaled screenshot), a `deck_anchor` (`#scene-<N>`,
combined with the request's `deck_url`), and an integer `video_t` (the scene's
start offset; the canopy-web player seeks there).

### The principle is universal — this gate is just the automated case

This gate only auto-fires for `review_mode: human` AND only clusters PRODUCT
findings. But "surface every finding with a clickable look-at link, never a
link-less table" applies **whenever you hand findings to the user** — autonomous
mode, a CONCEPT / USER `visual_polish` blocker the gate skips, or a plain chat
report. When the gate does not post, hand-build the per-finding links (see
`ddd-run/SKILL.md` → Step 5b → *"Always link findings when you surface them to
the user"*): `<canopy-base>/w/<id>#t=<int_seconds>` (offset from the explainer
beats, into the beat for a late moment) and/or `<DECK_URL>#scene-<N>`, plus the
`/ddd/<slug>` run page. A findings table the user can't click into defeats the
whole point.

## request_json (POSTed to canopy-web `/api/reviews/`)

```jsonc
{
  "run_id": "program-admin-report-2026-06-11-001",
  "gate": "product_findings",            // run-child marker
  "feature": "program-admin-report",     // the run's narrative/feature slug
  "iteration": 3,                         // 1-based judged iteration
  "video": { "url": ".../w/<clip-id>/content" },  // embeddable mp4
  "deck_url": ".../w/<deck-id>",          // supports #scene-N anchors
  "summary": { "concept_score": 2, "user_score": 2, "verdict": "FAIL" },
  "clusters": [                           // rides on request_json.findings
    {
      "id": "scene-9-task-completion",
      "title": "Completed audits still show enabled mutation buttons",
      "severity": "high",                 // derived: PRODUCT/CONCEPT + low score → high;
                                          //   redesign → high; options → medium; else low
      "fix_kind": "mechanical",           // mechanical | options | redesign
      "route": "PRODUCT",
      "scenes": [9],
      "suggested_fix": "Lock state: completed audits render read-only…",
      "count": 1,
      "evidence": [
        { "scene": 9,
          "thumb": "data:image/jpeg;base64,…",  // ~480px-wide JPEG q≈70 of scene_9.png
          "deck_anchor": "#scene-9",
          "video_t": 84 }                        // int seconds = scene start_seconds
      ]
    }
  ]
}
```

NO `narrative_slug` — never send one for this gate. `thumb` is omitted when the
scene snapshot is missing; `video_t` is omitted when the scene was untimed
(`--skip-empty-scenes`).

## response_json (submitted by the human)

```json
{
  "decisions": {
    "<cluster_id>": { "decision": "implement" | "skip" | null, "comment": "free text" }
  }
}
```

Per-finding: a `decision` (implement / skip; `null` = the reviewer left a comment
but didn't explicitly pick — treat as guidance to address, never auto-skip) plus an
optional `comment`. Nothing is pre-selected, and only findings the reviewer touched
appear. The `apply` subcommand parses this into `{implement, skip, commented, comments}`
— the **implement** set (honoring each finding's `comment`) is what downstream applies.

## Inputs

- **`run_id`** — a JUDGED run (`phase: judged`) from `scripts.ddd.runstate`.
  The run dir must contain `design_findings.json` (concept judge, clusters
  source), `verdict-user.yaml` + `verdict-concept.yaml` (judge `overall_score`s
  → `summary`), `run-report.json` (per-scene `start_seconds` → `video_t`), and
  the per-scene screenshots at `snapshots_iter<N>/scene_<N>.png` (with a
  fallback to the flat `snapshots/scene_<N>.png` the recorder writes today) →
  `evidence[].thumb`. The run_state must carry `iteration_decks` /
  `iteration_clips` for the current iteration (stamped by `ddd-run` Step 2b).

## Procedure

### Step 1 — Resolve the canopy runtime

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
```

### Step 2 — Check the mode (skip this gate in autonomous mode)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC_ABS="$(realpath docs/walkthroughs/<narrative-slug>.yaml)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.findings_review mode "$SPEC_ABS")
# prints: autonomous | human
```

If the mode is `autonomous`, STOP — this gate does not apply. Route findings
per the standard table in `agents/ddd.md` (mechanical PRODUCT findings
auto-apply).

### Step 3 — Post the findings review

Run from the TARGET repo's directory (the run dir resolves from the CWD's git
toplevel), with `uv run` pointed at the canopy runtime:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
(cd "$DDD_REPO" && uv run --project "$DDD_REPO" python -m scripts.ddd.findings_review post "<run_id>")
# To run from the target repo's CWD instead (so .canopy/ddd resolves there):
PYTHONPATH="$DDD_REPO" uv run --project "$DDD_REPO" python -m scripts.ddd.findings_review post "<run_id>"
```

The command:

- reads `design_findings.json` + `verdict-user.yaml` + `verdict-concept.yaml`
  + `run-report.json` + the per-scene PNGs from the run dir, and
  `iteration_decks` / `iteration_clips` from run_state;
- clusters PRODUCT findings by scene + dimension (an explicit `cluster` key
  on a finding overrides), merging fix_kind worst-of;
- **derives** each cluster's `severity` (PRODUCT/CONCEPT on a failing
  iteration → high; `redesign` → high; `options` → medium; else low);
- attaches per-cluster `evidence[]` in the contract shape:
  `{scene, thumb (inline ~480px JPEG data-URI), deck_anchor: "#scene-<N>",
  video_t (int seconds)}`;
- posts ONE **run-child** `gate: product_findings` review (NO narrative_slug)
  with `feature`, `iteration`, `video`, `deck_url`, `summary`, and the
  `clusters[]`, plus implement/skip/defer decisions per cluster and one
  overall decision;
- **stamps `run_state.yaml`** with `findings_review_id` +
  `findings_review_url` — do NOT stamp by hand;
- prints JSON: `{posted, id, clusters, internal_url, share_url, ...}`.

Flags: `--spec <path>` (when the spec isn't at
`docs/walkthroughs/<narrative_slug>.yaml`), `--deck-url` / `--clip-url`
(override the run_state URLs).

If it prints `{"posted": false, "reason": "no PRODUCT findings to review"}`,
there is nothing to gate — continue the loop on the remaining
CONCEPT/RESEARCH/DEFER routes.

### Step 4 — Present the URL + inline summary table

Present the **`internal_url`** (owner view, left rail) as the single review
link, plus a compact summary so the user can react without leaving the chat:

```
Product findings review — <narrative-slug> (iteration <N>)
══════════════════════════════════════════════════════════
Judges: concept <X>/5 · user-artifact <Y>/5 — <M> PRODUCT finding cluster(s)

| # | Cluster | Sev | Fix kind | Scenes | Evidence |
|---|---------|-----|----------|--------|----------|
| 1 | <title> | high | mechanical | 9 | thumb + deck#scene-9 · video@1:24 |
| 2 | <title> | med  | options    | 3 | thumb + deck#scene-3 · video@1:21 |

▶ Review and decide at: <internal_url>
```

The inline `thumb`, `deck_anchor`, and `video_t` per scene come from the post
output's `findings[].evidence` — the review surface renders the screenshot and
the "Watch @ m:ss" / deck deep-link buttons. Only hand out `share_url` when the
user explicitly wants to share externally.

### Step 5 — Await and apply the user's response

Poll `review.await_resolution` (or wait for the inline response). Once
resolved, write the `response_json` to a temp file and parse the selection:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
RESPONSE_JSON_FILE="$(mktemp /tmp/findings_response_XXXXXX.json)"
# Write the resolved response_json to $RESPONSE_JSON_FILE, then:
(cd "$DDD_REPO" && uv run python -m scripts.ddd.findings_review apply "$RESPONSE_JSON_FILE")
```

It parses the contract `response_json` — `decisions` keyed by `cluster_id`, with
`overall` / `notes` as siblings — and prints the machine-readable selection:

```json
{"overall": "proceed",
 "notes": "ship the read-only lock now",
 "selections": [{"cluster_id": "scene-9-task-completion", "decision": "implement"}],
 "implement": ["scene-9-task-completion"],
 "skip": ["scene-3-clarity"],
 "defer": ["user-trust"]}
```

The `implement` list is the set downstream applies.

### Step 6 — Gate: route on the selection

| Outcome | Effect |
|---------|--------|
| `overall == "proceed"` | Apply ONLY the `implement` clusters (their `suggested_fix`), log `skip` to the digest, append `defer` clusters to learnings/backlog. Then re-fire `/canopy:ddd-run` on the same scope. |
| `overall == "discuss"` | Do NOT apply anything. Surface the clusters inline and have the conversation; re-post after it resolves. |

Unknown/missing decisions are treated as `defer` — never auto-apply on
ambiguity.

### Step 7 — Report

```
DDD Findings Gate — <narrative-slug> (iteration <N>)
══════════════════════════════════════
  Overall:     proceed | discuss
  Implement:   <n> cluster(s) — <ids>
  Skip:        <n> cluster(s)
  Defer:       <n> cluster(s)
  Review URL:  <internal_url>

  <If proceed:> Applying <n> fixes, then re-firing /canopy:ddd-run.
  <If discuss:> Holding — nothing applied.
```

## Important

- This gate fires **only** when the spec sets `review_mode: human`
  (`UnifiedSpec.review_mode`; default `autonomous`). Check with
  `findings_review mode <spec_path>` — never guess.
- In human mode, **nothing is auto-applied** — not even mechanical PRODUCT
  findings. The review IS the application decision.
- CONCEPT / RESEARCH / DEFER findings are NOT part of this gate — they keep
  their standard routing (`agents/ddd.md` § Route findings).
- Per standing policy, this gate posts to the **canopy-web review surface**,
  never `AskUserQuestion`.
- This is a **run-child** review — never send `narrative_slug` for
  `gate: product_findings`. Sending one would make canopy-web treat it as a
  bogus narrative version (the bug this gate's first-class shape fixes).
- Inline `thumb`s require the per-scene screenshots
  (`snapshots_iter<N>/scene_<N>.png`, or the flat `snapshots/` fallback) and
  Pillow (a declared canopy dep). `video_t`s require the recorder's per-scene
  timings (`run-report.json` → `scenes[]`). Old runs missing either degrade
  gracefully (no thumb / no video_t for that scene) — expected, not an error.
