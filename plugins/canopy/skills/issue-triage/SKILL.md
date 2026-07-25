---
name: issue-triage
description: Use when asked to triage, review, or clean up a GitHub repo's open issues against the current code — scan all open issues, evaluate each against the latest code, and recommend implement / investigate / close (no longer relevant), then act on the recommendations behind gates (close obsolete issues with a reasoned comment, comment + label the ambiguous ones, open draft PRs for the ones worth building). Defaults to the current repo's origin; pass an explicit owner/repo to point it elsewhere. Built to close the loop on issues ACE files as it runs.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue — do not block on the upgrade.

# Issue Triage — evaluate a repo's open issues against the current code

## Purpose

Point canopy at a GitHub repo, pull **all open issues**, evaluate each one
against the **latest code**, and recommend per-issue:

- **implement** — still valid, actionable, not yet done
- **investigate** — can't decide without a repro / more info / scope clarification
- **close** — already fixed/implemented in code, obsolete, or a duplicate (no longer relevant)

Then, behind per-group gates, act on the recommendations: close the obsolete
ones with a reasoned comment, comment + label the ambiguous ones, and open
**draft** PRs for the ones worth building.

This is the **inverse** of `canopy:pm-scout` / the `product-management` skill,
which explores the codebase for *new* work. Issue-triage triages *existing*
issues. Built to close the loop on issues ACE files as it runs.

## Critical rules

- **Read-only until the gate.** Phases 0–3 perform **no** GitHub writes. The
  only mutations (`gh issue close`, `gh issue comment`, label edits, opening
  PRs) happen in Phase 5, and only after the user approves that group.
- **Every `close` needs code evidence.** A close recommendation must cite the
  `file:line` that already resolves the issue. No evidence → downgrade to
  `investigate`.
- **No silent truncation.** If the repo has more open issues than the cap, say
  so explicitly in the report ("triaged 30 of 47 open issues").
- **PRs are drafts.** The implement path opens draft PRs that reference the
  issue; it never auto-merges. The merge decision stays with the human.
- **One repo per run.** No org-wide or cross-repo sweeps in a single invocation.

## Phase 0 — Pre-flight (one sequential bash block, NEVER parallel)

Run this synchronously before any other tool calls:

```bash
# 1. gh must be authenticated
gh auth status >/dev/null 2>&1 || { echo "PREFLIGHT: gh-unauthenticated"; exit 0; }

# 2. Resolve target slug. ARG is the owner/repo passed to the command (may be empty).
ARG="<owner/repo arg, or empty>"
if [ -n "$ARG" ]; then
  SLUG="$ARG"
else
  SLUG=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)
fi
[ -z "$SLUG" ] && { echo "PREFLIGHT: no-target (run inside a repo or pass owner/repo)"; exit 0; }

# 3. Is the target the repo we're standing in?
LOCAL_SLUG=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)
if [ "$SLUG" = "$LOCAL_SLUG" ]; then echo "CODE: local"; else echo "CODE: remote"; fi
echo "SLUG: $SLUG"
```

Branch on output:
- `PREFLIGHT: gh-unauthenticated` → tell the user to run `gh auth login` (or
  `/canopy:auth-preflight`), then stop.
- `PREFLIGHT: no-target` → ask for an `owner/repo`, then stop.
- Otherwise capture `SLUG` and whether the code is `local` or `remote`.

## Phase 1 — Gather open issues

```bash
gh issue list --repo "$SLUG" --state open --limit 30 \
  --json number,title,body,labels,createdAt,updatedAt,comments
```

- Default cap is **30**. If the command arg carried a `--limit N`, use it.
- Get the total open count to detect truncation:
  ```bash
  gh issue list --repo "$SLUG" --state open --limit 1 --json number | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" >/dev/null
  gh api "repos/$SLUG" -q .open_issues_count   # includes open PRs; treat as an upper bound
  ```
  If the fetched count is less than the number of open issues, note the
  truncation in the report.
- If there are **zero** open issues, report that and stop — nothing to triage.

## Phase 2 — Resolve the code to evaluate against

- **`CODE: local`** → search the current working tree directly (Grep/Glob/Read).
- **`CODE: remote`** → shallow-clone into a temp dir and search there:
  ```bash
  TMP=$(mktemp -d)
  git clone --depth=1 "https://github.com/$SLUG" "$TMP/repo" >/dev/null 2>&1
  echo "$TMP/repo"
  ```
  Remember to `rm -rf "$TMP"` at the end of the run.

## Phase 3 — Evaluate each issue (fan-out, read-only)

Dispatch **one subagent per issue** (use the Agent tool; for many issues,
batch so a handful run concurrently). Give each subagent:
- the issue: number, title, body, labels, and existing comments
- the path to the code (working tree root, or the cloned temp repo)
- the rubric and the required output shape below

**Subagent instructions (per issue):**
> Evaluate GitHub issue #N against the code at `<path>`. Search the code for
> the behavior, files, symbols, or error the issue describes. Decide ONE
> disposition:
> - **close** — the code already does what the issue asks, the behavior it
>   describes no longer exists, or it duplicates another open issue. You MUST
>   cite the `file:line` that resolves it.
> - **implement** — the request is still valid and not yet satisfied by the
>   code. Estimate effort S (<1hr) / M (2–4hr) / L (day+).
> - **investigate** — you cannot adjudicate from the code alone (needs a repro,
>   under-specified, or depends on an external system). Say what's missing.
>
> Return strictly: `number`, `disposition`, `confidence` (high/medium/low),
> `effort` (S/M/L or n/a), `evidence` (list of `file:line` + one-line note),
> `reasoning` (1–3 sentences). Do not modify anything. Do not call any `gh`
> write command.

Collect all verdicts.

## Phase 4 — Report

Print a table to chat, **close-candidates first** (cheapest wins), then
investigate, then implement:

```
#   Disposition   Conf    Effort  Title                         Evidence / why
--  -----------   ----    ------  ----------------------------  --------------------------
12  close         high    —       "Crash on empty config"       config.py:88 already guards
 7  close         med     —       "Add --json flag"             cli.py:210 flag exists
...
 4  investigate   med     —       "Slow on large datasets"      no repro; needs dataset
...
19  implement     high    S       "Typo in error message"       errors.py:44 still wrong
```

If issues were truncated, print the "triaged X of Y" line above the table.

Write the same content as a run log:
- `CODE: local` → `<repo-root>/.canopy/issue-triage/runs/YYYY-MM-DD.md`
  (create the dir; commit it alongside any other working-tree changes).
- `CODE: remote` → `$HOME/.canopy/issue-triage/<owner>-<repo>/YYYY-MM-DD.md`.

Use the current date from the environment context for `YYYY-MM-DD` (do not call
a date command in a way that breaks determinism — the conversation provides
today's date).

## Phase 4.5 — Recommend an action per issue (then offer to do it all)

Don't stop at dispositions. After the report, state a concrete **recommended
next action** for every issue, so the user can approve in one shot. Default
operating assumption: the user will usually say *"do everything you're
recommending"* — so make the list **executable, not advisory**.

- **implement → a PR you're confident in:** recommend **merging** (mark the
  draft ready + arm auto-merge). "Confident" means the project's validation
  passed AND the change required no unverifiable guess. Be honest about the
  exception: a draft you flagged as **unvalidated** — e.g. a recipe/selector
  gesture not yet confirmed on a live device, per a target repo's "validate
  live before shipping" rule — is the ~1-in-10 case. For it, recommend
  **hold + the specific validation step**, never a rubber-stamp merge.
- **investigate:** recommend the concrete **next step** — what to run, on which
  surface, and what evidence to capture to adjudicate it. Name the command /
  repro / dump; don't just restate "needs more info."
- **close:** already actioned in Phase 5 — no separate recommendation needed.

Then present **one consolidated gate**: "Do all of the above?" (Approve all /
Let me pick / Skip). On *Approve all*, execute every recommendation — arm
auto-merge on the confident PRs, post any next-step plans, run whatever is
executable in-session. For steps gated on a live device / fresh run you can't
drive headlessly, say so explicitly and hand back the exact command for the
user to kick off. This consolidated gate is in ADDITION to the per-group gates
in Phase 5 below (those still apply to the close / investigate writes); think of
Phase 4.5 as the "what should happen next, and shall I just do it" close.

## Phase 5 — Act (gated, grouped by disposition)

Confirm **each non-empty group separately** via its own `AskUserQuestion`, so
outward-facing actions are gated and individually overridable. Each question
offers: **Approve all / Skip this group / Let me pick** (Other → name the
specific issue numbers to act on).

After getting the disposition for a group, take the action:

**close group**
```bash
gh issue close <n> --repo "$SLUG" \
  --comment "Triaged against current code: <one-line reason>. Evidence: <file:line>. Closing as no longer relevant — reopen if this is wrong."
```
Optionally add a label first: `gh issue edit <n> --repo "$SLUG" --add-label "triage:obsolete"` (skip if the label doesn't exist rather than failing the run).

**investigate group**
```bash
gh issue comment <n> --repo "$SLUG" \
  --body "Triage couldn't adjudicate this from the code alone. To proceed we need: <what's missing>."
gh issue edit <n> --repo "$SLUG" --add-label "needs-info"   # skip if label absent
```
Leave the issue open.

**implement group**
For each approved issue, follow the `product-management` skill's Phase 4/5
implement+ship conventions:
- branch `<prefix>/<issue-slug>` off the default branch (never commit to main)
- implement the change, run the project's full validation (lint + build + tests)
- open a **draft** PR whose body references the issue (`Refs #<n>` — or
  `Closes #<n>` only if you're confident it fully resolves it)
- do one issue at a time; if validation can't pass after 2 attempts, stop and
  report rather than thrashing
- **then honor the Phase 4.5 recommendation:** if you're confident in the PR
  (validation passed, no unverifiable guess) and the user approved, mark it
  **ready** and arm auto-merge (`gh pr ready <n>` + `gh pr merge <n> --auto
  --merge`, matching the target repo's merge convention). If you flagged it
  **unvalidated** (a guess that needs live-device / runtime confirmation),
  keep it a draft and recommend the validation step instead — never
  auto-merge an unverified change.

Read `skills/product-management/SKILL.md` from the same install path if you need
the full implement/ship detail — do not reimplement branch/PR logic from memory.

After acting, update the run log's "action taken" column for each issue.

## Cost discipline

- Phase 1: one `gh issue list`.
- Phase 3: one subagent per issue — the bulk of the cost. Respect the cap; for
  very large backlogs, triage the cap and tell the user how many remain.
- Phases 0/2/4/5: cheap.
