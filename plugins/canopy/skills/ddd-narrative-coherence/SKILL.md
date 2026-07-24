---
name: ddd-narrative-coherence
description: |
  Run narrative-coherence QA on a unified spec YAML — catches OUTCOME LEAKAGE
  (a beat asserts specific values that a later step is supposed to generate, or
  values the action this same beat performs would produce). Pure-python rules,
  no LLM. Returns a Verdict (pass | fail). Sits between ddd-spec-qa (structural)
  and ddd-narrative-actionability-eval (cold-derive), and gates both the
  actionability eval and the human narrative-review on a pass.
  Use when asked to "coherence check", "does the narrative make sense",
  "outcome leakage", or after ddd-spec-qa passes.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# DDD Narrative Coherence

A logical sense-check for the per-scene fields (`show` + `concept_claim`) of a
unified spec. Sits between structural QA (`ddd-spec-qa`) and the cold-derive
actionability eval (`ddd-narrative-actionability-eval`).

## Why it exists

`ddd-spec-qa` checks structure (every persona is defined, every provenance maps
to a spine id, every concept_claim is non-empty and ≥5 words).

`ddd-narrative-actionability-eval` checks whether an AI could rederive the
build plan from the narration (coverage / specificity / correctness /
consistency).

Neither asks: *does this narrative make sense as a forward-looking demo?*
A persona running a live demo can describe **actions they take** and **inputs
they choose**, but cannot pre-commit to **system-generated values** they would
only see after running those actions. Pre-committed values produce two bad
outcomes:
1. The rendered demo diverges from the spec (the materializer doesn't
   reproduce the cited numbers), making the eval verdicts unstable.
2. The narrative reads as a recap, not a forward demo, weakening the
   "agree to the story before we build" contract DDD relies on.

This skill is the rule-based gate that catches the most common failure mode:
**outcome leakage** — citing specific output values in per-scene fields.

## What it catches (v1 — outcome leakage)

The rule-based detector flags numeric patterns in per-scene `show` and
`concept_claim` fields that name **system-generated KPIs or counts**, while
leaving **input config values** alone. The catalog of patterns lives in
`scripts/ddd/narrative_coherence.py:OUTCOME_PATTERNS` and currently includes:

| Pattern | Why it's an outcome |
|---|---|
| `<N> work area(s)` | Per-plan work-area count is computed by the materializer. |
| `<N>% imbalance` / `imbalance <N>%` | Workload imbalance is a KPI computed after assignment. |
| `fit (score) <N>` / `★ <N>` | Fit score is a KPI computed from work areas + footprints. |
| `<N> km (max) travel` / `<N> km max spread` | Travel/spread is a KPI computed from geometry. |
| `<N> Approved + <N> In review` (etc.) | Lifecycle split is the consequence of LLO decisions. |

Inputs (e.g. `100 buildings per work area`, `opportunity 123`, `program 135`,
`balance tolerance ±10%`) are NOT flagged — they're values the persona enters,
not values the system returns.

The top-level `spec.narrative` paragraph is INTENTIONALLY NOT audited — that's
an overview field allowed to mention outcomes in passing. The discipline is
per-scene.

## What it doesn't catch yet (v2 backlog)

These need LLM judgment and are not in v1:
- **Temporal order** — a beat references state no prior beat (or initial
  demo state) produces.
- **Persona-can't-do-that** — a beat assigns an action to a persona whose
  declared role doesn't cover it.

Both can be added as a complementary LLM-judge sub-skill that runs after the
rule-based check passes.

## Inputs

- `spec_path` — path to the unified spec YAML (e.g. `docs/walkthroughs/<narrative-slug>.yaml`).

## Procedure

### Step 1 — Run the coherence module

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
cd "$DDD_REPO"
uv run --quiet python -m scripts.ddd.narrative_coherence <spec_path>
```

Exit code:
- `0` → pass; proceed to `/canopy:ddd-narrative-actionability-eval`.
- `1` → fail; the `blocking_reason` lists every leak with the scene title and the
  matched substring. Report each to the user, then loop back to `/canopy:ddd-spec`
  (or hand-edit the spec) to rewrite the offending `show` / `concept_claim`
  fields so they describe the action the persona takes, not values the action
  would produce.
- `2` → usage error.

### Step 2 — Report and gate

If `pass`: report `narrative_coherence: pass` and continue the pipeline.

If `fail`: print the `blocking_reason` verbatim. Do NOT proceed to actionability
eval or to the human narrative-review gate — the narrative must be revised first.
A revised spec must re-pass `ddd-spec-qa` and `ddd-narrative-coherence` before
re-running actionability.

## Provenance

Surfaced during DDD-on-microplans dogfooding (2026-05-29): a spec for the
ten-Kano-wards run cited per-plan work-area counts (Galinja 7 / Jibga 43 /
Gora 53 / Dawakin Gulu 6) and KPI values (Jibga 106% imbalance / 15.3 km
travel / fit 20.0, Galinja ★ 98.7, Madobi 100%) in the materialize beat —
all values the materializer would only produce after running. The user caught
it; the loop didn't. Recorded as `gap-narrative-coherence-check` in DDD's
own why-brief, then built.
