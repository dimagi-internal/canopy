---
name: ddd-gap-walk
description: |
  Pre-render gap walk for a locked DDD narrative — one cheap pass (no
  screenshots, no judges) that reads the TARGET repo's routes, views,
  operations and seed data against every scene's narration + features[], and
  writes gaps.json (scene, claim, missing capability, evidence path). Non-empty
  gaps mean the loop's next action is BUILD, not render: a judge round on a v1
  product with a missing capability only re-discovers that it is missing.
  Use after ddd-narrative-review locks the story and before ddd-run, or when
  asked to "gap walk", "is this narrative buildable", "what's missing before
  we render".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# DDD Gap Walk

On a freshly-built product the first full render + judge round (~14 min,
~600k judge tokens) routinely learned one thing: *this scene's feature does not
exist yet*. Reading the target repo says that in one pass. This skill is that
pass, run between the narrative lock and the first render.

## Inputs

- **`run_id`** — the run (its dir holds `gaps.json`).
- **`unified_spec`** — `docs/walkthroughs/<slug>.recipe.yaml` (or legacy
  `<slug>.yaml`). The narrative must be locked (approved).
- **target repo** — the product repo the spec films (usually the cwd).

## Procedure

### Step 1 — List what each scene claims

Load the composed spec (recipe + lock) and, for every scene in order, note:
its `narrative`, `concept_claim`, `features[]` (id + `verify`), `url`, the
`actions` it performs, and any `setup:` outputs (`${var}`) it relies on.

### Step 2 — Walk the target repo, scene by scene

For each scene, find the code that would have to exist for the scene to be
filmed truthfully. Read, don't guess — cite what you read:

- **route** — the scene's `url` (and every URL an action navigates to) resolves
  in the repo's URL config;
- **view / template** — the page renders the thing the narration names (the
  column, the button, the banner, the number);
- **operation** — every effecting action (fill + submit, award, approve,
  record) has a handler that performs it;
- **data** — the seed / `setup.command` produces the entities, states and
  quantities the narration asserts (a "twenty kits" claim needs twenty kits).

A scene is **covered** when every claim has code behind it. Otherwise record a
**gap** per missing capability. Classify each gap:

- `build` — the capability is unambiguous from the narration and can simply be
  implemented (a missing route, column, action, seed row).
- `decision` — the narration asks the product to do something nobody has
  decided it should (a new policy, a new workflow shape). These go to a human.

Do not score polish, wording or layout — that is the judges' job after the
render. The only question here is *can this scene be filmed at all?*

### Step 3 — Write `<run_dir>/gaps.json`

```json
{
  "narrative_slug": "<slug>",
  "target_repo": "<abs path>",
  "walked_at": "<ISO timestamp>",
  "covered": [
    {"scene": 1, "evidence": ["connect_labs/supply_chain/views.py:88 commodity_detail"]}
  ],
  "gaps": [
    {"scene": 4,
     "claim": "Hauwa awards the compliant quote from the comparison",
     "missing_capability": "no award action on the comparison page",
     "evidence": ["connect_labs/supply_chain/urls.py (no award route)"],
     "kind": "build",
     "build_hint": "POST /procurement/rounds/<id>/award/ + an Award button per row"}
  ]
}
```

Every scene appears in `covered` or `gaps`; every entry cites at least one path
you actually read.

### Step 4 — Check + stamp

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
GAPS_ABS="$(realpath <run_dir>/gaps.json)"; SPEC_ABS="$(realpath <unified_spec>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.gap_walk check "$GAPS_ABS" --spec "$SPEC_ABS" --run-id "<run_id>")
```

Exit 0 `render` · exit 1 `build` or `decide` · exit 2 invalid (fix gaps.json).
It stamps `gaps_path` / `open_gaps` on `run_state.yaml`.

## What the orchestrator does with it

| action | next step |
|--------|-----------|
| `render` | proceed to `/canopy:ddd-run` |
| `build` | implement every `build` gap as ONE batch in the target repo (one PR, one deploy; parallel fixers are fine), record the merge SHA with `judge_gate set-fix-sha`, then re-run this skill. Render only when it returns `render`. |
| `decide` | open the `concept_change` gate with the `decision` gaps (build the `build` gaps alongside once decided). A decision may also mean the narrative should change — `redraft` returns to `ddd-spec`. |
