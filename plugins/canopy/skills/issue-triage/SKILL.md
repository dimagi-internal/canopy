---
name: issue-triage
description: Use when asked to triage, review, or clean up a GitHub repo's open issues AND open pull requests against the current code — scan every open issue, evaluate it against the latest code, and recommend implement / investigate / blocked / close; scan every open PR and recommend merge / unblock / needs-work / close. Then act behind gates (close obsolete issues with a reasoned comment, comment + label the ambiguous ones, route externally-blocked ones to a validation queue, unblock and merge finished PRs stranded on mechanical CI gates, open PRs for the ones worth building). Declares a scope contract up front so a triage stays a triage. Defaults to the current repo's origin; pass an explicit owner/repo to point it elsewhere. Built to close the loop on the issues and PRs agents leave behind as they run.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue — do not block on the upgrade.

# Issue Triage — evaluate a repo's open issues **and open PRs** against the current code

## Purpose

Point canopy at a GitHub repo, pull **all open issues and all open PRs**,
evaluate each against the **latest code**, and recommend a disposition.

**Per issue:**

- **implement** — still valid, actionable, not yet done, **and fixable from here**
- **blocked** — the fix is known but cannot be validated from this session; it
  needs a live surface (a device, a live upstream form, a fresh run, someone
  else's permission grant). See § The `blocked` disposition.
- **investigate** — can't decide without a repro / more info / scope clarification
- **close** — already fixed/implemented in code, obsolete, or a duplicate

**Per open PR** (see § Phase 3b):

- **merge** — finished, green, still wanted → land it
- **unblock** — finished and correct, failing only a *mechanical* gate (a
  missing VERSION bump, a docs-sync acknowledgement, a stale base). A small
  deterministic fix makes it mergeable. **This is the highest-value
  disposition in the whole skill.**
- **needs-work** — a real test/review failure, or the change is wrong
- **close** — superseded, obsolete, or its issue is gone
- **blocked** — correct but unmergeable until a live surface validates it

Then, behind per-group gates, act: close the obsolete issues with a reasoned
comment, comment + label the ambiguous ones, route the blocked ones to a
validation queue, **unblock and merge the finished PRs**, and ship the issues
worth building.

This is the **inverse** of `canopy:pm-scout` / the `product-management` skill,
which explores the codebase for *new* work. Issue-triage triages what already
exists. Built to close the loop on the issues **and PRs** agents leave behind
as they run.

### Why open PRs are in scope

Issue triage without PR triage measures the wrong end of the pipeline. An open
PR is work that is *already paid for* — written, tested, reviewed by its
author — and it is the cheapest thing in the repo to convert into shipped
value. It is also the most likely thing to rot: branches go stale, CI
requirements change under them, and a repo where nobody reviews (canopy's
maintainer explicitly does not) has no other mechanism that notices.

The measured case this exists for: a canopy triage found two open PRs, both
carrying verified work with passing tests, both stranded — one on a missing
VERSION bump, one on a `docs-sync` gate wanting a one-line acknowledgement in
the PR body. Neither needed judgment. Both were invisible to a triage that only
read issues, and would have aged indefinitely because the repo has no reviewer
to notice them. Two minutes of mechanical work shipped both.

So: **a triage that reports on issues and ignores a mergeable PR has left the
most valuable item in the repo on the floor.**

## Critical rules

- **Read-only until the gate.** Phases 0–4 perform **no** GitHub writes. The
  only mutations (`gh issue close`, `gh issue comment`, label edits, opening
  PRs) happen in Phase 6, and only after the user approves that group.
- **Declare the scope contract before spending anything.** Phase 0.5 asks how
  far this run goes and the run **stops at that boundary**. A triage that
  silently becomes a 15-PR build session is a failure mode, not a bonus — see
  § Scope discipline.
- **Every `close` needs code evidence.** A close recommendation must cite the
  `file:line` that already resolves the issue. No evidence → downgrade to
  `investigate`.
- **`implement` means fixable-and-verifiable from here.** If the fix needs a
  surface you can't reach this session, it is `blocked`, not `implement`.
  Mislabelling blocked work as `implement` is what makes a backlog look like
  churn: the item is filed under a verb nobody can execute, so it survives
  triage after triage.
- **An open PR outranks its issue.** If an open PR already implements an open
  issue, that issue is **never** dispositioned `implement` — the work exists.
  Disposition the *PR*, and mark the issue as awaiting it. Re-implementing
  something that is sitting in an open branch is the most expensive mistake
  this skill can make.
- **A mechanical CI gate is not a review outcome.** `check-version`,
  `docs-sync`, a lint nit, a stale base — these are unblockable by a
  deterministic edit and must be dispositioned `unblock`, not `needs-work`.
  Reserve `needs-work` for a failure that requires judgment about the change
  itself.
- **No silent truncation.** If the repo has more open issues or PRs than the
  cap, say so explicitly in the report ("triaged 30 of 47 open issues; 12 of 12
  open PRs").
- **Report outcomes, not machinery.** The chat report says what changed, what's
  left, and whose it is. Phase names, the carry-forward split, sidecar mechanics
  and other self-accounting go in the run log. See § Reporting discipline — a
  correct triage with an unreadable report is a failed run.
- **Never auto-merge an unvalidated change.** See Phase 5 for the confidence
  test. The merge decision on anything unverified stays with the human.
- **One repo per run.** No org-wide or cross-repo sweeps in a single invocation.

## Scope discipline

The failure mode this skill has actually produced in the wild: a triage of 29
issues turned into 15 merged PRs plus a multi-hour live-device debugging
detour, ran 34 hours of wall-clock, and ended with the operator asking "what's
left in this session?" The triage itself was good; the unbounded action phase
is what made it unholdable.

So: **the scope contract is chosen up front (Phase 0.5), announced, and
enforced.** When the run hits the boundary, it stops and reports the remainder
as a recommended next invocation — it does not keep going because the next item
looked cheap. If a genuinely blocking discovery surfaces mid-run (the repo is
on fire; the triage can't proceed at all), surface it and *ask* rather than
absorbing it into this run.

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

## Phase 0.5 — Declare the scope contract (one `AskUserQuestion`, before any spend)

Ask once, up front. This is the cheapest question in the skill and it prevents
the failure mode in § Scope discipline.

**Question:** "How far should this run go?"

| Option | What it means |
|--------|---------------|
| **Triage only** | Report + run log + posted dispositions. Opens no new PRs. Cheapest; best when a recent triage already exists. |
| **Triage + quick wins** (recommended default) | Triage, then ship up to **N = 5** `implement` items whose effort is **S** and whose validation is fully local. Everything else is handed back. |
| **Triage + one cluster** | Triage, then take **one** named cluster (see Phase 4) all the way, including the live validation it needs. Best when a blocking cluster is the real target. |
| **Full sweep** | No cap. Explicitly acknowledge in the report that this run may be long and hard to hold. |

**PR dispositions are outside the cap, in every contract including `Triage
only`.** The cap exists to stop a triage from becoming an unbounded *build*
session; unblocking and merging a PR that already exists is not building. It is
bounded by the number of open PRs — which the repo, not the run, decides — and
it is the cheapest value in the skill. Merging is still gated in Phase 6 like
everything else, and an `unblock` that turns out to need real code changes
stops and re-dispositions to `needs-work` rather than absorbing the work.

Announce the chosen contract in one line before Phase 1, and repeat it in the
run log header. If the user picked a capped option, the cap is a hard stop:
when it's reached, go straight to the close-out report.

## Phase 1 — Gather open issues **and open PRs**

```bash
gh issue list --repo "$SLUG" --state open --limit 30 \
  --json number,title,body,labels,createdAt,updatedAt,comments

gh pr list --repo "$SLUG" --state open --limit 30 \
  --json number,title,body,isDraft,mergeable,mergeStateStatus,reviewDecision,\
headRefName,baseRefName,author,createdAt,updatedAt,labels
```

- Default cap is **30** each. If the command arg carried a `--limit N`, use it.
- Get the total open count to detect truncation:
  ```bash
  gh api "repos/$SLUG" -q .open_issues_count   # includes open PRs; treat as an upper bound
  ```
  If the fetched count is less than the number of open issues, note the
  truncation in the report.
- **Zero of both** → report that and stop. **Zero issues but open PRs exist** →
  do NOT stop; PR triage is the run. The reverse is also fine.

## Phase 2 — Resolve the code, and skip issues whose evidence hasn't moved

**2a. Resolve the code to evaluate against.**

- **`CODE: local`** → search the current working tree directly (Grep/Glob/Read).
- **`CODE: remote`** → shallow-clone into a temp dir and search there:
  ```bash
  TMP=$(mktemp -d)
  git clone --depth=1 "https://github.com/$SLUG" "$TMP/repo" >/dev/null 2>&1
  echo "$TMP/repo"
  ```
  Remember to `rm -rf "$TMP"` at the end of the run.

**2b. Incremental triage — do not re-derive a verdict nothing invalidated.**

A full fan-out over a backlog that was triaged two days ago spends one subagent
per issue to reproduce the same table. Before dispatching, find the most recent
prior run and reuse its verdicts where the evidence is provably unchanged.

```bash
# Most recent prior run's machine-readable sidecar (see Phase 4b).
#   local  → <repo-root>/.canopy/issue-triage/runs/*.yaml
#   remote → $HOME/.canopy/issue-triage/<owner>-<repo>/*.yaml
LAST=$(ls -1 .canopy/issue-triage/runs/*.yaml 2>/dev/null | tail -1)
echo "LAST: ${LAST:-none}"
[ -n "$LAST" ] && python3 -c "
import sys,yaml
d=yaml.safe_load(open('$LAST'))
print('run_date:', d.get('run_date'))
for v in d.get('verdicts',[]):
    print(v['number'], v['disposition'], ','.join(v.get('evidence_paths',[])))
"
```

Then, for each issue that has a prior verdict, decide **stale or fresh**:

```bash
# Paths touched since the prior run. Compare against each issue's evidence_paths.
git log --since="<prior run_date>" --name-only --pretty=format: | sort -u
```

- **fresh** — none of the issue's `evidence_paths` appear in the touched set,
  the issue has no new comments since `run_date`, and the prior disposition was
  not `investigate`. Carry the prior verdict forward verbatim, marked
  `source: carried-forward`. **Do not dispatch a subagent.**
- **stale** — anything moved, a new comment landed, or the prior disposition was
  `investigate` (those are by definition unresolved). Re-triage it.

Report the split explicitly: "16 open · 3 re-triaged (evidence moved) · 13
carried forward from 2026-07-24." A run that carries most of the backlog
forward is the *success* case, not a shortcut — it means the prior triage is
still good and the money goes to what changed.

If no prior sidecar exists, everything is stale; triage all of it.

**Carry-forward applies to issues only. Open PRs are always re-evaluated.** A
PR's disposition turns on CI state, mergeability, and base drift — all of which
move without a single tracked file changing. A carried-forward PR verdict is
therefore stale by construction. PR triage is cheap enough (Phase 3b) that this
costs nothing.

## Phase 3 — Evaluate each stale issue (fan-out, read-only)

Dispatch **one subagent per stale issue** (use the Agent tool; for many issues,
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
> - **implement** — the request is still valid, not yet satisfied by the code,
>   AND the fix can be written and verified without a surface outside this
>   session. Estimate effort S (<1hr) / M (2–4hr) / L (day+).
> - **blocked** — you know what the fix is, but proving it needs a surface you
>   cannot reach by reading code: a live device, a live upstream form or API
>   response, a fresh end-to-end run, a permission grant, someone else's repo.
>   Name the surface and the single concrete observation that would unblock it.
> - **investigate** — you cannot adjudicate from the code alone (needs a repro,
>   under-specified, or the issue's own claims contradict the code's comments).
>   Say what's missing.
>
> Return strictly: `number`, `disposition`, `confidence` (high/medium/low),
> `effort` (S/M/L or n/a), `blocking` (`blocks-e2e` | `harness` | `polish` —
> your judgment of what breaks if this is never fixed), `evidence` (list of
> `file:line` + one-line note), `evidence_paths` (just the bare file paths from
> your evidence, deduped — this is what the next run diffs against),
> `unblocked_by` (for `blocked` only: the one observation needed), `reasoning`
> (1–3 sentences). Do not modify anything. Do not call any `gh` write command.

Collect all verdicts, then merge in the carried-forward ones from Phase 2b.

## Phase 3b — Evaluate each open PR (read-only)

Cheap and mostly deterministic — **do this inline, not as a fan-out.** A PR's
disposition is decided by its CI state and its diff, both of which you can read
directly. Spending a subagent per PR buys nothing.

For each open PR, gather:

```bash
gh pr checks <n> --repo "$SLUG"                      # per-check pass/fail
gh pr view <n> --repo "$SLUG" --json files,body,mergeStateStatus,isDraft
gh run view <failed-run-id> --repo "$SLUG" --log-failed | tail -30   # WHY it failed
```

Reading the failing log is not optional. `mergeStateStatus: BLOCKED` says
nothing about whether the blocker is a missing version bump or a broken test,
and that distinction *is* the disposition.

Then assign one:

| Disposition | Test |
|---|---|
| **merge** | Every required check green (or red only on a check the repo's conventions treat as advisory and you can name), not a draft, diff still wanted. |
| **unblock** | Checks red **only** on a mechanical gate — and you can state the exact deterministic fix in one line. See the catalog below. |
| **needs-work** | A test genuinely fails, the diff is wrong, or deciding requires judgment about the change. Say what specifically. |
| **close** | Superseded by a merged PR, obsolete, or its motivating issue is closed as not-planned. Cite the superseding commit/PR. |
| **blocked** | The diff is right but merging it needs a live surface first (an unvalidated selector/recipe/migration). Same rules as the issue-side `blocked` — name the one observation. |

**The mechanical-gate catalog** (canopy and the agent fleet; extend per repo):

| Symptom in the log | The one-line fix |
|---|---|
| `N plugin file(s) changed but VERSION did not advance` | run the repo's documented version-bump command on the branch (canopy family: `canopy version bump`, per that repo's CLAUDE.md), commit, push — never hand-edit the version files |
| `docs-sync gate failed: <src> missing <SKILL.md>` | Either teach the SKILL.md the new surface, **or** — only when the change is genuinely engine-internal — add `Docs-not-needed: <reason>` to the PR body |
| Branch is behind base / merge conflict in a lockfile-ish file | rebase or merge base in, push |
| A lint/format check only | run the formatter, commit |

**Two rules keep `unblock` honest:**

1. **Verify the escape hatch is true before using it.** `Docs-not-needed` is a
   claim that the diff adds no author-facing surface — read the diff and
   confirm it. Adding the line to silence a gate that is correctly firing
   converts a real docs gap into a merged one, which is worse than the red
   check.
2. **If the "mechanical" fix turns out to need real changes, stop.**
   Re-disposition to `needs-work` and report it. `unblock` is a promise that
   the work is done; it must not become a side door for finishing someone
   else's PR inside a triage.

Also record, per PR, the **issues it references** (`Closes #n` / `Refs #n` in
the body, or an obvious subject match). Phase 3.5 reconciles these.

## Phase 3.5 — Cluster and dedupe (ONE pass over all verdicts, before any action)

**This phase is mandatory and it is not optional per-issue work.** The fan-out
in Phase 3 is deliberately blind — each subagent sees exactly one issue — so
duplicates and shared root causes are structurally invisible to it. A single
pass over the whole verdict set is what catches them, and it costs one agent
instead of the per-issue rediscovery it replaces.

Real example this exists for: two issues filed three days apart described the
same defect — same file, same function, same repro, same fixture IDs. Nothing
in a per-issue fan-out could see that; a full triage subagent was spent
concluding "duplicate."

Do one pass (inline, or one subagent given every verdict) and produce:

1. **Duplicate sets.** Issues whose evidence converges on the same
   `file:line` + same root cause. Pick the **canonical** one (oldest, or the
   superset scope) and mark the rest `close` with `duplicate_of: <n>`. Port any
   unique detail from the duplicates into a comment on the canonical issue —
   never lose the extra repro.
2. **Root-cause clusters.** Distinct issues that one change would resolve
   together (e.g. three recipes all needing the same new "which screen am I on"
   signal). Name the cluster, list its members, and state the **one**
   implementation that serves all of them. Clusters are the unit the
   `Triage + one cluster` scope contract operates on.
3. **PR ↔ issue reconciliation.** For every open PR, resolve which open issues
   it addresses. Then:
   - An issue with an open PR against it is **never** `implement`. Rewrite its
     disposition to point at the PR ("awaiting #<pr>") and let the PR's
     disposition carry the work.
   - A `merge`/`unblock` PR whose body says `Closes #n` means issue `n` closes
     itself on merge — do **not** also queue it in the close group, or Phase 6
     posts a redundant close comment on an issue GitHub already closed.
   - A PR referencing an issue you dispositioned `close` (already fixed) is a
     contradiction: one of the two verdicts is wrong. Re-check before acting.
4. **A blocking-order ranking.** Sort clusters and singletons by `blocking`
   (`blocks-e2e` > `harness` > `polish`), not by effort. Effort decides *how* to
   sequence within a tier; it must not decide which tier gets attention. A loop
   that always takes the cheapest item grazes the easy tier forever while the
   `blocks-e2e` tier ages — that is the churn signature.

## Phase 4 — Report

Print a table to chat, ordered by the Phase 3.5 blocking rank (**not** by
disposition or effort), with close-candidates folded in as a cheap-wins
appendix:

```
#   Disp          Blocking    Conf  Effort  Cluster            Title                       Evidence / why
--  -----------   ----------  ----  ------  -----------------  --------------------------  --------------------------
893 blocked       blocks-e2e  high  M       home-differentiator "viewJobCard false ..."     deliver-launch.yaml:84 keys on it
863 blocked       blocks-e2e  high  M       home-differentiator "already-Learn-complete..."  connect-claim-opp.yaml:464
19  implement     harness     high  S       —                  "Typo in error message"      errors.py:44 still wrong
12  close         polish      high  —       —                  "Crash on empty config"      config.py:88 already guards
```

Then, if any PRs are open, a **second table — print it first, above the issue
table.** Open PRs are the nearest-to-shipped work in the repo and burying them
under a backlog inverts the priority:

```
PR   Disp        Checks              Blocker                          Title
---  ----------  ------------------  -------------------------------  -----------------------------
417  unblock     pytest✓ docs-sync✗  needs `Docs-not-needed:` line    "ddd(preflight): walk the ..."
420  unblock     pytest✓ version✗    needs `canopy version bump`      "ddd: make a run dir writt..."
408  needs-work  pytest✗             test_foo fails on the diff       "..."
```

Above both tables print only:
- one line per cluster: name, members, the single fix that serves them
- the **tier counts** — how many `blocks-e2e` vs `harness` vs `polish`. This
  one line is the closest thing the run produces to "is the backlog healthy?"
- **the mergeable count** — "2 open PRs, both one mechanical fix from
  mergeable". If every open PR is `merge`/`unblock`, that is the headline of
  the run, not a footnote.
- the truncation line, **if** the cap actually truncated something

Everything else about how the run was performed — the scope contract, the
re-triaged/carried-forward split, which prior run was compared against, why
carry-forward did or didn't apply — goes in the run log (Phase 4b) and **not** in
chat. See § Reporting discipline.

### Phase 4b — Write the run log in BOTH forms

**Prose, for humans** — same content as the chat report:
- `CODE: local` → `<repo-root>/.canopy/issue-triage/runs/YYYY-MM-DD.md`
- `CODE: remote` → `$HOME/.canopy/issue-triage/<owner>-<repo>/YYYY-MM-DD.md`

**YAML sidecar, for the next run** — same basename, `.yaml`. This is what
Phase 2b reads, and it's what makes trends computable at all; a prose-only log
can't be diffed. Required shape:

```yaml
run_date: 2026-07-26
repo: dimagi-internal/ace
scope_contract: triage-plus-quick-wins
code_ref: d5c1f198               # the commit triaged against
open_total: 16
triaged: 16
carried_forward: 13
truncated: false
clusters:
  - name: home-differentiator
    members: [893, 863, 796]
    one_fix: "suite action-bar title selector replaces viewJobCard as the Learn/Deliver signal"
tier_counts: { blocks-e2e: 10, harness: 5, polish: 1 }
prs:                              # open PRs at run time; never carried forward
  - number: 417
    disposition: unblock          # merge | unblock | needs-work | close | blocked
    checks: { pytest: pass, check-version: pass, docs-sync: fail }
    blocker: "docs-sync wants a `Docs-not-needed:` line; orchestrator diff is a pure helper move"
    mechanical: true              # false => needed judgment, i.e. needs-work
    refs_issues: []
    action: null                  # filled in by Phase 6
    outcome: null                 # filled in by Phase 6
verdicts:
  - number: 893
    disposition: blocked          # implement | blocked | investigate | close
    blocking: blocks-e2e          # blocks-e2e | harness | polish
    confidence: high
    effort: M
    cluster: home-differentiator
    evidence_paths:               # bare paths — Phase 2b diffs these
      - mcp/mobile/recipes/static/deliver-launch.yaml
      - mcp/mobile/selectors/connect-2.63.0.yaml
    unblocked_by: "one live ui-dump from a Deliver-complete opp"
    duplicate_of: null
    source: fresh                 # fresh | carried-forward
    action: null                  # filled in by Phase 6
    outcome: null                 # filled in by Phase 6
```

Use the current date from the environment context for `YYYY-MM-DD` (do not call
a date command in a way that breaks determinism — the conversation provides
today's date). Commit both files alongside any other working-tree changes.

## Phase 5 — Recommend an action per issue, within the scope contract

State a concrete **recommended next action** for every issue, so the user can
approve in one shot. Keep the recommendations *inside* the Phase 0.5 contract —
if the contract is `Triage only`, every recommendation is a next-invocation
suggestion, not something this run offers to do.

- **implement, inside the cap → ship it.** Confident means: the project's
  validation passed AND the change required no unverifiable guess. Ship those
  and arm auto-merge if the repo's convention allows.
- **implement, over the cap → hand back.** Name it as next-run work; do not
  quietly absorb it.
- **blocked → produce the validation ticket, not a PR.** Say exactly what to
  run, on which surface, and what evidence adjudicates it. Where a draft PR
  already holds the candidate fix, keep it a draft and say so. Group all
  `blocked` items by the surface they need, so one device/session/permission
  visit clears several at once — that grouping is the whole point of the
  disposition.
- **investigate → the concrete next step.** Name the command / repro / dump;
  don't restate "needs more info."
- **close → already handled in Phase 6.**
- **PR `unblock` → state the exact fix, then do it.** Not "fix CI" — the
  literal command or the literal line to add. Outside the cap (Phase 0.5).
- **PR `merge` → merge it,** subject to the unvalidated-change exception below.
- **PR `needs-work` → comment what fails and hand back.** Do not fix someone
  else's PR inside a triage.

Then present **one consolidated gate**: "Do all of the above?" (Approve all /
Let me pick / Skip). *Do not* tell the user what they'll probably say — offer
the choice neutrally. On *Approve all*, execute everything that the scope
contract permits and hand back the rest explicitly.

**The honest exception, stated every time it applies:** a change you flagged as
**unvalidated** — a recipe/selector/gesture not yet confirmed on the live
surface, per a target repo's "validate live before shipping" rule — is never a
rubber-stamp merge. Recommend hold + the specific validation step.

## Phase 6 — Act (gated, grouped by disposition)

Confirm **each non-empty group separately** via its own `AskUserQuestion`, so
outward-facing actions are gated and individually overridable. Each question
offers: **Approve all / Skip this group / Let me pick** (Other → name the
specific issue numbers to act on).

**close group**

Pick the close reason deliberately — a repo whose every close is `COMPLETED`
has a tracker that can't tell you how many real defects it eliminated:

```bash
# Actually fixed by code (this run or a prior one):
gh issue close <n> --repo "$SLUG" --reason completed \
  --comment "Triaged against current code: <one-line reason>. Evidence: <file:line>. Closing as fixed — reopen if this is wrong."

# Duplicate, obsolete, superseded, or no-longer-relevant:
gh issue close <n> --repo "$SLUG" --reason "not planned" \
  --comment "Triaged against current code: <one-line reason>. Duplicate of #<m> / obsolete because <evidence>. Reopen if this is wrong."
```

Optionally label first: `gh issue edit <n> --repo "$SLUG" --add-label "triage:obsolete"` (skip if the label doesn't exist rather than failing the run).

**blocked group**

No PR. Comment the validation ticket, label it, and leave it open:

```bash
gh issue comment <n> --repo "$SLUG" \
  --body "Triage: fix is known, blocked on a surface this session can't reach.
**Needs:** <the one observation>.
**Surface:** <live device / upstream form / fresh run / permission>.
**Adjudicates it:** <what evidence closes this>.
Grouped with #<others needing the same surface> — one visit clears all of them."
gh issue edit <n> --repo "$SLUG" --add-label "blocked-on-validation"   # skip if label absent
```

Then print the **validation queue** — one block per surface, listing the issues
it would clear. That queue is the deliverable of this group; it turns "book two
hours with a device" into an obviously-scheduled action instead of a thing that
never happens.

**investigate group**
```bash
gh issue comment <n> --repo "$SLUG" \
  --body "Triage couldn't adjudicate this from the code alone. To proceed we need: <what's missing>."
gh issue edit <n> --repo "$SLUG" --add-label "needs-info"   # skip if label absent
```
Leave the issue open.

**PR group** (gate this one FIRST — it is the cheapest shipped value)

For each `unblock`, apply the named mechanical fix on that PR's branch, push,
wait for checks, then merge. For each `merge`, merge directly.

```bash
# Work on the PR's branch, not main.
git fetch origin <headRefName> && git switch <headRefName>   # or a fresh worktree

# --- the mechanical fix, e.g. ---
#   version bump: use the TARGET repo's own documented command (see its
#   CLAUDE.md); never hand-edit VERSION / plugin.json / marketplace.json.
#   docs-sync acknowledgement:
gh pr edit <n> --repo "$SLUG" --body "$(gh pr view <n> --repo "$SLUG" --json body -q .body)

Docs-not-needed: <the honest one-sentence reason>"

git push
gh pr checks <n> --repo "$SLUG" --watch    # required checks must go green FIRST
gh pr merge <n> --repo "$SLUG" --merge
```

Rules that are load-bearing here:

- **Never `--admin` past a red required check.** A red `check-version` means
  the bump is wrong; fix the bump. Forcing it re-creates the exact silent
  failure the gate exists to prevent.
- **Merge one at a time, re-checking between.** Two PRs touching the same file
  can both be green and still conflict; the second one's checks must run
  against the first one's merge.
- **A `Docs-not-needed:` line must be true.** You read the diff in Phase 3b —
  quote what makes it engine-internal. If you can't, update the doc instead.
- **After merging anything under `plugins/canopy/` in a canopy-family repo,
  run the repo's plugin-update step** (`/canopy:update`) — a merged bump that
  nobody distributes is a bump that didn't ship.
- **`needs-work` PRs get a comment, not a fix:**
  ```bash
  gh pr comment <n> --repo "$SLUG" --body "Triage: not mergeable as-is — <what fails, specifically>."
  ```

**implement group** (only up to the Phase 0.5 cap)
For each approved issue, follow the `product-management` skill's Phase 4/5
implement+ship conventions:
- branch `<prefix>/<issue-slug>` off the default branch (never commit to main)
- implement the change, run the project's full validation (lint + build + tests)
- open a PR whose body references the issue (`Refs #<n>` — or `Closes #<n>`
  only if you're confident it fully resolves it)
- do one issue at a time; if validation can't pass after 2 attempts, stop and
  report rather than thrashing
- honor the Phase 5 recommendation: confident + approved → mark ready and arm
  auto-merge per the target repo's convention. Flagged **unvalidated** → stays
  a draft with the validation step named.
- **when the cap is reached, stop.** Report the remainder as next-run work.

Read `skills/product-management/SKILL.md` from the same install path if you need
the full implement/ship detail — do not reimplement branch/PR logic from memory.

After acting, update **both** run-log forms: the prose table's "action taken"
column and the sidecar's `action` / `outcome` fields per verdict. The sidecar's
`outcome` is what a later run reads to know whether a shipped fix actually
landed.

### Phase 6b — Close out in plain language

The final message is the only part of this run most people read. It is a status
update, not a trace. Write it as four short parts, in this order:

1. **State change, one line.** "N open issues → M, P open PRs → Q." Nothing
   else on that line.
2. **One line per PR, then one line per issue**, by number: what it asked for
   and what you did. PRs first — merged work is the result people care about.
   Link the merged PRs and the closed-issue comments.
3. **What's left, and whose it is.** Anything you deliberately didn't do, one
   line each, with who has to act (a setting only they can flip, a design call,
   a follow-up PR). If nothing is left, say "nothing left."
4. **Judgment calls you made that they might overrule** — only the ones that
   would change their decision. Cap at three.

Then stop. Do not append a process summary, a phase recap, or a note about how
the run compared to a previous one.

## Reporting discipline

The measured failure: a 2-issue triage produced a correct result and an
unreadable report. The operator's response was *"I don't understand what you're
saying or why you keep referring to the prior triage so much, should this be this
hard?"* Every phase had reported itself — the scope contract, the
re-triaged/carried-forward split, which prior log lacked a sidecar — and that
bookkeeping buried the three facts that mattered (one issue shipped, one closed,
two follow-ups outstanding).

So the rule: **the run log carries the machinery, chat carries the outcome.**

| Belongs in the run log only | Belongs in chat |
|---|---|
| Phase names and numbers | What changed, by issue number |
| The carry-forward split, and which prior run it diffed | What's left and whose it is |
| That a prior log lacked a sidecar | Judgment calls the human might overrule |
| `evidence_paths`, `source: fresh`, sidecar shape | Blocking-tier counts; cluster lines |
| Cost/coverage self-accounting | Truncation — but only if it truncated |

Three specifics, because each was a real defect:

- **Never mention the prior run unless it changed this run's output.** If
  carry-forward saved subagents, one clause is enough ("13 carried forward from
  2026-07-24"). If it saved nothing, say nothing — its absence is not news.
- **Never explain the skill's own plumbing.** Sidecars, phase gates, and
  cost-saving steps are how the work got done, not results. A reader who has to
  learn the skill's internals to read its output has been handed the wrong
  artifact.
- **If the ceremony is disproportionate to the task, say so once — in one
  sentence — and stop.** A 2-issue backlog does not need the clustering
  narrative. Naming the mismatch is useful signal; re-litigating it is not.

## The `blocked` disposition

`blocked` exists because of a measured failure: in one repo, 10 of 16 open
issues needed a live surface, all were dispositioned `implement`, and three of
them survived two consecutive full triages a month apart — each time re-earning
an `implement` verdict nobody could act on. The work wasn't stalled because it
was hard; it was stalled because it was filed under the wrong verb.

Use it when the fix is **known** but unverifiable here. Do not use it as a
softer `investigate`: if you don't know the fix, that's `investigate`.

Typical surfaces:
- a live device / emulator (mobile recipes, selector maps, gesture calibration)
- a live upstream form or API whose real field names you'd otherwise guess
- a fresh end-to-end run (one-way state the current run already consumed)
- a permission or role grant only a human can make
- another repo you don't own

Two rules make the disposition worth having:
1. **Name the single observation that unblocks it.** "Needs live validation" is
   useless; "needs one `ui_dump` from a Deliver-complete opp to confirm the
   action-bar title differentiates Learn from Deliver home" is a bookable task.
2. **Group by surface, not by issue.** The output is a validation queue, so one
   visit to a surface clears every issue waiting on it.

## Cost discipline

- Phase 0.5: one question. Saves the most.
- Phase 1: one `gh issue list` + one `gh pr list`.
- Phase 2b: cheap, and the single biggest saver on a repeat run — carrying 13
  of 16 issues forward turns a 16-subagent run into a 3-subagent run.
- Phase 3: one subagent per **stale** issue — the bulk of the cost. Respect the
  cap; for very large backlogs, triage the cap and say how many remain.
- Phase 3b: inline, ~3 `gh` calls per open PR. **The best value in the skill** —
  it converts already-paid-for work into shipped work for the price of reading
  a CI log. Never fan this out to subagents.
- Phase 3.5: one pass. Cheaper than the per-issue rediscovery it prevents.
- Phases 4/5/6: cheap, bounded by the scope contract (PR actions excepted).
