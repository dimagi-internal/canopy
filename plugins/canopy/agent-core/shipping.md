# Shipping — fleet-canonical ship loop, and how to WAIT

> **Fleet-canonical process (canopy agent-core).** Your `skills/shipping/SKILL.md` stub binds this
> to your repos and carries your local notes. Fleet-process changes → PR canopy; agent quirks →
> your stub.

Branch → commit → PR → wait → merge → **verify it landed** → say so. The expensive part is the
**wait**, and it was being re-invented per agent and per turn — hal, eva and ace each wrote their
own copy of these rules, and ace additionally shipped a *prescribed* poll loop that could not run.
The mechanics live here now.

Applies to every repo an agent ships into: its own, canopy, canopy-web, and any product repo.

## Step 0 — is there anything to wait FOR?

**This is the step that pays**, because most of the time the answer removes the wait entirely. One
call, and the answer is **not guessable**:

```bash
gh pr checks <n> 2>&1 | head -3     # "no checks reported" => nothing to wait for, merge now
```

⚠️ **"no checks reported" has TWO meanings and this command cannot tell them apart.** It is also
what you get when a provider outage DROPPED the `pull_request` event on a repo that very much does
have gates — and merging on the Step-0 reading then ships genuinely unverified code. Disambiguate before acting on it:

```bash
# Best, WHEN IT WORKS: the repo's own list of required checks.
gh api repos/<owner>/<repo>/branches/main/protection --jq .required_status_checks.contexts
```

**Required contexts non-empty + `gh pr checks` reporting none = the event was dropped**, not "no
CI here."

⚠️ **That call 403s on a private repo without branch protection** — which is every agent repo
(`hal`, `eva`, `echo`, `ada`): `Upgrade to GitHub Pro or make this repository public…`. A 403 is
NOT "no required checks"; it means you cannot ask. Fall back to the workflow files plus the run
list, which work everywhere:

```bash
grep -l 'pull_request' .github/workflows/*.yml   # does this repo gate PRs at all?
gh run list --branch <branch> --limit 5          # did anything actually fire?
```

**Workflows trigger on `pull_request` + zero runs for the branch = dropped.**
**No workflow triggers on `pull_request` = genuinely no gates; merge.**
See § Dropped events below.

Measured 2026-08-13, re-verified 2026-08-17 — "agent repos have no CI" is **false**, which is
exactly why you check instead of assuming:

| Repo | PR checks? | Merge shape |
|---|---|---|
| **hal, echo, eva, ada** | **no** — single workflow on `push: main` | `gh pr merge <n> --squash`, immediately |
| **ace** | **yes** — required: `clean-install` only (`version-check` is advisory) | arm `--auto --merge`, wait, ~70s |
| **canopy** | **yes** — required: `check-version`, `enforce_admins` on, **no `--auto`** | wait (~10s), then `gh pr merge <n> --merge` |
| **canopy-web** | **yes** — required: `Backend tests`, `Frontend build` | **merge queue** — see below |
| **connect-labs, ace-web** | **yes** | wait, then merge |

Re-derive the row (`ls .github/workflows/`, `grep -l pull_request`,
`gh api repos/<owner>/<repo>/branches/main/protection --jq .required_status_checks.contexts`)
rather than trusting the table if a repo has been touched since. `gh pr checks` is the fact; the
table is a starting point.

**Know the merge latency too, not just the shape.** An ace PR merges in ~70 seconds (measured
2026-08-17: 72s, 67s, 81s, 76s). A poll interval longer than the merge is how a wait costs more
than the thing it waits on.

## Step 1 — the wait: never a foreground polling loop

**A foreground `sleep` used to wait is blocked by the harness Bash contract.** This is not a
gating rail and no config can exempt it — no `sleep` rule exists in `gating-baseline.json` or in
any agent's `config/gating.json`. It is the tool contract. Reproducer, 2026-08-17:

```
$ sleep 30; echo "survived"
Blocked: sleep 30 followed by: echo "survived". To wait for a condition, use Monitor
with an until-loop (e.g. `until <check>; do sleep 2; done`). To wait for a command you
started, use run_in_background: true. Do not chain shorter sleeps to work around this
block.
```

Short sleeps pass (`sleep 3; gh pr view …` runs fine), which is why this keeps being rediscovered
the expensive way: the pattern looks like it works until the interval is long enough to matter.
**Do not chain shorter sleeps to get under the threshold** — the block message names that
workaround specifically, and the block is not the real cost. What follows it is: the blocked
command dies, the fallback runs in the foreground anyway, and the turn eats the full 10-minute
Bash timeout as `Exit code 143 Command timed out`.

**The correct wait is one backgrounded command that EXITS on the condition.** `gh` already blocks
correctly; the job is to run it where it doesn't block *you*:

```bash
gh pr checks <n> --watch --fail-fast; gh pr checks <n> 2>&1 | tail -5
```

run via **`Bash` with `run_in_background: true`** — one background task, one notification when it
settles, and the turn keeps working meanwhile. Don't schedule a wakeup to poll it either; the
harness re-invokes you when it exits.

A bounded loop is fine *inside* a backgrounded command, and is what you want when the terminal
state is a merge rather than a check result:

```bash
for i in $(seq 1 40); do
  s=$(gh pr view <n> --json state --jq .state 2>/dev/null)
  case "$s" in MERGED|CLOSED) break;; esac
  sleep 15
done
gh pr view <n> --json number,state,mergedAt,mergeStateStatus \
  --jq '"PR #\(.number) state=\(.state) mergedAt=\(.mergedAt // "null") mergeState=\(.mergeStateStatus)"'
```

The `sleep` inside is fine — the block is on *foreground* sleeps. **Always bound it** so a stuck PR
surfaces as a *result* instead of a hang.

**`run_in_background`, not `Monitor`.** The two read as if they disagree; they don't. `Monitor` is
for one notification *per occurrence*; a merge-wait wants exactly one at the end, and Monitor's own
guidance sends that shape back here. Reach for `Monitor` only if you want running commentary.

## Step 1b — a `gh` command that ERRORS may still have DONE the thing

Most `gh` subcommands that write or read structured data go through **GraphQL**; `gh api repos/…`
goes through **REST**. GitHub degrades them independently, so you can see
`HTTP 503: No server is currently available…` from one while the other is fine.

**A failed `gh pr create` is AMBIGUOUS about whether the PR was created** — the mutation may have
committed before the response died. Retrying blind is how one branch gets two PRs. Confirm through
the *other* transport first, then retry there:

```bash
gh api "repos/<owner>/<repo>/pulls?head=<owner>:<branch>&state=all" --jq '.[] | "#\(.number) \(.state)"'
gh api --method POST repos/<owner>/<repo>/pulls --input -    # {title, head, base, body}
```

**Check the provider before blaming your branch — the FEED, never a hunch.** One call, checkable:

```bash
curl -s https://www.githubstatus.com/api/v2/summary.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status']['description']); [print(' ',c['name'],c['status']) for c in d['components'] if c['status']!='operational']; [print(' incident:',i['name'],i.get('shortlink')) for i in d['incidents']]"
```

A named incident plus a working control is evidence; "several calls failed" is not. **A CI gate can
go red for the outage rather than for your diff — read the job log before you touch the code**, and
never force-merge past a CI system that is genuinely down. Say so and leave the PR open.

## Dropped events — an Actions outage silently un-CIs your PR

Measured 2026-08-26 on `ace#1671` during a GitHub Actions **major outage**: the branch pushed and
the PR opened normally, and **zero workflow runs were ever created**. Not queued, not failed, not
`startup_failure` — absent. `gh pr checks` reported "no checks reported", which is exactly the
Step 0 string meaning "merge now."

Four facts, each of which cost a turn to establish:

1. **The dropped event is never replayed.** GitHub builds check runs from the webhook; one lost to
   an outage is gone. The PR sits at `mergeStateStatus: BLOCKED` with no runs, silently,
   indefinitely — nothing times out and nothing complains. **Something has to push again** to
   re-fire it:
   ```bash
   git commit --allow-empty -m "chore: re-fire CI after <date> Actions outage dropped the PR event"
   git push
   ```

2. **`--admin` does not rescue it.** Admin merge bypasses a check that FAILED; it refuses one that
   never reported — `Required status check "<name>" is expected. (mergePullRequest)`. There is no
   force path, which is the correct outcome. Know it before spending a turn hunting the flag.

3. **Arm auto-merge and let it finish.** It survives the outage, survives later pushes, and merges
   the instant the gates go green — no second turn:
   ```bash
   gh pr merge <n> --squash --auto
   gh pr view <n> --json autoMergeRequest --jq '.autoMergeRequest != null'   # confirm ARMED
   ```

4. **A partial recovery re-drops it.** Actions came back long enough to run one set of checks, then
   went down again — so a subsequent force-push was dropped a second time. Re-check the provider
   feed after every push during an incident; do not assume one successful run means recovery.

**Reproducing the gates locally while you wait is worth something — but only if `main` has not
moved.** Run each gate's own `run:` commands, then:

```bash
git rev-list --count HEAD..origin/main    # 0 => your branch head IS the merge result CI builds
```

Non-zero and your green says nothing about the merge commit CI would evaluate; the honest report
is "unverified", not "passes locally".

## Version collision — the failure that ONLY happens because you waited

A long wait is itself a hazard on any repo whose CI asserts the VERSION advances past `main`
(`ace`, `canopy`). While your PR sits unmerged, a sibling PR merges *your* version number, and the
gate that then fails is not about your change at all:

```
##[error]VERSION 0.13.999 is ALREADY on origin/main. Merging would put two different
trees behind one version, and the plugin cache is keyed by version.
```

This is the same-day sequel to the outage above: ace#1671 waited out the incident, another PR
merged 0.13.999 meanwhile, and the re-fired run failed on the collision rather than on anything in
the diff. Recover **without losing the race** — disable auto-merge FIRST, or it races your rebase:

```bash
gh pr merge <n> --disable-auto
bash scripts/version-bump.sh --rebase-first     # ace/canopy ship this; rebases then bumps
git push --force-with-lease
gh pr merge <n> --squash --auto                 # re-arm only after the new VERSION is pushed
```

Then **re-run the suite before trusting it**: the rebase pulled in main's new commits, so the
pre-rebase green is void by the rule above (ace#1671: 5056 tests before the rebase, 5100 after —
main's two new commits brought their own).

## Step 1c — never build a markdown PR/issue body inside an inline `python3 -c`

A PR body is markdown: backticks, parentheses, `[text](url)` links, quotes, `$`. An inline
`python3 -c "…body='''…'''…"` — or any heredoc whose delimiter is **unquoted** — puts all of that
somewhere bash still expands, where backticks and `$(…)` are command substitution and the quoting
collapses on the first link. That is not a "be careful with escaping" problem; it is unfixable in
the general case, and every minute spent re-quoting it is wasted.

**Write the body to a file first, with a SINGLE-QUOTED heredoc, then read it back:**

```bash
cat > /tmp/.../body.md <<'EOF'
## What changed
- see [the GitHub incident](https://www.githubstatus.com/incidents/abc)
- `gh pr create` went through GraphQL and 503'd
EOF

gh pr create --title "…" --body-file /tmp/.../body.md
# or, over REST when GraphQL is browning out (Step 1b):
python3 -c "import json,subprocess; body=open('/tmp/.../body.md').read(); ..."
```

`<<'EOF'` — **quoted** delimiter — is the load-bearing part: it disables ALL expansion inside the
heredoc, so the markdown arrives byte-for-byte. Prefer `--body-file` when `gh` is healthy; when
falling back to `gh api`/python, still read the body from that file rather than inlining it.

**The same trap bites PATCH SCRIPTS, not just PR bodies.** Any `python3 - <<PY` / `cat > f <<EOF`
whose delimiter is unquoted will have its backticks and `$(…)` eaten before python or the file ever
sees them — and when the payload is markdown you are *editing into a doc*, the corruption lands
silently in the file rather than erroring. Quote the delimiter unless you specifically want
expansion, and re-read the file after writing it.

(Real misses, both 2026-08-17: two consecutive `python3 -c` PR-body attempts died on bash quoting
before the session converged on `body=open(...).read()`; separately, a `python3 - <<PY` patch of
`agent-core/turn.md` had its replacement text mangled by command substitution and had to be
reverted with `git checkout --` and redone with a quoted delimiter.)

## Step 2 — merge, reconcile, verify it LANDED

```bash
gh pr merge <n> --squash        # or --merge / --auto per the Step 0 row
git fetch origin main && git log --oneline origin/main..HEAD    # expect EMPTY
```

**From a worktree, drop `--delete-branch`.** emdash runs each turn in a worktree while `main` is
checked out elsewhere, so `git checkout main` and `gh pr merge --delete-branch` fail with "main
already checked out."

**A merged PR does not mean YOUR commits landed.** Anything `origin/main..HEAD` lists is unlanded
and reachable only from the branch — open another PR. Nothing errors when work strands, which is
what makes this check load-bearing.

**`not mergeable` right after the base branch moved is usually TRANSIENT — re-read before you
"fix" anything.** When a sibling's PR lands while your turn is open, the sequence is:

```
$ gh pr merge 202 --squash
GraphQL: Base branch was modified. Review and try the merge again.   # real: main moved
$ git fetch origin main && git merge origin/main && git push
$ gh pr merge 202 --squash
GraphQL: Pull Request is not mergeable (mergePullRequest)            # usually NOT real
```

The second error reads like a conflict and is not one. GitHub recomputes mergeability
asynchronously after a push, and `gh pr merge` refuses while that is still `UNKNOWN`. **Read the
state instead of believing the error:**

```bash
gh pr view <n> --json mergeable,mergeStateStatus -q '{m:.mergeable,s:.mergeStateStatus}'
# MERGEABLE / CLEAN  -> transient; just call gh pr merge again
# CONFLICTING / DIRTY -> real; resolve it
```

Doing this backwards is the expensive part: a turn that trusts the message starts resolving a
conflict that does not exist — re-merging, or force-pushing over a clean branch. One read
distinguishes them. (Origin: 2026-08-20, an eva turn whose branch was clean the whole time; a
sibling turn merged into the same file mid-turn, and the retry reported `not mergeable` once
before reporting `MERGEABLE / CLEAN` moments later.)

**After a squash-merge, merge `origin/main` back before the next commit** — a long-lived turn
branch plus squash merges otherwise conflicts with your own squashed history on every subsequent
PR. When resolving, `grep -c '<<<<<<<' <file>` (expect 0) BEFORE committing.

**Reconcile the running session** when you shipped into a plugin you are running: `/canopy:update`
after a canopy merge (the next `canopy` call otherwise refuses mid-flight), your own agent's
update command after your repo merges. If the change touched MCP server code, a plugin reload is
NOT enough — MCP subprocesses bind their tool list and schemas at spawn, so quit and reopen.

## Step 3 — the ship checkpoint (unconditional)

**A PR-related turn never closes on an implicit "done."** Before returning, state — explicitly,
every time:

1. **Merge state**, one of `MERGED` / `OPEN` / `CLOSED`, **read** from
   `gh pr view <n> --json state`. Never inferred from having armed auto-merge.
2. **`mergedAt`** when MERGED; **why it is still open** when OPEN (checks running / DIRTY /
   check-failed / queued).
3. **The next planned action** — update run, restart needed, issue closed, or "nothing further."

```
Ship: PR #1465 state=MERGED mergedAt=2026-08-17T18:22:03Z
Next: /ace:update run in-session; no MCP change, so no restart needed.
```

"Auto-merge armed", "checks running", "PR queued" and "watchers armed" are **not** terminal states
and must never be the last word. Armed-but-stuck is indistinguishable from merged to whoever reads
the report next, so the caller either re-polls by hand (defeating the point of backgrounding) or
silently builds on unmerged work. This is `agent-turn-review` § B applied to shipping: a done-claim
gets **verified, not asserted**.

## Merge-queue repos — the verify step lies TWICE

Some repos (canopy-web today) protect `main` with a GitHub merge queue. There:

1. **Strategy flags are refused.** `gh pr merge <n> --squash` prints
   `! The merge strategy for main is set by the merge queue` — which reads as a refusal, **but the
   PR is enqueued anyway.**
2. **Re-reading the PR lies a second time.** It shows `state=OPEN mergedAt=null`, which reads as
   independent confirmation that nothing happened. It isn't: the queue merges minutes later, after
   re-running the required checks against the *queued merge result*.

```bash
gh pr merge <n>                                # no strategy flag — the queue owns it
gh pr view <n> --json state,mergeStateStatus   # "already queued to merge" = it IS queued
```

Then wait for `MERGED` per Step 1 (bound it — the queue's own check timeout is 60min). Treat
`OPEN` + `CLEAN` + "already queued" as **merging**, not failed. The failure this prevents is
*acting on the false negative* — re-pushing, force-merging, or opening a duplicate PR on top of a
merge already in flight. (2026-08-13, canopy-web#594: the strategy-flag error plus `state=OPEN`
looked like two independent confirmations of failure; it landed as `fed3f2b`.)

**Never report "shipped" on a merge-queue repo without `state=MERGED`.**

## Also check the org

`gh … -R` against the wrong org answers `[]` / "no results" with **exit 0** — and an empty answer
is exactly the shape of "nothing to worry about." When a `gh` query returns nothing and the
conclusion you're about to draw is *"clear to proceed"*, confirm the repo first:
`gh repo view -R <owner>/<name> --json name` errors loudly where `pr list` stays quiet.
(canopy-web is `dimagi-internal/canopy-web`, not `jjackson/…`.)

## Related
- `turn.md` — the turn close-out; it defers to this file for anything ship-shaped.
- `agent-turn-review` (your wrapper) — gates the *report* about the merge, never the merge.
- `task-tracker.md` — where a multi-PR initiative's state lives across turns.
