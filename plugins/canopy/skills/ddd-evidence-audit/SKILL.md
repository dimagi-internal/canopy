---
name: ddd-evidence-audit
description: |
  Audit existing evidence (docs, code, research, Drive folders, memory) for a
  named feature. Classifies each evidence item as documented | implemented |
  assumed, writes evidence-inventory.md (human-readable) and evidence.json
  (machine-readable) into the run directory. Output feeds ddd-why-brief.
  Use when asked to "audit evidence for", "gather evidence for", or at the
  start of a DDD Phase 0 cycle.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# DDD Evidence Audit

Ground Phase 0 of DDD by cataloguing every piece of evidence that supports (or
fails to support) the feature under investigation. The output becomes the input
to `ddd-why-brief`.

## Inputs

The caller supplies:

- **`feature_name`** — the feature being investigated (e.g. "Rooftop Survey Sampling").
- **`source_pointers`** — a list of places to look for evidence.  Each pointer is one of:
  - A file path or glob (e.g. `docs/specs/sampling.md`, `commcare_connect/audit/**/*.py`)
  - A Google Drive folder ID (e.g. `1A2B3C4D5E6F...`)
  - A memory key (e.g. `memory://project_rooftop_surveys_microplanning`)
  - A plain description (used as a prompt to search the codebase)
- **`run_dir`** — directory where output files will be written (created if missing).

If called without explicit inputs, ask for the feature name and at least one source pointer before proceeding.

## Procedure

### Step 1 — Gather each source

For each source pointer in `source_pointers`:

**File paths / globs:**
```bash
# Use Glob to expand globs, then Read each matched file
# Example:
ls -la <path>   # verify existence
```
Use the Read tool on each file.  Note the file path, key sections, and any claim the content supports.

**Drive folder IDs:**
Use Drive MCP tools (when available — `mcp__plugin_ace_ace-gdrive__drive_list_folder`, `mcp__plugin_ace_ace-gdrive__drive_read_file`) to list and read files in the folder.  If Drive MCP is unavailable, log the pointer as `assumed` with note "Drive MCP not available; content unverified."

**Memory keys:**
Use the Read tool on `~/.claude/projects/*/memory/<key>.md` or equivalent.  If the file is found, its content is `documented`.  If not found, log as a gap.

**Plain-description searches:**
Use Grep to search for relevant patterns in the codebase.  Record matching files as `implemented` if they contain working code, `documented` if they contain prose/comments only.

### Step 2 — Classify each evidence item

For each piece of evidence found, emit a row with:

| Field | Values |
|-------|--------|
| `kind` | `documented` — written spec, design doc, research note, comment |
| | `implemented` — working code, deployed feature, test case |
| | `assumed` — believed true but not verified from a source |
| `ref` | Path, URL, Drive ID, or short description |
| `summary` | One sentence: what claim this evidence supports |
| `claim_hint` | The claim this most naturally backs (used by ddd-why-brief) |

**Classification rules:**
- A design spec or research doc is `documented`.
- A code file with the feature implemented is `implemented`.
- An assertion the team "knows" without a pointer is `assumed`.
- When in doubt between `documented` and `assumed`: if you can quote a specific line, it's `documented`; if you're paraphrasing something you believe, it's `assumed`.

### Step 3 — Write output files

Create `<run_dir>/` if it doesn't exist:
```bash
mkdir -p <run_dir>
```

**`<run_dir>/evidence-inventory.md`** — human-readable evidence inventory:

```markdown
# Evidence Inventory — <feature_name>
Generated: <ISO timestamp>

## Summary
- documented: N items
- implemented: N items
- assumed: N items
- Total: N items

## Items

### [EV-001] <short title>
- **kind:** documented | implemented | assumed
- **ref:** <path or URL>
- **summary:** <one sentence>
- **claim_hint:** <the claim this backs>

### [EV-002] ...
```

**`<run_dir>/evidence.json`** — machine-readable for ddd-why-brief:

```json
{
  "feature": "<feature_name>",
  "generated_at": "<ISO timestamp>",
  "items": [
    {
      "id": "EV-001",
      "kind": "documented",
      "ref": "<path or URL>",
      "summary": "<one sentence>",
      "claim_hint": "<the claim this backs>"
    }
  ]
}
```

### Step 4 — Report

After writing both files, print a summary:

```
Evidence Audit — <feature_name>
══════════════════════════════════════

  documented:  N items
  implemented: N items
  assumed:     N items
  ─────────────────────
  Total:       N items

Output:
  <run_dir>/evidence-inventory.md
  <run_dir>/evidence.json

Next step: run /ddd-why-brief to draft the why-brief from this evidence.
```

If all items are `assumed`, warn: "⚠ All evidence is assumed — the why-brief will have no grounded claims. Consider adding documented or implemented sources before proceeding."

## Allowed tools

Read, Write, Glob, Grep, Bash, Agent (for parallel file reads), and Drive MCP tools when available.
