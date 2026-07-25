---
name: ddd-why-eval
description: |
  LLM-as-judge eval for a why_brief.yaml. Scores five weighted dimensions
  (problem_clarity .20, rationale_soundness .25, evidence_sufficiency .25,
  gap_honesty .15, user_narrative_strength .15) using the rubric.yaml bundled
  with this skill. Gated by ddd-why-qa — if QA fails, this eval is skipped.
  Emits the visual-judge verdict shape (pass | warn | fail | blocked).
  Use when asked to "eval the why-brief", "score why-brief", or after
  ddd-why-qa passes.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# DDD Why-Brief Eval

LLM-as-judge scoring of a `why_brief.yaml` against a 5-dimension rubric.
Measures whether the why-brief is compelling enough to justify building the
feature — not just structurally valid (that's ddd-why-qa's job), but
narratively strong.

**QA gate:** If ddd-why-qa returned verdict: fail, skip this eval.

## Inputs

- **`why_brief_path`** — path to `why_brief.yaml`.
- **`evidence_inventory_path`** — path to `evidence-inventory.md` and/or
  `evidence.json` (same run dir, produced by ddd-evidence-audit). Optional, but
  it is the eval's ONLY out-of-chain anchor: without it, `evidence_sufficiency`
  is capped at 3 (see Step 4) because the why-brief's own evidence-status claims
  are AI-authored text — the inflation zone.

## Procedure

### Step 0 — Check QA gate

Before scoring, verify the QA gate has been passed (the script lives in the canopy runtime):

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
# pass the file arg as an absolute path (resolved before the cd):
WHY_BRIEF_ABS="$(realpath <why_brief_path>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.why_qa "$WHY_BRIEF_ABS")
```

If the exit code is non-zero (verdict: fail), stop immediately and tell the user:

```
ddd-why-eval: BLOCKED — ddd-why-qa must pass before eval.
  blocking_reason: <why_qa blocking_reason>
  Fix the structural issues listed above, re-run /ddd-why-qa, then retry /ddd-why-eval.
```

Do not score a structurally broken why-brief.

### Step 1 — Load rubric

Read the bundled rubric:

```bash
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'] + '/skills/ddd-why-eval/rubric.yaml')"
```

Read the file at that path.

### Step 2 — Read artifacts

Read `why_brief_path` and, if provided, `evidence_inventory_path` (prefer the
machine-readable `evidence.json` next to it when present).  Hold both in context.

### Step 3 — Adversarial pass (mandatory before scoring)

You are the harshest reviewer this why-brief will ever face.  Before scoring any dimension, write three lists:

1. **Three weakest claims in the spine** — the ones most likely to be challenged in a design review.  Quote the claim text verbatim.
2. **Three ways the gaps could have been listed more honestly** — are any assumed items not surfaced as gaps?  Are any DECISION gaps hiding as RESEARCH?
3. **The binary narrative test** — would a non-technical program director, reading only the spine claims in order, understand *why* this feature deserves to be built?  YES or NO, with a one-sentence reason.

Only proceed to Step 4 after completing all three lists.

### Step 4 — Score each dimension

For each dimension in the rubric, score it 1–5 starting from `default_score: 3`:

- Apply the `anchor` text verbatim.
- Apply `deduction_rules` as hard caps when the rule fires.
- For every score ≥ 4: write a one-sentence justification a skeptical stranger would accept.
- For every score ≤ 2: write a one-sentence reason citing a specific observable problem.

**Out-of-chain anchoring for `evidence_sufficiency`** (canopy#265 item 3): an
evidence item counts as real (non-assumed) ONLY when the run's evidence
inventory — `evidence.json`, produced by ddd-evidence-audit's probes —
classifies it `documented` or `implemented`. The why-brief's own evidence
`status` field is NOT authoritative (it is AI text grading AI text). If no
evidence inventory was provided, `evidence_sufficiency` is capped at 3.

Compute `overall_score` via `overall_rule: lowest` (minimum dimension score).

### Step 5 — Compute verdict

| overall_score | verdict |
|---------------|---------|
| ≥ 4 | pass |
| 3 | warn |
| ≤ 2 | fail |

Set `verdict: "blocked"` only if Step 0 fired (QA gate failed) — at this point
the why-brief passed QA, so blocked should not occur in normal operation.

### Step 6 — Write verdict

Write `<run_dir>/verdict-why.yaml` (the unified `verdict-<kind>.yaml` naming —
canopy#265 item 1):

```yaml
schema_version: 1
kind: why
gate: advisory            # records + reports; never gates render convergence
live_state_verified: false  # grades AI text against AI text — the schema caps overall_score at 4
calibration: provisional  # rubric not yet calibrated against defect fixtures
rubric_name: ddd-why-brief
ran_at: <ISO timestamp>
why_brief_path: <input path>

adversarial:
  weakest_claims:
    - "<verbatim claim text>"
    - "..."
    - "..."
  gap_honesty_concerns:
    - "..."
    - "..."
    - "..."
  narrative_test: YES | NO
  narrative_test_reason: <one sentence>

dimensions:
  problem_clarity:        { score: N, weight: 0.20, justification: "..." }
  rationale_soundness:    { score: N, weight: 0.25, justification: "..." }
  evidence_sufficiency:   { score: N, weight: 0.25, justification: "..." }
  gap_honesty:            { score: N, weight: 0.15, justification: "..." }
  user_narrative_strength: { score: N, weight: 0.15, justification: "..." }

overall_score: N
overall_rule: lowest

verdict: pass | warn | fail | blocked
blocking_reason: <null unless blocked>

fix_recommendation: |
  <Concrete fix description addressing the lowest-scoring dimensions.
   Include [SPEC] tag for why-brief content issues.>
```

### Step 7 — Report

Print a summary:

```
Why-Brief Eval — <feature_name>
══════════════════════════════════════

  problem_clarity:         N/5  — <one-line justification>
  rationale_soundness:     N/5  — <one-line justification>
  evidence_sufficiency:    N/5  — <one-line justification>
  gap_honesty:             N/5  — <one-line justification>
  user_narrative_strength: N/5  — <one-line justification>
  ────────────────────────────────────
  Overall (lowest):        N/5

  Verdict: PASS | WARN | FAIL

  Output: <run_dir>/verdict-why.yaml
```

If verdict is `warn` or `fail`, print the `fix_recommendation`.

If verdict is `pass`, tell the user:

```
Why-brief is ready. Next step: run /ddd-why-brief to iterate if needed, or
proceed to the concept judge (/ddd-concept-eval) with any DECISION gaps surfaced.
```

## Output verdict shape

```yaml
schema_version: 1
dimensions:
  <dim_id>: { score: <float>, weight: <float> }
overall_score: <float>
verdict: pass | warn | fail | blocked
blocking_reason: <string | null>
fix_recommendation: <string | null>
```
