---
name: ddd-spec-qa
description: |
  Run pure-python structural QA on a unified spec YAML. No LLM — just rules:
  delegates to validate() for persona-defined, provenance-to-spine-id, and
  required-field checks; adds falsifiability check on every Scene.concept_claim
  (fails on empty, whitespace, banned marketing phrases, or fewer than 5 words).
  Returns a Verdict (pass | fail). Gates the concept judge (ddd-concept-eval).
  Use when asked to "qa the spec", "validate spec", or after ddd-spec completes.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# DDD Unified Spec QA

Structural quality gate for `docs/walkthroughs/<narrative-slug>.yaml` before the
concept judge runs.  Pure python — deterministic, fast, no LLM calls.

Implements two layers of checks:

**Layer 1 — delegated to `scripts.ddd.validate` (provenance + persona + schema):**
1. Every `Scene.persona` must be a key in the `personas` dict.
2. When a `why_brief` is declared and resolvable, every `Scene.provenance` must
   match a `SpineItem.id` in that why_brief.
3. All Pydantic-required fields (`name`, `narrative`, `base_url`, `personas`,
   `scenes`) must be present and well-typed.

**Layer 2 — QA-specific (falsifiability of concept_claims):**
4. Every `Scene.concept_claim` must be **non-empty** (whitespace-only fails).
5. Every `Scene.concept_claim` must be **falsifiable** — fails if it:
   - Contains a banned marketing phrase: "world-class", "seamless", "powerful",
     "robust", "best-in-class", "cutting-edge", "revolutionary", etc.
   - Is fewer than 5 words (too short to be a specific, testable claim).

Note: verb-pattern detection is intentionally absent.  It blocked legitimate
nominalized domain claims ("GPS pinning accuracy within 5 meters") while
accepting articulate-but-empty fluff ("The system is good" — copula + adjective).
Subtle vacuousness judgment belongs to the LLM concept judge (SP3).

**Layer 2 — QA-specific (data-setup contract + late binding, order-aware):**
6. If any scene `url` or action `target`/`value` uses a `${...}` placeholder, the
   variable must be provided — either by a `setup:` block's `outputs:` (the
   synthetic generator that mints it before the render) OR by an on-camera
   `capture` action in an EARLIER scene (see ddd-spec Step 5, "Data setup
   contract" + "Capture + late binding"). Validation is **order-aware**: a
   `${var}` is valid iff a setup output OR an earlier `capture` provides it — a
   var referenced before anything binds it is rejected (it would film a literal
   `/runs/${run_id}/` URL). A var bound only by `capture` needs no `setup:` block
   at all. The converse is fine: declared-but-unused outputs are not an error.

**Layer 2 — QA-specific (show, don't tell / action-fidelity):**
7. A scene that **declares actions** but scripts ONLY non-effecting ones
   (`hover` / `scroll_to` / `wait_for`) while its `narrative` / `concept_claim`
   promises an **effecting verb** (create / fill / submit / award / select /
   publish / enter / type) fails — it is a hover-only "claimed, not shown" demo
   (the judge sees the same end-frame whether the form was filled or merely
   hovered over). Fix: add the `fill`/`click` that effects the narrated act, OR
   soften the narration to match what the demo does. This is **scoped to the
   actions list** — a scene with NO actions is exempt (a legacy scroll-pan
   narrative beat), and it is NOT a prose-only verb check (the removed
   falsifiability verb-check false-positived on nominalized claims).

These rules exist because concept_claims are the testable hypotheses that the
concept judge (SP3) will score.  A non-falsifiable claim cannot be judged.

## Inputs

- **`spec_path`** — path to the unified spec YAML (e.g. `docs/walkthroughs/<narrative-slug>.yaml`).
  The why_brief (if declared) is resolved automatically from the spec file's
  `why_brief` field relative to the spec file — no separate path argument needed.

## Procedure

### Step 1 — Run the QA module

Run the QA module (it lives in the canopy runtime):

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
# pass the file arg as an absolute path (resolved before the cd):
SPEC_ABS="$(realpath <spec_path>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.spec_qa "$SPEC_ABS")
```

The module exits 0 on pass, 1 on fail, 2 on usage error.  Capture stdout/stderr
to display to the user.

### Step 2 — Parse and report the verdict

The module prints one of:

**Pass:**
```
spec_qa: pass
```

**Fail:**
```
spec_qa: fail
  blocking_reason: <semicolon-separated list of violations>
  fix_recommendation: <guidance>
```

Report the verdict verbatim.  The Verdict object shape is:

```yaml
schema_version: 1
dimensions: {}           # empty for QA (no LLM scoring)
overall_score: 1.0       # 1.0 on pass, 0.0 on fail
verdict: pass | fail
blocking_reason: <null on pass, string on fail>
fix_recommendation: <null on pass, string on fail>
```

### Step 3 — On pass: hand off to concept judge

If `verdict: pass`, tell the user:

```
ddd-spec-qa: PASS
Next step: run /ddd-concept-eval to score concept_claims against the walkthrough.
```

### Step 4 — On fail: guide the fix

If `verdict: fail`, display the `blocking_reason` and `fix_recommendation`.
Tell the user to fix the issues in the spec file and re-run `/ddd-spec-qa`.

**Common fixes by violation type:**

- `concept_claim is empty` → write a specific, observable outcome for this scene.
- `concept_claim is not falsifiable` → remove banned phrases and ensure the claim
  is at least 5 words; write a specific, testable outcome
  (e.g. "Users can filter tasks by status and see only open items").
- `scene references undefined persona` → add the persona to `personas` or fix the
  scene's `persona` key.
- `provenance ... does not match any SpineItem.id` → update the provenance to
  match a valid spine id from the linked why_brief.
- `why_brief declared but not resolvable` → fix the relative path or supply
  `why_brief_path` explicitly.
- `spec uses ${...} placeholder(s) ... that no capture action binds, but
  declares no setup: block` (or `setup.outputs is not declared`) → declare the
  synthetic generator in `setup.command` + point `setup.outputs` at the flat
  JSON it emits, add a `capture` action that mints the var on camera, or remove
  the placeholders if the URLs are genuinely static.
- `url references ${var} but nothing provides it yet` / `... action references
  ${var} but nothing provides it yet` → order-aware violation: the var is used
  before any setup output or earlier `capture` binds it. Move the `capture`
  before the use, or bind it via `setup.outputs`.
- `scene '...' narrates '<verb>' but performs no effecting action` → the scene's
  actions are hover/scroll/wait only while the narration promises an effecting
  act. Add the `fill`/`click`/`select` that effects it, or soften the narration
  to match what the demo actually does.

Do NOT proceed to the concept judge if this QA returns `verdict: fail`.
