# Huddle — fleet-canonical leader procedure (canopy agent-core)

**Fleet-canonical process.** A team binds this with a thin skill (Ada: `skills/huddle`) that names
the leader, team, members, principal, sharing rule and the leader's ONE sanctioned send path —
apply this doc bound to that. To change THIS process for every team, PR canopy
(`plugins/canopy/agent-core/huddle.md` + `canopy version bump`).

A **huddle** is a team of agents syncing, led by one of them. It starts from the principal's
**priorities brief** — 3–5 numbered priorities with hard dates, written for the team before the
huddle — which every round prompt opens with. In the `work` type each member reports where it
can move those priorities (round 1: its `levers`), sees EVERY teammate's report and proposes
solo or joint work toward a brief priority (round 2), the leader merges duplicates, and each
member co-signs — or amends / declines — joint work it is named in (round 3). When a partner amends,
the proposal's lead and that partner settle the change DIRECTLY in a short **agreement thread**
(`canopy thread`, agent-core `thread.md`) that you moderate. Survivors become `suggested` board
tasks; the principal decides on the board.

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
Work in a scratch directory; `priorities.md`, `plan.json`, the prompts, `crit.json`,
`merges.json`, `props.json`, `outcomes.json` and `digest.md` are working files, not deliverables.

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

**1. The brief, then plan.** Get the principal's priorities brief FIRST, into `priorities.md`
(the binding names how — Ada: `bin/ada-huddle brief --out priorities.md`, which asks Eva, who
reads the principal's goals). Format: a numbered list, 3–5 items, each
`N. <priority, one line> — hard dates: <dates or none> — source: <where it comes from>`, plus an
optional `Not now:` line; themes and dates only, nothing restricted. Never write one yourself
from guesses — the point is that nobody guesses (huddle work-fleet-20261006: 3 of 4 members had
not read the goals and produced four different priority lists).
```bash
uv run --project "$CANOPY_ROOT" canopy huddle plan --team <team> --type work --members <a,b,c> \
  --days 7 --principal "<name>" --sharing-rule "<the binding's rule, verbatim>" \
  --priorities-file priorities.md --repo <your-repo> --out plan.json
```
The brief is stored verbatim in `plan.json` (`priorities_brief`) and opens every round prompt
under "<principal>'s top priorities". Without `--priorities-file` the engine still runs the
pre-brief huddle (round 1 asks each member to state the priorities) and warns loudly — say so
in your close. It picks the id (`<type>-<team>-<YYYYMMDD>`, `-2`… when taken), files the anchor turn, builds one
context pack per member (open tasks, projects, recent turns) and loads the team's last huddle
records with each filed outcome's LIVE board status (a principal's decline shows as `declined:`).

**2. Round 1 — report.** Each member answers with `state` (≤3 things it is mid-way through
that matter to the brief), `levers` (per brief priority it can move: the most valuable move in
the next 2 weeks, `kind` new / unblock / existing, `verified`), `offers` and `needs`
(`{"from": teammate or the principal, "ask"}`). For EVERY member:
```bash
uv run --project "$CANOPY_ROOT" canopy huddle prompt --plan plan.json --member <m> --round 1 --out <m>-r1.md
uv run --project "$CANOPY_ROOT" canopy huddle dispatch --plan plan.json --member <m> --round 1 --prompt-file <m>-r1.md
```
then `canopy huddle await --huddle <id> --round 1`, looped in the foreground until exit 0.

**3. Round 2 — roundtable.** Read every round-1 block (`canopy huddle status --huddle <id>`, and
the page). Write `crit.json` — per member, pointed questions about ITS report: brief priorities
it could move but has no lever for, levers that cannot land before a hard date, overlaps it missed with teammates' offers and needs,
claims without a link, levers that are an open task re-wrapped. `{"<member>": ["question", …]}`.
Then for every member that replied: `prompt --round 2 --critique crit.json`,
`dispatch --round 2`, foreground `await --round 2`. A round-2 proposal names a brief priority by
NUMBER, a `kind`, `cost_to_jonathan` (none / yes / decision / time + detail) and `fails_if`
(no confidence %), with dated plan steps (`too_late` when a step misses a brief hard date).

**4. Round 3 — merge duplicates, then co-sign.**

**4a. Merge duplicates (after round 2, before any round-3 prompt).**
`canopy huddle proposals --huddle <id> --out props.json` marks each proposal's `overlaps`: other
proposals for the same brief priority that share a person (lead or `with`). Two of those are
usually ONE piece of work (work-fleet-20261006: Ace's and Eva's IDM-demo proposals were the same,
both were co-signed and filed, and the principal had to decline one). Resolve every overlap in
`merges.json`:
```json
{"<kept title>": {"absorbs": ["<title>", "…"], "why": "one line"},
 "<title>": {"distinct_from": ["<title>"], "why": "how they differ"}}
```
`absorbs` drops the absorbed proposals and makes the kept one's partners the union of both
(each absorbed lead gets a part in `ask_of_partners`, and sees in its round 3 where its proposal
went — it `amend`s if the merge lost something). `distinct_from` says they really are different.
Keep the proposal with the right owner and the better plan. Re-run `proposals --merges
merges.json` until its `unresolved_overlaps` is `[]`. Round 3, `agree` and `file` REFUSE (exit 2,
naming the pairs) while an overlap is unresolved — pass `--merges merges.json` to each.

**Then the round-3 prompts.** With `props.json` from 4a, critique each
proposal — the evidence, overlap with open tasks and earlier huddle outcomes, value vs cost to the
principal's attention, whether the owner is right, and for joint work whether the split makes
sense — into `crit.json`: `{"proposals": {"<title>": "critique"}, "<member>": ["question on its own
proposals"]}`. Each member's round-3 prompt carries its own round-2 proposals verbatim with your
critique of each (it revises them, same title), plus every joint ask naming it — including a
proposal a teammate named it LEAD of, which that lead must co-sign too. Dispatch round 3 to every
member who proposed, or is named as a PARTNER or LEAD of, a proposal worth keeping (`prompt
--round 3 --critique crit.json --merges merges.json`; it refuses a member with nothing to
answer), then foreground `await --round 3`. Rounds 3 and 4 also carry each member's own round-1
and round-2 blocks verbatim ("What you said earlier").

**4b. Settle amends — agreement threads (only if any).** An `amend` is neither a yes nor a no —
the gate holds it, and the lead answered in the same round 3, so without this step every amended
proposal is held by construction. The lead and the amending teammate settle it between
themselves, directly, in a bounded thread you moderate (no relayed hop through you):
```bash
uv run --project "$CANOPY_ROOT" canopy huddle agree --plan plan.json --merges merges.json
```
It opens ONE agreement thread per open amend — participants the proposal's lead (`author`) and
the amender (`asker`), parent `{huddle, title, lead}`, context = the proposal verbatim + the amend
note verbatim; default 4 messages / 90 minutes — and prints each thread's id and its `run` line.
No amends → it opens nothing; go to step 5. It is idempotent (a re-run returns the open threads).
Then for EACH thread, in the foreground:
```bash
uv run --project "$CANOPY_ROOT" canopy thread run --thread <thr-id>   # exit 3 → run it again
```
The moderator loop sends message 1 to the author (who sees the change request), alternates, and
closes the thread itself: **agreed** when both latest positions are `agree` (the latest proposal
on the table is adopted), **not agreed** on a `decline`, out of messages, or out of time. Exit 0 =
closed. `canopy thread show --thread <id>` prints the conversation; it is also on the huddle page.
Every thread must be closed before step 5 — an amend whose thread is still open is held.

(The relayed round 4 — `prompt`/`dispatch --round 4`, where the lead answers `accept`/`reject` —
still works and `proposals` still reads it, but threads replace it: use it only if canopy-web's
threads API is unavailable.)

**5. Rank, file.** `canopy huddle proposals --huddle <id> --merges merges.json --out props.json`
again (it now
carries each partner's answer, how each amend settled, and whether the lead answered your
critique). An amend whose agreement thread settled agreed is `amend→accepted` and counts as a
co-sign — the thread's proposal replaces the original when it carried one; one that closed any
other way is `amend→not agreed` and holds the proposal ("Eva and Echo didn't agree: <why>").
`threads` names each amend's thread; `needs_agreement` lists amends with no thread yet (go back
to 4b). Its stdout carries `asks_of_jonathan`: every round-1 need addressed to the principal and
every proposal's `cost_to_jonathan`, de-duplicated (`{"ask", "who", "for"}`). Write
`outcomes.json` from the list: drop what your critique sank, rank best first (merging happened in
4a — a late duplicate goes in `merges.json` and `file --merges`). Do not hand-edit `answers` — a co-sign is the partner's, not
yours. Then:
```bash
uv run --project "$CANOPY_ROOT" canopy huddle file --plan plan.json --outcomes outcomes.json --repo <your-repo> --dry-run
uv run --project "$CANOPY_ROOT" canopy huddle file --plan plan.json --outcomes outcomes.json --repo <your-repo> --digest-out digest.md
```
`file` refuses an unresolved overlap (pass `--merges merges.json` if `outcomes.json` was not built
with it), then enforces the type's gates — every partner co-signed, and so did a lead a teammate named
(an unresolved `amend` holds, and so does one whose agreement thread closed without agreement or
is still open, or one a lead rejected in a relayed round 4; an agreed amend counts as a co-sign;
`file` re-reads the threads, so one that closed after `proposals` ran still counts), the
priority is a number in the brief (pre-brief huddles: stated in some round-1 report), a project
is named, the critique was answered,
nothing declined before without `new_evidence`, at most 5 — and files each survivor as a
`suggested` task on the lead's board (project find-or-create: a "(P3)" in the name that is on
the lead's board wins, else names match without a trailing "(…)", else a new project under the
stripped name) plus a linked task on each
partner's (patched with `source_url`/`rationale`/`plan` after the sync, which drops them), then
writes the Drive record and marks the anchor finished. Re-running is safe: tasks are found by
huddle page + title (and re-patched), the record is replaced. An over-long field is REJECTED, never
truncated — shorten it in `outcomes.json` and re-run.

**6. Tell the principal — once.** Run your pre-send review on `digest.md`, then send it through
your binding's send path: ≤5 lines, each linking the lead's board and saying which brief
priority it serves and what would make it fail, then ONE "Waiting on you" list (`file`'s
`asks_of_jonathan`: each ask once, with who is asking), and the huddle page. Nothing
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
  reached for that round; a joint proposal it never co-signed is held — and an amend whose
  agreement thread runs out of time closes not agreed (held, with that reason).
- **A thread message fails or comes back malformed:** `thread run` asks the same participant
  again (it spends one message — canopy-web counts every message turn). A refusal from
  canopy-web's guard (closed, past deadline, over budget, out of order) exits 2 with the reason.
- **`file` stops part-way or the Drive record fails:** it reports the tasks already filed and does
  NOT claim success. Fix the cause and re-run `file` — nothing is duplicated.
