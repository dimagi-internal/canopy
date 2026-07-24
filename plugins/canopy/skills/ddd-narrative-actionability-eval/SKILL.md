---
name: ddd-narrative-actionability-eval
description: |
  LLM-as-judge actionability eval for a unified spec's narrative scenes. For each
  scene, derives a cold build plan from narration/concept_claim/show only (NOT its
  features[]), runs ~3 independent derivations for self-consistency, then scores
  against the declared features[] on 4 dimensions: coverage (.35), specificity (.25),
  correctness (.20), consistency (.20). A narrative that scores low here is too vague
  to act on and must be revised before a human reviews it. Gated by ddd-spec-qa —
  skip if QA failed. Emits verdict-actionability.yaml + actionability_findings[].
  Use when asked to "actionability eval", "can we build from this narrative", or
  "narrative actionability".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

## Calibration status

Provisional rubric; calibrate via a defect-creator analog after 3 real runs. Not yet calibrated.

# DDD Narrative Actionability Eval

LLM-as-judge scoring of a unified spec's narrative scenes (per-scene cold derivation
then comparison against declared features[]). Measures whether the **narrative is
actionable** — whether an AI reading only the narration can independently derive what
to build. Emits structured `actionability_findings[]` that identify vague spots to revise.

**Actionability gate:** A narrative that scores below the warn threshold here is too
vague to act on and must be revised before a human reviews it. Do not proceed to
`/ddd-narrative-review` or any rendering step with a failing actionability score —
the narrative must be revised first.

**QA gate:** Gated by ddd-spec-qa — skip if QA failed (spec-qa must pass before
running this eval). If ddd-spec-qa returned verdict: fail, stop immediately and tell
the user:

```
ddd-narrative-actionability-eval: BLOCKED — ddd-spec-qa must pass first.
  blocking_reason: <spec_qa blocking_reason>
  Fix the structural issues listed above, re-run /ddd-spec-qa, then retry /ddd-narrative-actionability-eval.
```

## Inputs

- **`unified_spec_path`** — path to `unified_spec.yaml`. Must contain scenes with:
  - `narration` or `concept_claim` — the narrative text for the scene
  - `show` — the browser actions that will be executed
  - `features[]` — the declared buildable features (id, description, verify)

## Procedure

### Step 0 — Check QA gate

Verify ddd-spec-qa passes before running:

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
SPEC_ABS="$(realpath <unified_spec_path>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.spec_qa "$SPEC_ABS")
```

If exit code is non-zero, stop. Do not score a structurally broken spec.

### Step 1 — Load rubric and spec

Read the bundled rubric:

```bash
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'] + '/skills/ddd-narrative-actionability-eval/rubric.yaml')"
```

Read the file at that path. Also read `unified_spec.yaml`.

### Step 2 — Per-scene cold derivation (the load-bearing step)

For EACH scene in the unified spec, perform a **cold derivation**:

**Cold derivation rules:**
- Read ONLY the scene's `narration` (or `concept_claim` if no narration field), `show`,
  and the scene `title`.
- Do NOT look at the scene's `features[]` during derivation. The goal is to determine
  what a builder would infer independently — looking at the declared features defeats
  the purpose of the eval.
- **Sandbox the derivation from the source repo.** If you dispatch subagents to derive,
  give each one ONLY the narration payload (title + show + concept_claim per scene) and
  instruct it explicitly NOT to read the codebase, grep, or open any file — derive purely
  from the text. A derivation agent that reads the implementation is no longer cold; it
  will "infer" features it actually just read, inflating coverage and consistency and
  hiding genuine narrative vagueness. (This leak was caught dogfooding the eval on DDD
  itself: agents that browsed the repo scored ~4 where blind derivation would land ~3.)
- Write out the concrete build steps you would execute to make this scene real: what
  endpoints, UI elements, data structures, or behaviors need to exist?

**Self-consistency via ~3 independent derivations:**
Run the cold derivation independently ~3 times (three separate reasoning passes, each
starting fresh from the narration only). This self-consistency check reveals whether the
narration is ambiguous — if the three derivations disagree significantly, the narration
is ambiguous and will confuse real builders too.

Record:
- `cold_plan_1`, `cold_plan_2`, `cold_plan_3` — the three lists of inferred build steps
- `consensus_plan` — the items that appeared consistently across ≥2 derivations

### Step 3 — Score each scene against declared features

For each scene, compare the `consensus_plan` (and any divergence) against the scene's
declared `features[]`. Score on the 4 rubric dimensions:

**coverage** — Did the cold derivation independently infer all the declared features?
- For each declared feature, check: is this feature implied by at least one item in
  the consensus_plan?
- Missing features → the narration is too vague to imply them.

**specificity** — Are the inferred items concrete and buildable, or hand-wavy?
- Concrete: named endpoint (`POST /tasks`), UI element (`Status dropdown`), schema
  field (`task.status: enum`), test assertion (`assert confirm_message in DOM`)
- Hand-wavy: "add filtering logic", "handle the form submission", "show results"

**correctness** — Do the inferred items match the declared feature's intent?
- For each cold-derived item, does it align with a declared feature's description
  and verify condition?
- Wrong inferences → the narration implies something different from what is declared.

**consistency** — Did the ~3 independent derivations agree?
- Items in all 3 derivations → narration is clear on this point
- Items in 2/3 derivations → slightly ambiguous
- Items in only 1 derivation → narration is ambiguous here; builders would diverge

Apply the rubric anchors and deduction rules from the loaded rubric file.

### Step 4 — Collect actionability_findings

For each dimension score ≤ 3 per scene, emit an `actionability_finding`:

```yaml
scene: <scene title>
dimension: <dim_id>
severity: high | medium | low   # high if score ≤ 1, medium if score == 2, low if score == 3
declared_features_missed:
  - <feature.id of any declared feature not inferred in cold derivation>
wrong_inferences:
  - <cold-derived item that contradicts a declared feature>
ambiguous_phrases:
  - <narration excerpt that caused divergence across derivations>
fix_recommendation: <specific rewrite suggestion for the narration/concept_claim>
```

These findings are the vague spots — the exact sentences or claims in the narration
that need to be revised to make the scene buildable.

### Step 5 — Aggregate overall score

Compute `overall_score` across ALL scenes via `overall_rule: lowest` — the minimum
dimension score across all scenes for all four dimensions.

### Step 6 — Compute verdict

| overall_score | verdict |
|---------------|---------|
| ≥ 4 | pass |
| 3 | warn |
| ≤ 2 | fail |

`verdict: "blocked"` is only set if Step 0 fired (QA gate failed).

### Step 7 — Write outputs

Write to the run dir (or alongside the spec file if no run dir is given):

**`verdict-actionability.yaml`** (visual-judge verdict shape):

```yaml
schema_version: 1
kind: actionability
gate: advisory            # records + reports; never gates render convergence
live_state_verified: false  # cold-derives from narration text, never live state — schema caps overall_score at 4
calibration: provisional  # rubric not yet calibrated against defect fixtures
rubric_name: ddd-narrative-actionability-eval
ran_at: <ISO timestamp>
spec_path: <input>

dimensions:
  coverage:     { score: N, weight: 0.35, justification: "..." }
  specificity:  { score: N, weight: 0.25, justification: "..." }
  correctness:  { score: N, weight: 0.20, justification: "..." }
  consistency:  { score: N, weight: 0.20, justification: "..." }

overall_score: N
overall_rule: lowest

verdict: pass | warn | fail | blocked
blocking_reason: <null unless verdict==blocked>

fix_recommendation: |
  <Concrete rewrite suggestions for the lowest-scoring scenes.
   Focus on: which phrases are ambiguous, which features are not implied,
   which inferences were wrong. Be specific enough that the author can
   rewrite the narration/concept_claim directly from this text.>

actionability_findings:
  - scene: <title>
    dimension: <dim_id>
    severity: high | medium | low
    declared_features_missed: [...]
    wrong_inferences: [...]
    ambiguous_phrases: [...]
    fix_recommendation: "..."
```

### Step 8 — Report

Print a summary:

```
Narrative Actionability Eval — <spec name>
══════════════════════════════════════
  Scenes evaluated: <N>

  coverage:     N/5  — <one-line justification>
  specificity:  N/5  — <one-line justification>
  correctness:  N/5  — <one-line justification>
  consistency:  N/5  — <one-line justification>
  ────────────────────────────────────
  Overall (lowest):   N/5

  Verdict: PASS | WARN | FAIL

  actionability_findings: <count> findings
  Output: verdict-actionability.yaml
```

If verdict is `warn` or `fail`:
- Print the `fix_recommendation`.
- Tell the user: "This narrative is too vague to act on in the flagged scenes. Revise
  the narration/concept_claim for the scenes listed in `actionability_findings` before
  proceeding to `/ddd-narrative-review`."

If verdict is `pass`:
- Tell the user: "Narrative is actionable — a cold reader can independently derive the
  declared features. Next step: `/ddd-narrative-review` to get explicit human agreement
  on the narrative before rendering or building."

## Output verdict shape

```yaml
schema_version: 1
rubric_name: ddd-narrative-actionability-eval
ran_at: <ISO timestamp>
dimensions:
  coverage:     { score: <float>, weight: 0.35 }
  specificity:  { score: <float>, weight: 0.25 }
  correctness:  { score: <float>, weight: 0.20 }
  consistency:  { score: <float>, weight: 0.20 }
overall_score: <float>
overall_rule: lowest
verdict: pass | warn | fail | blocked
blocking_reason: <string | null>
fix_recommendation: <string | null>
actionability_findings:
  - scene: <string>
    dimension: <string>
    severity: high | medium | low
    declared_features_missed: [<feature.id>, ...]
    wrong_inferences: [<string>, ...]
    ambiguous_phrases: [<string>, ...]
    fix_recommendation: <string>
```
