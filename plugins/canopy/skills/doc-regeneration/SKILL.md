---
name: doc-regeneration
description: Bring a repo's CLAUDE.md and docs to their ideal state — a compact, current-state index loaded into every session, with detail in on-demand topic docs and history left to GitHub. Audits staleness, broken references, duplication and size; restructures; adds a size guard so the file cannot regrow. Use when CLAUDE.md is large, over Claude Code's size warning, stale, or full of "why we changed it" narrative.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue — do not block on the upgrade.

# Doc Regeneration v3

## The model this skill enforces

Documentation has four layers. Each fact belongs in exactly one of them:

| Layer | Holds | Loaded |
|---|---|---|
| **`CLAUDE.md`** | The CURRENT state as an index: what the system is, how to run/test/ship it, and every rule an agent must not break, each as a short statement plus a pointer to where it is enforced and detailed. | Every session — so every character costs every session. |
| **Topic docs** (`docs/architecture/<topic>.md` or the repo's equivalent) | Current-state detail for one subject: how it works, the moving parts, the gotchas, phrased in the present tense. | On demand, when an agent works on that subject. |
| **Design specs** (`docs/**/specs/`) | The design record: why it was built this way, alternatives rejected. | Rarely; only when someone wants to dig. |
| **GitHub** (commits, PRs, issues) | History: what changed, when, what broke, what was measured, who decided. | Never by default — `git log -S`, `gh pr view`. |

**Docs describe the present. History is not documentation.** "It used to…", "this replaced…", "observed on 2026-07-26", "found by…", "cost us 52 minutes", "PR #378 fixed one site, then #421…" — all of that is already in GitHub, and putting it in an always-loaded file makes every session pay for it. A topic doc may keep a sentence of history only when an agent would otherwise *reintroduce* the mistake, and even then it is phrased as a present constraint ("don't relax this to X: Y depends on it"), not a story.

The way this goes wrong, and what this skill exists to reverse: every PR appends a paragraph to CLAUDE.md justifying itself with its own history. Each paragraph is reasonable alone; together they push the file past Claude Code's size warning (150k chars) and bury the rules in narrative.

## Modes

- **`--dry-run` (default):** write everything under `docs/.dry-run/` mirroring the repo layout (`docs/.dry-run/CLAUDE.md`, `docs/.dry-run/docs/architecture/…`). Nothing is committed.
- **`--apply`:** branch `docs/regen-YYYY-MM-DD`, write the files in place, verify (Phase 4), open a PR.
- **`--prune` (with `--apply`):** also delete docs classified SUPERSEDED/CONTRADICTED (git keeps them). Without it they are listed in the PR body for the user to confirm.

## Budget

Measure in **characters**, not lines (`wc -c`) — Claude Code's limit is in characters, and a "line" in these files is often a 3,000-char paragraph.

- **Target: CLAUDE.md ≤ 40,000 chars.** A large multi-app system can fit: ~1–2 lines per rule, one line per route, pointers for the rest.
- **Hard ceiling: 60,000 chars.** Above that the regen is not done; keep moving detail out.
- Report per-section sizes before and after.
- Every section gets the test: *would the next agent do something wrong without this in its context at session start?* If not, it belongs in a topic doc, a spec, or GitHub.

## Genesis mode (no CLAUDE.md)

If there is no CLAUDE.md, author one to this model. Staleness/pruning checks are n/a — say so. Every path, count and claim must be verified by a file read or command this session; prefer fewer verified facts to more unverified ones, and mark anything unverifiable `TODO: verify`.

## Phase 1 — Read everything (no output yet)

1. `CLAUDE.md` in full, plus any nested `CLAUDE.md` / `AGENTS.md` that load with it. `wc -c` each and sum.
2. Every existing topic doc (`find docs -maxdepth 2 -name '*.md'`, `docs/architecture/`, `ARCHITECTURE.md`, `README.md`) — you must know what already exists before deciding where a fact moves, or you will create a second copy.
3. Design-doc dirs (discover them: `docs/specs/`, `docs/superpowers/{specs,plans}/`, `docs/archive/`, `docs/learnings/`…) — headers and status only.
4. Tests and scripts that READ docs: `grep -rlE 'CLAUDE\.md|docs/' tests scripts .github 2>/dev/null`. Those files pin doc content; moving a pinned sentence breaks them.
5. `gh pr list --state merged --limit 100` and `gh issue list --state open --limit 100` — to confirm what is current. Warn if truncated.
6. Growth history: `git log --format='%h %ad' --date=short -- CLAUDE.md | head -50` and the size at a few points (`git show <sha>:CLAUDE.md | wc -c`). Report the growth rate; it tells you whether the guard in Phase 3 is needed (it almost always is).

## Phase 2 — Analyze

**Check 1 — Size map.** Chars per section and per bullet, largest first. This is where the work is.

**Check 2 — History content.** For every paragraph, separate:
- *Rule* — what is true / required now ("`Agent.workspace` is NOT NULL").
- *Enforcement / pointer* — what holds it ("`tests/test_x.py`", "`apps/foo/access.py`", "spec: …").
- *Gotcha* — a present-tense trap an agent would fall into ("the `@canopy` npm scope is someone else's — don't scope packages").
- *History* — everything else: what it replaced, when, incidents, measurements, who found it, PR chains, "before this…".

History is cut from CLAUDE.md and from topic docs. If it is genuinely useful (a real design rationale someone may want to dig into) and a spec already holds it, link the spec; otherwise GitHub has it. Never move history into a new doc just to avoid deleting it.

**Check 3 — Placement.** For each rule: does an agent need it *before* it knows what it is working on (cross-cutting invariants, CI-enforced boundaries, how to ship, safety rules)? → CLAUDE.md, one or two lines. Only relevant inside one subject? → that subject's topic doc, with CLAUDE.md keeping at most a one-line pointer in a "where to read before touching X" index. Group topic docs by subject (access/identity, chat, runners, embedding, infra, frontend…), merging into an existing doc when one covers the subject.

**Check 4 — Duplication.** The same fact in CLAUDE.md and a topic doc, or in two topic docs → keep it once, in the more specific place, and point at it.

**Check 5 — Staleness and reference integrity.** For every path, `file::symbol`, `module.function`, test name, command and URL in CLAUDE.md and in anything you write:
- paths: `test -e`; symbols: `grep -n "def <name>\|class <name>"` in the resolved file.
- A reference to code that no longer exists is a finding even when the prose around it is "history" — it is often a *current* rule whose worked example rotted. Fix the pointer to the live example or drop it.
- Claims about counts, flags, defaults and settings are re-derived from code, never copied.
- Contradictions between CLAUDE.md, topic docs and code: code wins; report each.

**Check 6 — Retirement.** Classify each standing spec/plan/learning: LIVING / SHIPPED-RECORD (keep, untouched — it is the design record) / SUPERSEDED / CONTRADICTED (would misdirect an agent today). Do not add status banners to shipped records; their being under `specs/` or `archive/` says so. Delete SUPERSEDED/CONTRADICTED only with `--prune`.

## Phase 3 — Produce

**CLAUDE.md** (restructure freely — section order and wording are not sacred; the content's correctness is):
1. One paragraph: what the repo is.
2. A short **"How to write this file"** note at the top, so the next PR does not regrow it: *current state only; a rule plus its pointer in ≤ 2 lines; detail in `docs/<topic>.md`; history goes in the PR description, not here; size is checked by `<guard>`.*
3. Architecture in a few lines, dev/test/ship commands, the cross-cutting invariants (one line each + pointer), and an **index of topic docs** ("touching auth → read `docs/architecture/access.md`").
4. Route/URL tables: one line per route. Behaviour detail moves to the topic doc.

**Topic docs** — present tense, organized by how the thing works today, each rule keeping its enforcement pointer. Move content *verbatim-in-substance*: every rule, constraint and gotcha removed from CLAUDE.md must land somewhere (Phase 4 checks this); only history is allowed to disappear.

**Size guard** (apply mode, if the repo has none): a test in the repo's own test framework that fails when CLAUDE.md exceeds the hard ceiling, with a failure message that says what to do ("move detail to docs/<topic>.md; history belongs in the PR"). Place it where CI already runs. This is the structural fix; without it the file regrows (measure Phase 1 item 6 and quote the rate in the PR).

**`review-report.md`** (dry-run and PR body):
1. Size: before/after per section, total, growth rate since last regen.
2. What moved where (CLAUDE.md section → topic doc), and what history was dropped (counts, examples).
3. Staleness / broken references found and how each was fixed; "verified: N references, M broken".
4. Duplications removed, contradictions found.
5. Retirement table.
6. What would confuse an agent starting today, even after this regen.

## Phase 4 — Verify (required before delivering)

1. `wc -c CLAUDE.md` ≤ ceiling (target noted).
2. **Conservation:** list every rule/constraint/gotcha removed from CLAUDE.md and the file+heading it now lives in. Grep for a distinctive term from each to prove it landed. Anything unaccounted for is either restored or explicitly named as dropped-history in the report.
3. Re-run reference integrity over the NEW files.
4. Run the tests found in Phase 1 item 4 (doc-pinning tests) and the new size guard. Fix the docs, not the tests, unless the test pins history.
5. Read the new CLAUDE.md top to bottom as an agent starting cold: can you find how to run, test and ship, and where to read for each subject? Fix gaps.

## Phase 5 — Deliver

- **Dry-run:** write under `docs/.dry-run/`, present the report, suggest `--apply`.
- **Apply:** branch, commit `docs: regenerate CLAUDE.md as a current-state index`, open a PR using the repo's conventions (auto-merge if CLAUDE.md says so). PR body = the report, plus "Retirements needing your call" when `--prune` was not passed.

## Principles

- **Current state, not history.** GitHub is the history.
- **Index, not encyclopedia.** CLAUDE.md points; topic docs explain.
- **Restructure, don't append.** A regen that only adds is not a regen.
- **Conserve rules, drop narrative.** No rule may silently vanish; every story may.
- **Verify every reference you write.** CLAUDE.md is the most-read file in the repo; a broken pointer there is worse than anywhere else.
- **Leave a guard.** A size check in CI, and the "how to write this file" note, so the next hundred PRs do not undo the work.
