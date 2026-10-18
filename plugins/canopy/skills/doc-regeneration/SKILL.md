---
name: doc-regeneration
description: Bring a repo's CLAUDE.md and docs to their ideal state — a compact, current-state index loaded into every session, with detail in on-demand topic docs and history left to GitHub. Audits staleness, broken references, duplication and size; restructures; adds size and history guards so the file cannot regrow. Runs a full regen the first time and an incremental pass on an already-regenerated repo. Use when CLAUDE.md is large, over Claude Code's size warning, stale, or full of "why we changed it" narrative.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
_DR="$_CANOPY_PLUGIN/skills/doc-regeneration/scripts"
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue — do not block on the upgrade.

# Doc Regeneration v4

## The model this skill enforces

Documentation has four layers. Each fact belongs in exactly one of them:

| Layer | Holds | Loaded |
|---|---|---|
| **`CLAUDE.md`** | The CURRENT state as an index: what the system is, how to run/test/ship it, and every rule an agent must not break, each as a short statement plus a pointer to where it is enforced and detailed. | Every session — so every character costs every session. |
| **Topic docs** (`docs/architecture/<topic>.md` or the repo's equivalent) | Current-state detail for one subject: how it works, the moving parts, the gotchas, phrased in the present tense. | On demand, when an agent works on that subject. |
| **Design specs** (`docs/**/specs/`) | The design record: why it was built this way, alternatives rejected. | Rarely; only when someone wants to dig. |
| **GitHub** (commits, PRs, issues) | History: what changed, when, what broke, what was measured, who decided. | Never by default — `git log -S`, `gh pr view`. |

**Optimize every doc for clarity about the current state. History is wasted context.** "It used to…", "was X, now Y", "this replaced…", "observed on 2026-07-26", "cost us 52 minutes", "PR #378 fixed one site, then #421…", a completed rollout or migration runbook, a measurement of past behaviour — all of it is already in GitHub, and every reader pays for it in every doc that carries it. The codebase moves fast; a doc is only as good as how clearly it says what is true *now* and what to do *now*.

This holds for every audience, including docs outside integrators read (an embedding guide, an SDK README, a public API guide): they describe the current contract and what to do. If an older integration must change something, say it as the current requirement in the body ("hosts must send `aud` = the public base URL"), never as a changelog.

**The only exception** is a present-tense constraint that stops an agent reintroducing a mistake: "don't relax this to X: Y depends on it" — a rule, not a story.

The way this goes wrong, and what this skill reverses: every PR appends a paragraph justifying itself with its own history. Each is reasonable alone; together they push the file past Claude Code's size warning (150k chars) and bury the rules in narrative.

## Modes and scope

- **`--dry-run` (default):** write everything under `docs/.dry-run/` mirroring the repo layout. Nothing is committed.
- **`--apply`:** branch `docs/regen-YYYY-MM-DD`, write in place, verify (Phase 4), open a PR.
- **`--prune` (with `--apply`):** also delete docs classified SUPERSEDED/CONTRADICTED (git keeps them). Without it they are listed in the PR body for the user to confirm.

Scope is detected, not chosen — `regen_status.py` prints it:

- **Genesis** (no CLAUDE.md): author one to this model. Every path, count and claim is verified by a file read or command this session; prefer fewer verified facts to more unverified ones; mark anything unverifiable `TODO: verify`.
- **Full** (first regen): all phases over every doc.
- **Incremental** (already regenerated: CLAUDE.md carries the "How to write this file" note and/or a size guard exists, and a prior regen commit is found): review only what changed since the last regen — `git diff <last-regen> -- CLAUDE.md` and the topic docs that diff lists — plus the full reference check and history scan over CLAUDE.md and every topic doc. Restructuring untouched docs is out of scope unless a check flags them.

## Budget

Measure in **characters** (`wc -c`), not lines — Claude Code's limit is in characters, and a "line" here is often a 3,000-char paragraph.

- **CLAUDE.md target ≤ 40,000; hard ceiling 60,000.** Above the ceiling the regen is not done. A large multi-app system fits: 1–2 lines per rule, one line per route, pointers for the rest.
- **Topic doc soft warning: 50,000 each.** Report any above it and recommend a split by sub-subject, or a searchable index at its top; the CLAUDE.md pointer to it says *grep it, don't read it whole*. Report, don't force — a long reference doc can be right.
- Every section faces one test: *would the next agent do something wrong without this in its context at session start?* If not, it belongs in a topic doc, a spec, or GitHub.

## Tools (use these; don't hand-roll greps)

Three stdlib-only scripts in `$_DR` (set in the Preamble). Run them from the repo root. The shell may be zsh: quote every argument, don't print unquoted separators like `echo ====`, and write files with a heredoc, never a bare `cat > file`.

```bash
python3 "$_DR/regen_status.py"                                  # mode, last regen, growth, changed docs, topic-doc sizes, doc-pinning tests
python3 "$_DR/check_refs.py" CLAUDE.md docs/architecture/*.md   # broken path / path::symbol / pkg.mod.func references
python3 "$_DR/find_history.py" CLAUDE.md docs/architecture/*.md # dated, PR-numbered and "used to / replaced / fixed by" lines
```

- `check_refs.py` resolves backticked tokens and relative link targets: repo paths (also relative to the doc), `path::symbol`, and dotted forms (`apps/agents/access.decide`, `apps.agents.access.decide` → module file, then `def|class|name =`). It skips placeholders (`< * { … $`), routes and other repos' paths. Output `MISSING`/`SYMBOL` lines and `checked N, broken M`; exit 1 if broken.
- `find_history.py` flags ISO dates, `#1234` / `repo#1234`, and history phrases, but not dated spec filenames/links or section numbers. Output `file:line: [kinds] text` and a per-file count. A flag is a candidate, not a verdict — judge each line.
- `regen_status.py --since <ref>` overrides the detected last regen.

## Phase 1 — Read (no output yet)

1. `regen_status.py` — record the mode, the last regen, the changed-docs list and the growth line.
2. CLAUDE.md in full, plus any nested `CLAUDE.md` / `AGENTS.md` that load with it. **Full:** every topic doc (`docs/architecture/`, `ARCHITECTURE.md`, `README.md`, `find docs -maxdepth 2 -name '*.md'`) — know what exists before deciding where a fact moves, or you create a second copy. **Incremental:** the changed docs; the rest only where a check points.
3. Design-doc dirs (`docs/specs/`, `docs/superpowers/{specs,plans}/`, `docs/archive/`…) — headers and status only.
4. Tests and scripts that READ docs (`regen_status.py` lists those naming CLAUDE.md; also `grep -rlE 'docs/' tests scripts .github`). They pin doc content; moving a pinned sentence breaks them.
5. `gh pr list --state merged --limit 100` and `gh issue list --state open --limit 100` — to confirm what is current (incremental: merged since the last regen). Warn if truncated.

## Phase 2 — Analyze

**Check 1 — Size map.** Chars per section and per bullet, largest first; topic docs over the soft warning.

**Check 2 — History.** Run `find_history.py`, then read each paragraph and separate: *rule* (what is true now), *enforcement/pointer* (what holds it), *gotcha* (a present-tense trap), *history* (everything else: what it replaced, "was X, now Y", when, incidents, measurements of past behaviour, who decided, PR chains, completed rollout or migration runbooks). History is cut from CLAUDE.md and every topic doc, integrator-facing ones included; a migration note becomes the current requirement it implies, or goes. If real design rationale is worth keeping and a spec holds it, link the spec; never move history into a new doc to avoid deleting it.

**Check 3 — Placement.** Needed *before* an agent knows what it is working on (cross-cutting invariants, CI-enforced boundaries, how to ship, safety rules)? → CLAUDE.md, one or two lines. Only inside one subject? → that subject's topic doc; CLAUDE.md keeps at most a one-line pointer in a "where to read before touching X" index. Merge into an existing topic doc when one covers the subject.

**Check 4 — Duplication.** The same fact in CLAUDE.md and a topic doc, or in two topic docs → keep it once, in the more specific place, and point at it.

**Check 5 — Staleness and references.** Run `check_refs.py` over CLAUDE.md and every topic doc (both modes). A broken reference is a finding even inside "history" prose — it is often a current rule whose worked example rotted: fix the pointer or drop it. Claims about counts, flags, defaults and settings are re-derived from code, never copied. Contradictions between CLAUDE.md, topic docs and code: code wins; report each.

**Check 6 — Retirement.** Classify each standing spec/plan/learning: LIVING / SHIPPED-RECORD (keep untouched — it is the design record) / SUPERSEDED / CONTRADICTED (would misdirect an agent today). No status banners on shipped records. Delete SUPERSEDED/CONTRADICTED only with `--prune`. Incremental: only specs added or referenced since the last regen.

## Phase 3 — Produce

**CLAUDE.md** (section order and wording are not sacred; correctness is):
1. One paragraph: what the repo is.
2. A **"How to write this file"** note at the top: *current state only; a rule plus its pointer in ≤ 2 lines; detail in `docs/<topic>.md`; history goes in the PR description, not here; size and history are checked by `<guard>`.*
3. Architecture in a few lines, dev/test/ship commands, cross-cutting invariants (one line each + pointer), and an **index of topic docs** ("touching auth → read `docs/architecture/access.md`").
4. Route/URL tables: one line per route; behaviour detail lives in the topic doc.

**Topic docs** — present tense, organized by how the thing works today, each rule keeping its enforcement pointer. Move content *verbatim-in-substance*: every rule, constraint and gotcha removed from CLAUDE.md lands somewhere; only history may disappear.

**Guards** (apply mode; add whichever the repo lacks, in its own test framework, where CI already runs):
- **Size guard:** fails when CLAUDE.md exceeds the ceiling; message: "move detail to docs/<topic>.md; history belongs in the PR description".
- **History guard:** fails when CLAUDE.md contains an ISO date or a PR reference like `(#1234)`; message: "CLAUDE.md is current state only — state the rule as it holds now; put when and why it changed in the PR description".

## Phase 4 — Verify (required before delivering)

1. `wc -c CLAUDE.md` ≤ ceiling (target noted); topic docs over the soft warning are reported.
2. **Conservation, concretely:** a table with one row per rule you REWROTE or MOVED — `before (old wording, abridged) | after (new wording or file#heading)`. Grep a distinctive term from each "after" to prove it landed. A rule with no row was either untouched or is restored; dropped history is counted separately with examples. "Nothing was dropped" without the table is not verification.
3. Re-run `check_refs.py` and `find_history.py` over the NEW files; every remaining flag is fixed or justified in the report.
4. Run the doc-pinning tests from Phase 1 and the guards. Fix the docs, not the tests, unless a test pins history.
5. Read the new CLAUDE.md top to bottom as an agent starting cold: can you find how to run, test and ship, and where to read for each subject? Fix gaps.

## Phase 5 — Deliver

**Report** (`docs/.dry-run/review-report.md` on dry-run; the PR body on apply):
1. Mode and scope (incremental: the last-regen commit and the diff reviewed).
2. Size: before/after per section and total; topic docs over the soft warning with a split/index recommendation.
3. **Growth trajectory:** chars/day since the last regen that rewrote CLAUDE.md, the projected dates for the target and the ceiling (from `regen_status.py`), and when to run the next regen — before the projected target date.
4. The conservation table; what moved where; history dropped (counts, examples).
5. References: "checked N, broken M" before and after, and how each break was fixed.
6. Duplications removed, contradictions found, the retirement table.
7. **Steps not performed:** every phase step skipped or cut short — e.g. the PR/issue cross-check, count/flag re-derivation, the duplication check, retirement — each with why. Silence means it was done.
8. What would still confuse an agent starting today.

**Commit and PR title describe the actual diff**, in every mode — the change, not the skill's name. "docs: regenerate CLAUDE.md as a current-state index" fits only a diff that does that; an incremental pass that strips topic-doc history and fixes references is "docs: strip history from topic docs, fix stale refs". End the commit message with a `Doc-Regen: full|incremental` trailer — the next run finds its baseline by it.

- **Dry-run:** write under `docs/.dry-run/`, present the report, suggest `--apply`.
- **Apply:** branch, commit, open a PR with the repo's conventions (auto-merge if its CLAUDE.md says so); add "Retirements needing your call" when `--prune` was not passed.

## Principles

- **Clarity about the current state is the goal.** Every line of history is context a reader pays for and does not need; GitHub keeps it.
- **Index, not encyclopedia.** CLAUDE.md points; topic docs explain.
- **Restructure, don't append.** A regen that only adds is not a regen.
- **Conserve rules, drop narrative** — and show the before→after for each rule you touched.
- **Verify every reference with the tool**, not by eye.
- **Say what you skipped.** An unreported skip reads as done.
- **Leave guards.** Size and history checks in CI, plus the "how to write this file" note, so the next hundred PRs do not undo the work.
