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

## Local gates — a suite you contaminate reports a FALSE failure

Running the repo's suite in the background and then poking at a database in the foreground is the
natural shape of a turn — the suite is slow, so you use the time. It is also how you manufacture a
red result that has nothing to do with your change.

Django's pytest plugin reuses **one** database per repo (`--reuse-db` is in connect-labs'
`addopts`), named `test_<db>`. A backgrounded `pytest` owns it for the whole run. Any other process
that writes there — a scratch `manage.py` command, an inline script that inserts rows, a psql
session — is mutating state the suite is asserting against, and the failures surface somewhere
unrelated to what you touched.

**So: while a suite is running, do not touch its test DB.** If you need a database to experiment in,
use the dev database (`postgres`, not `test_postgres`), or wait. If you have already done it, the
run is void — kill it, drop the database, re-run. Use psycopg2 rather than `psql`, which is
frequently not installed on this Mac:

    pkill -f 'pytest <pkg>'
    # then, in python: terminate backends on test_postgres, DROP DATABASE, and re-run pytest

`--create-db` is NOT the escape hatch: it fails with *"database is being accessed by other users"*
while the suite still holds a connection, which reads like a permissions problem and isn't.

**Why this is a section rather than a shrug:** the contaminated run reported a real `F`, at 42%, in
a file I had not touched. The two tempting readings are "flaky suite" and "my change broke something
subtle" — both wrong, and both expensive. The correct reading was "I wrote to the test DB ninety
seconds ago." The clean re-run was **4543 passed, 3 skipped, exit 0**. Same shape as the
gate-output rule elsewhere in this skill: a result you trust has to come from a gate nothing else
was interfering with. (2026-08-24, connect-labs #1268.)

### The OTHER flavour: a reused DB that has gone stale reports failures nobody caused

Step 0b above is about a CONCURRENT writer. This one needs no second writer at all — it is
`--reuse-db` accumulating drift across many runs, and it looks completely different: a stable,
reproducible set of failures in files you never touched, which survives re-running and therefore
reads as a real upstream breakage rather than as noise.

The tells, all of which showed up together on 2026-08-27:

- `cannot truncate a table referenced in a foreign key constraint` from any
  `django_db(transaction=True)` test — the flush is missing a table Django no longer knows about,
  because the reused DB still has it.
- `check_migration_drift` reporting `N file(s) on disk, 1 recorded` — the DB predates migrations
  that have since landed.
- a data-migration test failing because its seeded row isn't there (it was seeded into a DB built
  before that migration existed).
- a count assertion off by leaked rows (`assert 9 == 3`).

**Baselining on a clean checkout does NOT distinguish this from a real breakage, and that is the
trap.** Checking out `origin/main` into a fresh worktree and re-running proves only "my branch did
not cause it" — the clean checkout uses the SAME stale database, so it fails identically. That is a
correct answer to *attribution* and it is silently the wrong answer to *"is main broken?"*.

**The discriminator is CI, and it costs one call:**

```bash
gh run list --repo <owner>/<repo> --workflow ci.yml --branch main --limit 5 \
  --json conclusion,headSha --jq '.[] | "\(.conclusion) \(.headSha[0:8])"'
```

CI always builds a fresh database. Green there plus red locally means the local DB is stale — full
stop. Then drop it and re-run; do not go looking for the bug.

```python
# Drop the stale test DB, then re-run. psycopg2 rather than psql, which is
# frequently not installed on this Mac. Confirm no suite is running first:
#   pgrep -fl pytest
import re, urllib.parse as up, psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

url = re.search(r"^DATABASE_URL=(.+)$", open(".env").read(), re.M).group(1).strip().strip('"')
p = up.urlparse(url)
target = "test_" + (p.path or "").lstrip("/")

conn = psycopg2.connect(dbname="postgres", user=p.username, password=p.password,
                        host=p.hostname, port=p.port or 5432)
conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
cur = conn.cursor()
cur.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
               WHERE datname=%s AND pid <> pg_backend_pid()""", (target,))
cur.execute(f'DROP DATABASE IF EXISTS "{target}"')
```

*Why this is here:* on 2026-08-26 I reported to Jonathan, twice, that "the connect_labs/mcp tests
are broken on main" — 8 failures plus 8 errors, and I had done the clean-checkout baseline and
believed it settled the question. CI was green on every one of those SHAs. Dropping
`test_commcare_connect` and re-running turned all 16 into **40 passed**. Nothing was broken. That is
a false finding about someone else's code, delivered with evidence that looked rigorous, and the
step that would have caught it was one `gh run list`.

Generalises to the rule this skill already states elsewhere: when a checker and the thing it checks
disagree, **the checker is a suspect too** — and "I baselined it" is a claim about the baseline's
environment as much as about the code.

## A `git commit` that FAILED looks exactly like one that worked, if you piped it away

`pre-commit` runs on commit and **fails the commit when a hook rewrites a file**
(black, isort). That is by design — you re-stage and commit again. But chain it
as `git add -A && git commit -q -m ... >/dev/null 2>&1 && echo DONE` and the
failure is invisible: DONE never prints, or prints from the next clause, and the
work simply is not committed.

This shipped a real defect on 2026-08-25. connect-labs#1283 merged **four source
files and none of its nine tests**, and the PR body listed those tests as
shipped. The squash commit had failed on a hook rewrite; the output was
suppressed; nothing else in the chain noticed. It went to `main` and deployed.

Two rules, and the second is the one that actually catches it:

- **Never redirect or `-q` a `git commit`.** The hook output is the signal.
- **Verify HEAD moved.** `git log --oneline -1` after committing, and
  `git diff --cached --stat` before. Cheap, and it fails loudly when the commit
  did not happen.

After a squash-merge, confirm what actually landed rather than trusting the PR:

```bash
git ls-tree HEAD <path> --name-only     # the file is really on main
gh pr view <n> --json files --jq '.files[].path'
```

Same class as the `--all-files` trap in hal's `labs-perf` §0 and the `pip install`
that exited 0 through a pipe while installing nothing: **a local check that
reports success without having done the thing.** Those are worse than no check,
because they are trusted.

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

## Green tests + a verified API is NOT a verified page

Measured 2026-08-27, and it took a screenshot from the user to catch.

A cross-run view shipped **blank** — an empty bar on every row, a
"PHASE 1 -> 1" axis, "queued" in every label. Throughout: component tests
green, backend suite green, and the endpoint hand-verified as returning the
new fields. All three were true. The page was still broken.

The UI read a DIFFERENT serializer. Three code paths built that payload;
the new fields went into two of them, and the one the screen actually used
hand-wrote a six-field subset. The component tests passed because they hand
the component a fixture — **nothing asserted the endpoint produces that
fixture's shape.**

So, before claiming a UI works:

- **Fetch what the PAGE fetches**, not the endpoint you happened to touch.
  Open devtools or read the route's loader; a sibling endpoint returning
  the right thing proves nothing.
- **Assert the field list a component reads** in a test against the
  SERIALIZER, not just against the component. Parametrise over the fields
  so adding a column means adding it there first.
- **`grep` for every place that shape is built.** "I added the field" is a
  claim about one function; the payload may have three authors.

The general form: **a fixture-fed component test and a hand-checked
endpoint can both be green while the two never meet.** The seam between
them is exactly where this hides, and only loading the real page closes it.

## Optimise what you MEASURED, not what you assumed

Same day, three wrong diagnoses on one slow page — worth the section
because each was confident and each was cheap to have disproved.

A run-listing endpoint took ~50s. The obvious culprit was a 1+2N
sequential Drive loop, so it got batched: ~25 calls to 2. **No
improvement.** Then a second copy of the same loop turned up one layer up,
and that got batched too. **Still no improvement.** Somewhere in there the
deploy pipeline got blamed for shipping a stale image; it hadn't, and
reading the workflow would have shown it builds its own.

Profiling the real path against real data took one command and settled it:

```
list_opp_runs: 12 runs in 19.52s
  get_content      12 calls   8.73s   <-- the actual bill
  list_folder      17 calls   5.66s
  get_contents      2 calls   3.77s   (the new fast path, working)
  find_in_folders   2 calls   0.90s   (the new fast path, working)
```

Twelve content reads for twelve runs: a per-run helper re-read the same
`opp.yaml` every iteration. Memoising it took four lines — 19.5s -> 8.9s,
and 43-100s -> 8-10s in production.

The batching was real work that made its own path 12x faster and moved the
page not at all. The tell was available before any of it: an isolated
timing showed the batched query at 0.5s and a read at 1s, so that whole
path's FLOOR was ~2s against a 50s endpoint. **When the part you are
optimising cannot account for the time, stop and profile.** A wrapper that
counts calls and seconds per method is usually a few lines and it ends the
argument.

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
