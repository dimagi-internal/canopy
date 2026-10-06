# Huddle — fleet-canonical leader procedure (canopy agent-core)

**Fleet-canonical process.** A team binds this with a thin skill (Ada: `skills/huddle`) that names
the leader, team, members, principal, sharing rule and the leader's ONE sanctioned send path —
apply this doc bound to that. To change THIS process for every team, PR canopy
(`plugins/canopy/agent-core/huddle.md` + `canopy version bump`).

A **huddle** is a team of agents syncing, led by one of them. In the `work` type each member
reports what it has been doing and what it understands the principal's / Dimagi's priorities to
be (round 1), sees EVERY teammate's report and proposes solo or joint work (round 2), and
co-signs — or amends / declines — joint work it is named in (round 3). Survivors become
`suggested` board tasks; the principal decides on the board.

**The conversation is not stored — it is derived.** Each round is a one-shot turn the engine
tags (`origin_ref.kind = huddle_round`) and parents on the huddle's anchor turn; canopy-web reads
the prompt, the member's reply block and the resulting tasks back out of those tags and shows them
at `/w/<ws>/huddles/<id>`. The only new write is the clean-outcomes record in the leader's Drive
(`Process State/Huddles/<id>.json`), which the next huddle reads so a declined item is not
re-raised without new evidence.

## Running the engine

Run `canopy huddle` from the plugin's runtime bundle, never a global CLI on PATH (a team binding
may wrap it — Ada runs `bin/ada-huddle <verb>`, same verbs and flags):

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy huddle --help
```

Below, `canopy huddle …` is shorthand for `uv run --project "$CANOPY_ROOT" canopy huddle …`.
Work in a scratch directory; `plan.json`, the prompts, `crit.json`, `props.json`,
`outcomes.json` and `digest.md` are working files, not deliverables.

## Rules that are not negotiable
- **Every round turn is fresh, one-shot and read-only.** The engine never pins a runner and never
  continues a session; members are told not to run their inbox or board steps. Their one write is
  filing the reply block as their close-out.
- **Never background a wait, never end your turn while a round is out.** `await` runs in the
  FOREGROUND and returns within ~9 minutes: exit 0 = the round settled, exit 3 = run the SAME
  command again. A one-shot cloud turn that backgrounds the wait and ends has abandoned the huddle.
- **canopy-web is the state.** If it is unreachable the huddle cannot run — say so in your close.
- **Outbound happens once**, at step 6, through YOUR sanctioned send path (the binding names it),
  under your turn mode. Everything else here is a fleet operation.

## The procedure, in order

**0. Resume first.** `canopy huddle resume` (`--leader <you>` outside your repo). An unfinished
huddle < 72 h old comes back with its round states and `next` steps — continue it from there;
never start a second one beside it. `{"huddle": null}` → step 1.

**1. Plan.**
```bash
uv run --project "$CANOPY_ROOT" canopy huddle plan --team <team> --type work --members <a,b,c> \
  --days 7 --principal "<name>" --sharing-rule "<the binding's rule, verbatim>" \
  --repo <your-repo> --out plan.json
```
It picks the id (`<type>-<team>-<YYYYMMDD>`, `-2`… when taken), files the anchor turn, builds one
context pack per member (open tasks, projects, recent turns) and loads the team's last huddle
records with each filed outcome's LIVE board status (a principal's decline shows as `declined:`).

**2. Round 1 — report.** For EVERY member:
```bash
uv run --project "$CANOPY_ROOT" canopy huddle prompt --plan plan.json --member <m> --round 1 --out <m>-r1.md
uv run --project "$CANOPY_ROOT" canopy huddle dispatch --plan plan.json --member <m> --round 1 --prompt-file <m>-r1.md
```
then `canopy huddle await --huddle <id> --round 1`, looped in the foreground until exit 0.

**3. Round 2 — roundtable.** Read every round-1 block (`canopy huddle status --huddle <id>`, and
the page). Write `crit.json` — per member, pointed questions about ITS report: gaps between the
priorities it states and the work it shows, overlaps it missed with teammates' offers and needs,
claims without a link. `{"<member>": ["question", …]}`. Then for every member that replied:
`prompt --round 2 --critique crit.json`, `dispatch --round 2`, foreground `await --round 2`.

**4. Round 3 — co-sign.** `canopy huddle proposals --huddle <id> --out props.json`. Critique each
proposal — the evidence, overlap with open tasks and earlier huddle outcomes, value vs cost to the
principal's attention, whether the owner is right, and for joint work whether the split makes
sense — into `crit.json`: `{"proposals": {"<title>": "critique"}, "<member>": ["question on its own
proposals"]}`. Dispatch round 3 to every member named as a PARTNER or LEAD of a proposal worth
keeping (`prompt` refuses a member with nothing to answer), then foreground `await --round 3`.

**5. Merge, rank, file.** `canopy huddle proposals --huddle <id> --out props.json` again (it now
carries each partner's answer and whether the lead answered your critique). Write `outcomes.json`
from it: merge near-duplicates across members (keep one, say what merged in its `why`), drop what
your critique sank, rank best first. Do not hand-edit `answers` — a co-sign is the partner's, not
yours. Then:
```bash
uv run --project "$CANOPY_ROOT" canopy huddle file --plan plan.json --outcomes outcomes.json --repo <your-repo> --dry-run
uv run --project "$CANOPY_ROOT" canopy huddle file --plan plan.json --outcomes outcomes.json --repo <your-repo> --digest-out digest.md
```
`file` enforces the type's gates — every partner co-signed (an unresolved `amend` holds), the
priority was stated in some round-1 report, a project is named, the critique was answered,
nothing declined before without `new_evidence`, at most 5 — and files each survivor as a
`suggested` task on the lead's board (project find-or-create) plus a linked task on each
partner's, then writes the Drive record and marks the anchor finished. Re-running is safe: tasks
are found by huddle page + title, the record is replaced. An over-long field is REJECTED, never
truncated — shorten it in `outcomes.json` and re-run.

**6. Tell the principal — once.** Run your pre-send review on `digest.md`, then send it through
your binding's send path: ≤5 lines, each linking the lead's board, and the huddle page. Nothing
filed (`digest.md` empty) → no email; say so in your close. The principal decides by Accepting /
Declining on the board; the lead drains that command and starts.

**7. Close** your turn naming the huddle page (`canopy huddle view --huddle <id>`), what was
filed and held, and who was not reached.

## When something goes wrong
- **A round turn `failed` / never claimed:** check remediation (unclaimable turns, runner health),
  then re-send with `dispatch … --attempt <k+1>` and await again.
- **`malformed`:** the block failed the type's schema (`await` prints the problems). Quote them in
  that member's next-round critique; a member malformed twice is not reached.
- **`timed_out`:** a round's deadline is 90 minutes after its first dispatch. The member is not
  reached for that round; a joint proposal it never co-signed is held.
- **`file` stops part-way or the Drive record fails:** it reports the tasks already filed and does
  NOT claim success. Fix the cause and re-run `file` — nothing is duplicated.
