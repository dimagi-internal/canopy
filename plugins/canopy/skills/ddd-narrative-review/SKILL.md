---
name: ddd-narrative-review
description: |
  Narrative-agreement gate (concept_change). Posts the demo narrative to the
  review surface for the user's explicit APPROVE or REDRAFT decision BEFORE any
  rendering, judging, or gap-routing. Presents one story beat per scene (the
  concept_claim arc + per-scene features[] + actionability score) so the user can
  approve the narrative as the build plan, or send it back for a re-draft.
  On redraft: loops back to /ddd-spec. On approve: narrative is locked in.
  Must run AFTER ddd-narrative-actionability-eval passes (fail verdict blocks it).
  Use when asked to "agree on the narrative", "narrative gate", or after
  ddd-narrative-actionability-eval passes and before ddd-run.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# DDD Narrative-Agreement Gate

This gate centers the demo **narrative** — the story the demo tells to a
prospective user — and gets the user's **explicit approve/redraft decision on it
BEFORE any rendering, judging, or gap-routing**.  The narrative is the north star
and the user's irreplaceable-taste call.  Nothing should be built, rendered, or
judged until the story arc is approved.

## When to run this gate (it is now OPT-IN)

Routine narrative edits no longer pause here. `ddd-run` Step 1b
(`scripts.ddd.narrative sync`) folds any resolved **web** review edits onto the
spec and auto-posts a new version on any narrative change — local OR web — and
attaches the run to it with **no human pause**. A posted version is immediately
the current/active narrative on canopy-web. Run THIS approve/redraft gate only
when:

- **First-ever narrative for a slug** — you want the user to APPROVE the story
  arc before anything is built/rendered for the first time, or
- **The user explicitly asks** to review the narrative (e.g. "let me approve the
  story before we render").

For every other narrative change between runs, do NOT run this gate — let
`ddd-run`'s auto-version step post the new version silently. The only durable
human approval in the routine loop is the **`external_release`** gate at upload.

The posted review now carries:
- **Per-scene `features[]`** — the concrete buildable units the author declared,
  so the user can see at a glance what is being committed to.
- **Actionability score** — from the `ddd-narrative-actionability-eval` that ran
  before this gate, so the user knows whether the narrative is machine-verifiable.

## Why this gate exists

The DDD loop authors a unified spec (scenes with `concept_claim` story beats and
declared `features[]`), QA-gates it structurally and for actionability, then jumps
straight to rendering.  It never stops to ask: *"Is this the story we want to
tell?"*  That is the defect this gate fixes.

## Inputs

- **`spec_path`** — absolute path to the unified spec YAML
  (`docs/walkthroughs/<narrative-slug>.yaml`).
- **`run_id`** — the DDD run identifier from `scripts.ddd.runstate`.

## Procedure

### Step 1 — Resolve the canopy runtime

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
```

### Step 1b — Narration voice check (do this BEFORE posting)

The review surface shows, per scene, the scene's **`narrative`** field — or, if it
is empty, it falls back to the terse third-person **`concept_claim`**, which reads
as an abstract UI claim, not a story. Before posting, confirm every scene carries
a persona-voiced `narrative` beat:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC_ABS="$(realpath <spec_path>)"
(cd "$DDD_REPO" && uv run python -c "
import sys, yaml
from scripts.narrative.models import UnifiedSpec
spec = UnifiedSpec.model_validate(yaml.safe_load(open('$SPEC_ABS')))
miss = [s.title for s in spec.scenes if not (s.narrative or '').strip()]
print('scenes missing a persona-voiced narrative beat:', miss or 'none')
")
```

If any scene is missing its `narrative` (or a beat reads as a UI tour rather than
the persona *doing* something — "The page shows…" instead of "David clicks…"),
**fix the spec first** (see ddd-spec → "Narrative voice" + "Two fields per scene")
and only then post. The persona is the named subject of each beat; the story is
about the user, not the UI.

### Step 1c — Use-case soundness check (do this BEFORE posting)

The cheapest place to kill a trivial use case is here, before anything is built or
rendered. Ask the **skeptical-buyer question** of the narrative as a whole:

> *Does this walkthrough show the feature doing load-bearing work on a task that
> obviously matters — or is it exercising the feature on something trivial, where a
> buyer would think "so what"?*

Concrete tells to catch (any → recommend REDRAFT, don't just post): the feature is
invoked on an input too small to warrant it (AI assistance on a single short field;
an analysis over one or two data points); the exact task shown would be as easy
WITHOUT the feature; or the demo never reaches the scale/complexity where the
feature's value is actually felt. If you spot one, propose the redraft direction in
the review ("invoke the coach on a full multi-question application, not a one-line
answer") rather than locking a thin story.

This is the SAME judgment the concept-eval's `use_case_soundness` dimension makes on
every render (routing a thin use case to `concept_change`, which can re-open even a
locked narrative) — this gate is just the earliest, cheapest catch. A narrative
being approved here does NOT immunize it: if it still reads as trivial once rendered,
the loop will surface it again as a redraft decision.

### Step 2 — Post the narrative for review

Pass the spec path as an absolute path resolved before the `cd`:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC_ABS="$(realpath <spec_path>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.narrative post "$SPEC_ABS" "<run_id>")
```

This returns JSON with **two explicit link fields** — use the right one:

```json
{"id": "<review_id>",
 "internal_url": "<base>/review/<review_id>/",          // owner view, LEFT RAIL — give the user THIS
 "share_url":    "<base>/review/<review_id>/?t=<token>", // standalone, NO rail — externals only
 "url": "...", "share_token": "..."}
```

- **`internal_url` — present THIS to the user.** It opens inside the workbench
  with the left rail / navigation because the user is signed in. This is the
  review surface they actually want.
- **`share_url`** carries the `?t=<token>` share token, which forces standalone
  **share mode with NO left rail**, for people who are not signed in. Only hand
  this out when the user explicitly asks to share externally — never as their
  primary review link.

(The bare `url` / `share_token` fields are the raw server response, kept for
back-compat. Prefer `internal_url`. `narrative post` also prints both links to
stderr labelled "internal (owner, left rail)" / "external (share, no rail)".)

**`post` stamps `run_state.yaml` for you — do NOT stamp it by hand.** The
command writes both `narrative_review_id` (the raw review UUID) and a
token-bearing `narrative_review_url` onto the run, and sends the run's explicit
`narrative_slug` with the review so it files under the right narrative even if
the slug was renamed. Those stamps are what `ddd-upload` reads to (a) attach this
run's artifacts to the exact narrative version and (b) prove a narrative review
ran — without them, upload refuses to publish (the run would show as "no
narrative"). If `post` prints a `WARNING` that it could not find `run_state`,
the run dir is missing or the `run_id` is wrong — fix that and re-run, do not
proceed.

### Step 3 — Present the URL + inline storyboard

Before waiting for the user's response, present:

1. **The review URL** — present the **internal (owner) link**
   (`<base_url>/review/<review_id>/`, the returned `url` with the `?t=` token
   stripped), the editable web page where the user reads each story beat and can
   approve or redraft. It opens inside the workbench with the left rail. Do NOT
   present the token-bearing `?t=` link as the primary review URL — that is the
   no-rail external share link, for non-signed-in recipients only.

2. **The inline storyboard** — a brief scene-by-scene arc so the user knows
   what they are agreeing to without leaving the chat.  Include the per-scene
   `features[]` and the actionability score so the user can judge concreteness
   at a glance.  Format it as:

   ```
   Narrative storyboard — <narrative-slug>
   ══════════════════════════════════════
   Actionability score: N/5 (<pass | warn | fail>)

   Scene 1 · <scene title>
     Story beat: <concept_claim>
     Features: <F1.id> — <F1.description> [verify: <F1.verify>]
               <F2.id> — ...

   Scene 2 · <scene title>
     Story beat: <concept_claim>
     Features: ...
   ...

   ▶ Review and approve at: <internal_review_url>   (= <base_url>/review/<review_id>/ , no ?t= token)
   ```

   Make reviewing easy and inviting.  The user should be able to glance at the
   storyboard, feel whether the arc is right, and either click the link or
   approve/redraft inline.

### Step 4 — Await and apply the user's response

The orchestrator polls `review.await_resolution` (async mode) or waits for the
user's inline response.  Once resolved, write the `response_json` to a
temporary file and apply edits:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC_ABS="$(realpath <spec_path>)"
RESPONSE_JSON_FILE="$(mktemp /tmp/narrative_response_XXXXXX.json)"
# Write the resolved response_json to $RESPONSE_JSON_FILE, then:
(cd "$DDD_REPO" && uv run python -m scripts.ddd.narrative apply "$SPEC_ABS" "$RESPONSE_JSON_FILE")
```

The `apply` command folds any `narration_edits` back onto the matching scenes'
`concept_claim` in the spec and prints `{"decision": "<decision>", "edited": <N>}`.

### Step 5 — Gate: route on the decision

| Decision  | Effect |
|-----------|--------|
| `approve` | Narrative is **APPROVED** — proceed to `ddd-run` (Render + Judge). |
| `redraft` | The narrative needs a structural re-draft — **loop back to `/ddd-spec`** to re-draft the narrative from the spine. Do NOT proceed to render. |

Legacy v2 values are accepted by the `apply` command and coerced automatically:
`"agree"`/`"edit"` → `"approve"`; `"rethink"` → `"redraft"`.

**Critical:** Do NOT proceed to Render + Judge until the decision is `approve`.
A `redraft` means the story is not yet right and rendering would be waste.

### Step 6 — Report

Print a brief summary:

```
DDD Narrative Gate — <narrative-slug>
══════════════════════════════════════
  Decision:          <approve | redraft>
  Scenes edited:     <N>
  Actionability:     <N>/5 (<pass | warn | fail>)
  Review URL:        <review_url>

  <If approve:>
  Narrative approved ✓ — ready for /ddd-run.

  <If redraft:>
  Redraft requested — looping back to /ddd-spec to re-draft the narrative.
```

## Iteration behavior — how to handle the user's redraft

When the user resolves the gate with `redraft` and `apply_narrative_edits` has
written their edits back to the spec, the following rules govern the next loop
turn. They exist so the iteration is fast, faithful, and doesn't burn the user's
attention on things they've already implicitly decided.

### Always show the resulting artifact in the conversation, not just the URL

After `apply_narrative_edits` resolves and you've posted a fresh review URL,
present the **updated narrative inline in chat** alongside the URL — the new
narrative paragraph + a per-scene beat-by-beat table (number, persona, scene
title, status badge `Existing feature`/`New feature`). The status badge is the
`status` field on each narration item (`built` → `Existing feature`, `new` →
`New feature`), derived at review-build time from the why-brief (mirrors
canopy-web's `sceneIsFrontier`): a beat is `new` when its `provenance` spine item
is a gap (status != `grounded`) OR a why-brief gap references it; otherwise
`built`. Do NOT eyeball this — read `narration[].status`; it is exactly what the
BUILD SEQUENCE panel labels, so the inline table and the panel agree.
A URL alone is the wrong
shape: it forces the user to chase, and most of the time they won't react to
something they have to chase to see. The artifact has to be in front of them in
the conversation so they can react in the same turn. Pull the artifact from
the per-scene `scene.narrative` values so the inline summary matches what they
see on the review surface.

### Fix typos/grammar without asking — defer only on taste

When applying edits to per-scene fields (`narrative`, `show`, feature
`description`/`verify`), clean obvious typos, dropped words, and mangled
grammar without asking the user. Their intent is the content they typed, not
the literal characters; carrying a typo through the loop noises the
actionability eval, the rendered demo, the docs page, and every blind judge
that reads it. A typo on a load-bearing word in a feature's `verify` can fail
the build check.

Test before deferring: *would the next DDD step (actionability eval, render,
build) be cleaner if I just fixed this?* If yes, fix.

**Defer only on irreplaceable taste:**
- Direction/framing — does this beat belong? right level of abstraction?
- Named-entity choices — don't rename a persona to a specific real person, don't
  change "Kano" to a different state without explicit approval.
- External release (already a hard gate).

### Act on edits that imply new structure — don't ask first

When a user's redraft introduces a new capability, persona change, or
structural shift, propagate it through the spec **without asking**. Their edit
IS the answer; asking re-buys their attention on something they've already
decided.

- **New capability in scene narrative** → add the matching spine item (status
  `gap`) + a gap entry with concrete `proposed_action` + features on the
  relevant scene with concrete `verify`s. If the scene now has a different
  central claim, change its `provenance` to point at the new spine item.
- **Persona rename / pronoun change** → propagate through every scene's
  narration + the persona's `name`/`role`/`intro`/`org` + any role text that
  references a beat count.
- **A scene's narration covers multiple distinct claims** (different
  provenances would apply to different sub-actions) → split into one scene per
  claim before posting. Don't ask "want me to split this?" — split, then show.
  See `ddd-spec` § "Scene shape" for the split rule.
- **Retitled a scene** → regenerate the `build_order` entry for the old slug
  with the new slug (or spec_qa will reject the spec).

The exception is genuine taste calls where the user hasn't expressed a
preference: "split into 2 scenes or 3" if both are equally honest, or
"which real person should we name." Those still gate on the user.

## Important

- This gate centers the demo **narrative** and gets the user's explicit
  **approve/redraft** decision **BEFORE any rendering, building, or judging**.
- The posted review carries per-scene `features[]` and the actionability score
  from `ddd-narrative-actionability-eval` so the user can judge concreteness.
- `approve` means "the narrative is locked in and we have a build plan."
- On `redraft`, re-run `/ddd-spec` with the same `why_brief` and iterate until
  the narrative is right.
- This is a **blocking `concept_change` pause** — the DDD loop must not advance
  to Render + Judge until this gate resolves with `approve`.
