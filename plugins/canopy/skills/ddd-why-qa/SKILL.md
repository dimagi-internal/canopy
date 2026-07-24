---
name: ddd-why-qa
description: |
  Run pure-python structural QA on a why_brief.yaml. No LLM — just rules:
  non-empty problem, non-empty rationale on every spine item, grounded items
  must have non-assumed evidence, all Gap.claim_refs must resolve. Returns a
  Verdict (pass | fail). Gates ddd-why-eval. Use when asked to "qa the
  why-brief", "validate why-brief", or after ddd-why-brief completes.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# DDD Why-Brief QA

Structural quality gate for `why_brief.yaml` before the LLM eval runs.
Pure python — deterministic, fast, no LLM calls.  Implements four rules:

1. **problem** must be non-empty (whitespace-only fails).
2. Every **SpineItem.rationale** must be non-empty.
3. Every **grounded** SpineItem must have ≥1 evidence item with `kind` != `assumed`.
4. Every **Gap.claim_ref** must resolve to an existing `SpineItem.id`.

## Inputs

- **`why_brief_path`** — path to `why_brief.yaml` (or `.json`).

## Procedure

### Step 1 — Run the QA module

Run the QA module (it lives in the canopy runtime):

```bash
# resolve the canopy runtime (scripts/ddd ships inside it):
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
# pass the file arg as an absolute path (resolved before the cd):
WHY_BRIEF_ABS="$(realpath <why_brief_path>)"
(cd "$DDD_REPO" && uv run python -m scripts.ddd.why_qa "$WHY_BRIEF_ABS")
```

The module exits 0 on pass, 1 on fail.  Capture stdout/stderr to display to the user.

### Step 2 — Parse and report the verdict

The module prints one of:

**Pass:**
```
why_qa: pass
```

**Fail:**
```
why_qa: fail
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

### Step 3 — On pass: hand off to ddd-why-eval

If `verdict: pass`, tell the user:

```
ddd-why-qa: PASS
Next step: run /ddd-why-eval for LLM scoring.
```

### Step 4 — On fail: guide the fix

If `verdict: fail`, display the `blocking_reason` and `fix_recommendation`.
Tell the user to fix the issues in `why_brief.yaml` and re-run `/ddd-why-qa`.

Do NOT proceed to `ddd-why-eval` if this QA returns `verdict: fail`.
