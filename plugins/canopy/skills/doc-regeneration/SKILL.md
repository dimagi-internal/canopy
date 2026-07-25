---
name: doc-regeneration
description: Audit project documentation for staleness and coverage gaps, then regenerate CLAUDE.md and learnings. Use when docs may be out of sync with actual project state.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# Doc Regeneration v2

## Purpose

Audit CLAUDE.md and docs/ against the actual project state (GitHub issues, PRs, learnings, design docs), then produce a corrected version. The goal is ensuring an AI agent starting a new session has accurate, complete context — no stale status, no missing learnings, no confusing contradictions, and no wasted context window on historical detail.

This is a two-way edit, not an append-only sync. Regeneration **adds** missing/changed facts AND **prunes** content that has stopped earning its place — superseded learnings, shipped-and-now-misleading plans, dead detail. On a fast-moving codebase the second half matters more than the first: stale docs don't just waste context, they actively misdirect the next agent.

## Modes

**Dry-run (default):** Generate all output into `docs/.dry-run/`. Nothing is committed or branched. Review the output and decide what to apply.

**Apply:** Create a `docs/regen-YYYY-MM-DD` branch, write the regenerated files, and create a PR.

To select mode, the invoker specifies `--dry-run` or `--apply` when calling the skill. Default is `--dry-run`.

## Genesis mode (no existing CLAUDE.md)

If the project has no CLAUDE.md at repo root, the skill runs in **genesis mode**:

- Checks 1 (Staleness), 3 (Checklist Drift), 4 (Compactness), and 6 (Stale-content pruning) in Phase 2 degenerate to "n/a" — there's no prior doc or accumulated docs/ to compare against. Note this explicitly in the review report; do not silently skip.
- **Check 5 (Reference Integrity) becomes mandatory, not optional.** When authoring a CLAUDE.md from scratch, the skill is transcribing facts from README, plans, and code into the highest-visibility doc in the repo. Every path, file count, table row, and external reference must be traced to a file read or shell command run in this session. No transcription from README or plan files without an independent `test -e` / `ls` / `wc -l` on the claim.
- Prefer fewer verified facts over more unverified ones. If a reference can't be verified, omit it or mark it `TODO: verify <reason>` in the draft so the user catches it in review.

## Process

### Phase 1: Read Everything (no output yet)

Read these inputs. Do NOT produce any output until all inputs are gathered:

1. **CLAUDE.md** — read the full file
2. **docs/learnings/** — read every file, note each learning's key takeaway
3. **Design-doc directories** — first discover what actually exists (`find docs -type d` / `ls`). Conventions vary: `docs/plans/`, `docs/specs/`, `docs/designs/`, `docs/superpowers/{plans,specs}/`, etc. Read headers and status sections of each (skip large bodies — first ~50 lines for context). Do not assume `docs/plans/` is the only one; on plugin-heavy repos the design docs live elsewhere.
4. **GitHub issues** — run `gh issue list --state all --limit 100` to get current state. If 100 results returned, paginate with `--limit 200`. Warn in the report if results were truncated.
5. **GitHub PRs** — run `gh pr list --state all --limit 100` to get merged/open PRs. Paginate if needed. Warn if truncated.

### Phase 2: Analyze (six checks)

**Check 1 — Staleness:**
Compare CLAUDE.md's status table against GitHub issue states. For each wave/task:
- Is the status in CLAUDE.md correct? (Open vs Done, issue number, file count)
- Does the PR link match?
- Are any completed items still marked as Open/Blocked?
- Do file counts in the Project Structure section match actual counts on disk?

**Check 2 — Coverage:**
For each learning in `docs/learnings/`:
- Is the learning's key takeaway reflected somewhere in CLAUDE.md? (Checklist item, guideline, key doc reference, etc.)
- If not, what section of CLAUDE.md should it be added to?

Also check:
- Are there patterns from merged PRs or closed issues that should be learnings but aren't?
- Are any docs/plans referenced in CLAUDE.md that don't exist or are obsolete?
- Do any learning files contradict each other? Flag contradictions explicitly.

**Check 3 — Checklist Drift:**
If CLAUDE.md contains a checklist or rules section:
- Are any checklist items never referenced in any learning file? (potentially dead rules — flag them)
- Do any learnings suggest patterns that should be checklist items but aren't? (missing rules)
- Are any checklist items redundant or overlapping?
- Note: flag but don't remove potentially dead rules — they may still apply to downstream consumers.

**Check 4 — Compactness (not line count):**
- Count total lines in CLAUDE.md and break down by section (header, status, key docs, checklists, rules, etc.) — as a *map of where the weight is*, not a budget to hit.
- **There is no fixed line target.** Do not cut accurate, load-bearing reference content to chase a number, and do not pad. A large multi-plugin system (e.g. ace, canopy) legitimately needs a longer CLAUDE.md than a single-service app; a thorough endpoint/route reference that an agent will actually consult earns its length. Compactness is about *removing waste*, not *minimizing lines*.
- The test for every line/section is **"would the next agent be worse off without this?"** If no → cut it. If yes → keep it, however long the file gets.
- Waste to remove (these are what compactness means here):
  - Completed phase/milestone tables → collapse to one-liner summaries with links to completion reports
  - Historical status / "currently working on" narration that's now shipped → delete
  - Duplication — the same fact stated in two sections → state once, cross-reference
  - Individual superseded plan references → keep the completion report, drop the dead plan link
  - Flat chronological learning lists → categorize by topic
- Signal to keep (do NOT compress these just because the file is long): accurate API/route/endpoint references, architecture decisions still in force, non-obvious constraints, anything an agent would otherwise re-derive by reading code.
- If you grow the file this run, say so explicitly in the report and justify it (e.g. "+41 lines, all newly-shipped endpoint coverage that was missing"). Growth for real coverage is correct; growth from un-pruned cruft is not.

**Check 5 — Reference Integrity:**
For every file path, directory, cross-repo reference, and link that appears in the current CLAUDE.md OR would be copied from README/plans/learnings into the regenerated CLAUDE.md:

- Run `test -e <path>` (absolute or resolved relative to repo root) before trusting the reference.
- For cross-repo references (e.g. `../ace/docs/...`), resolve and `ls` the target. Do not trust sibling-repo paths just because README mentions them.
- If a reference is broken:
  1. **Do not copy it verbatim into the regenerated CLAUDE.md.** Elevating a broken reference into the highest-visibility doc makes staleness worse.
  2. Try to find the real file: glob the parent directory for the nearest match by name or date stamp.
  3. Record the finding in the review report under **Staleness → broken references**, including: where the bad path came from (README, plan file, old CLAUDE.md), the path that was claimed, and the path that actually exists (if found).
  4. If the broken reference lives in a source-of-truth doc (README, plan), flag it as a follow-up fix to that doc — don't silently "fix it forward" by only correcting CLAUDE.md.
- File-count claims in CLAUDE.md (e.g. "10 Python files, 5 tests") must be re-counted at regen time, not copied from an older CLAUDE.md or plan. Either re-count, or omit — never transcribe stale counts.

**Principle:** the skill's own output becomes a source of truth for the next agent. A broken path written to CLAUDE.md is worse than the same broken path in README, because CLAUDE.md is loaded into every session's context.

**Check 6 — Stale-content pruning (retirement):**
The other checks mostly pull content *into* CLAUDE.md. This one looks at the standing corpus — every learning, plan, and design doc found in Phase 1 — and asks whether each still earns its keep. On a high-velocity codebase this is where most of the value is: superseded docs don't just waste reading time, they misdirect.

For each learning / plan / spec, classify it:
- **LIVING** — still an accurate description of current state. Keep as-is.
- **HISTORICAL-ACCURATE** — a point-in-time plan/spec that shipped as described. Real value as a record, but it won't match current code in detail. Keep, but recommend a one-line `**Status: shipped (PR #N, <date>) — historical record, not current-state**` banner at its top so the next reader doesn't mistake it for live design.
- **SUPERSEDED** — describes an approach later replaced (e.g. a DRF-era plan after a Ninja migration; a learning about a bug in code that no longer exists). Low/negative value. Recommend archiving (move to `docs/archive/`) or deleting.
- **CONTRADICTED** — actively wrong in a way that would misdirect an agent reading it today. Strongest retirement candidate; archive/delete or, if it must stay, annotate the wrong part inline.

Also: do any learnings contradict *each other*, or contradict current CLAUDE.md? Flag every contradiction explicitly — two sources of truth is worse than one stale one.

**Retirement is gated, never silent.** Produce a retirement table in the report (doc → classification → evidence → recommended action). Do NOT delete on your own judgment in either mode. In apply mode, the regen PR may *add status banners* to HISTORICAL-ACCURATE docs (low-risk, reversible), but SUPERSEDED/CONTRADICTED archive-or-delete actions are **presented as recommendations for the user to confirm** — list them in the PR body and let the human pull the trigger, unless the invocation explicitly authorized pruning (e.g. `--prune`). Deleting someone's design doc is high-regret and hard to reverse; recommend, don't unilaterally execute.

### Phase 3: Produce Output

Generate these files:

**`review-report.md`** — The core deliverable. Contains:
1. **Staleness findings** — table of what's wrong, with corrections
2. **Coverage findings** — table of each learning and whether it's reflected in CLAUDE.md
3. **Checklist drift findings** — dead rules, missing rules, redundancies
4. **Compactness analysis** — current line count + per-section breakdown, what waste was removed (or why none was found), and any justified growth. Do NOT report against a line-count target; report against "every line earns its place."
5. **Reference integrity findings** — broken paths, bad cross-repo refs, and which source doc propagated them. Include "verified: N references checked, M broken" even when M=0.
6. **Stale-content / retirement findings** — table of each standing learning/plan/spec → classification (LIVING / HISTORICAL-ACCURATE / SUPERSEDED / CONTRADICTED) → evidence → recommended action (keep / add status banner / archive / delete). Plus any cross-doc contradictions. If the corpus is clean (or absent), say so explicitly.
7. **Opinionated assessment** — "If I were an agent starting today, here's what would confuse me and what I'd need." Be specific and direct.
8. **Recommended changes** — bullet list of what the regenerated CLAUDE.md changes, and which retirements need the user's go-ahead.

**`CLAUDE.md`** — The regenerated version. Rules:
- Preserve the existing structure and section order exactly
- Update status information to match GitHub reality
- Compress completed milestones: replace detailed tables with one-liner summaries linking to completion reports. Only keep detailed tables for the CURRENT active phase (if any).
- Add missing learning/plan references — categorize learnings by topic rather than listing chronologically
- Do NOT add new sections unless a learning explicitly calls for one
- Do NOT remove or rewrite content that is already correct
- Mark completed/obsolete plans appropriately in Key Docs

**`changes.diff`** — A human-readable narrative diff (NOT a git diff). Organized by section, showing what changed and why. Include a summary table at the end with counts of: factual corrections, compressions, additions, structural changes, lines saved.

**`learnings/<name>.md`** (only if gaps found) — New learning docs for patterns discovered in PRs/issues that aren't captured yet. Use the existing learning format:
```
# Learning: <Title>

**Date**: YYYY-MM-DD
**Context**: <where this came from>
**Status**: <Resolved/Active>

## Problem
<what went wrong or was discovered>

## Root Cause
<why>

## Fix / Key Takeaway
<what to do differently>
```

### Phase 4: Deliver

**If dry-run (default):**
1. Create `docs/.dry-run/` directory
2. Write `docs/.dry-run/review-report.md`
3. Write `docs/.dry-run/CLAUDE.md`
4. Write `docs/.dry-run/changes.diff`
5. Write any new learnings to `docs/.dry-run/learnings/`
6. Present the review report to the user
7. Suggest: "Review `docs/.dry-run/` and compare against current docs. When ready, re-run with `--apply` to create a PR."

**If apply:**
1. Create branch `docs/regen-YYYY-MM-DD`
2. Write regenerated CLAUDE.md to repo root
3. Write any new learnings to `docs/learnings/`
4. Apply low-risk, reversible retirements directly: add `Status: shipped … historical record` banners to HISTORICAL-ACCURATE docs. Do NOT delete or move SUPERSEDED/CONTRADICTED docs unless the invocation passed `--prune` — instead include them as a checklist in the PR body for the user to confirm.
5. Commit with message `docs: regenerate documentation (staleness + coverage fixes)`
6. Create PR targeting `main`. The PR body must include: the coverage/staleness summary, the reference-integrity count, and a **"Retirements needing your call"** section listing each SUPERSEDED/CONTRADICTED doc with its recommendation — so the human can act on them in a follow-up even when this PR didn't touch them.

## Key Principles

- **Read before write.** Gather ALL inputs before producing ANY output.
- **Preserve structure.** CLAUDE.md's section order is intentional. Don't reorganize.
- **Be opinionated.** The assessment should say what would confuse a new agent, not just list facts.
- **Minimal changes.** Only change what's wrong or missing. Don't rewrite correct content.
- **Evidence-based.** Every finding must cite the source (issue number, learning filename, PR number). Every path or file reference written into the regenerated CLAUDE.md must be verified to exist on disk this session — citing README as the source is not enough if README is itself stale.
- **Compact, not short.** CLAUDE.md is loaded into every agent context, so every line must earn its place — but there is NO line-count target. Cut waste (stale status, duplication, dead detail), never cut accurate load-bearing reference. A big system gets a big-but-tight doc; a small one gets a small one. Judge by "would the next agent be worse off without this line?", not by length.
- **Prune, don't just append.** A regeneration that only adds is half-done. Each run must also ask what to retire — stale learnings, shipped plans, superseded specs. Retirement is gated (recommend; let the human confirm deletes), but never skipped.
