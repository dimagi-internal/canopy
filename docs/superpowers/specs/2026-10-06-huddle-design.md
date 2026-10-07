# Huddle — a team of agents syncs, led by one of them

Status: approved design (Jonathan, 2026-10-06). Spans canopy (engine + `work` type),
canopy-web (derived view), and the first team binding (ada). Implementation plan follows.

## Why

Ada's fleet sync (`ada/skills/fleet-sync`, `ada/bin/ada-sync`) runs a back-and-forth with each
fleet agent and emails Jonathan one decision. It works, but it is (a) Ada-only, (b) about how
well the *system* is working — every proposal is `new|improve|retry|sync` on the agent's own
operation, and (c) hub-and-spoke: agents never see each other.

Jonathan wants a second kind of output: each agent shares what it has been working on and what
it understands the principal's / Dimagi's priorities to be, and the agents work out what they
can push forward **as a team** — alone or together. And he wants to see the conversation, and
its outputs, on canopy-web. More sync types will follow, and other teams (not just Ada's fleet)
should be able to use the same thing. So it is canopy infrastructure, named a **huddle**
("sync" is taken on canopy-web by the manager-sync `AgentSync` record).

## Concepts

- **Team** — a leader agent + member agents. The leader runs the huddle from one of its turns.
  The first team is `fleet`, leader `ada`, members = fleet agents on canopy-web.
- **Huddle** — one run of a huddle type by a team. Id: `<type>-<team>-<YYYYMMDD>` (suffix `-2`…
  if one already exists that day).
- **Huddle type** — data, not code: per-round prompt templates, the reply-block schema, the
  gates, and the outcome rule. canopy ships `work`; a team may register its own (`health` later).
- **Round** — one fresh, one-shot, read-only turn per member. Same discipline `ada-sync` learned
  the hard way: never pinned to a runner, never a continued session, never backgrounded by the
  leader (foreground `await` loop, exit 3 = run it again).

## Principle: the conversation is not stored — it is derived

Everything the conversation consists of already lives on canopy-web:

| Piece | Where it already is |
|---|---|
| What the leader asked (incl. its critique) | the round turn's `prompt` |
| What the member answered | that turn's session messages; else its close-out report (`canopy agent turn --summary`) |
| How the rounds hang together | provenance: `parent_turn` = the leader's huddle turn, plus `origin_ref` |

So the engine stamps every round dispatch with:

```json
"parent": {"turn": "<leader turn id>"},
"origin_ref": {"kind": "huddle_round", "huddle": "<huddle id>", "type": "work",
               "round": 2, "member": "eva", "attempt": 1}
```

and the huddle has a **leader turn** carrying `origin_ref: {"kind": "huddle", "huddle": "<id>",
"type": …, "team": …}`. `plan` establishes it: inside a runner-launched turn it adopts
`CANOPY_TURN_ID` and tags it; in a hand-run session (no turn) it records one for the leader via
the close-out path (`canopy agent turn`, which gains an `origin_ref` field — §4), so every huddle
has exactly one leader turn id to parent its rounds on. Nothing re-records prompts, replies or round state. If retention has aged a session
out, the view says "transcript no longer available" — the outputs survive (below).

## 1. The Drive record — clean outputs only

`$GDRIVE_ROOT_FOLDER/Process State/Huddles/<huddle-id>.json`, written by the leader only (single
writer), through the same per-agent Drive identity `agent_gdoc` / `work_cursor` use:

```json
{"version": 1, "id": "work-fleet-20261006", "type": "work", "team": "fleet",
 "leader": "ada", "members": ["ace", "echo", "eva", "hal"],
 "leader_turn": "<uuid>", "started_at": "…", "finished_at": "…",
 "outcomes": [
   {"title": "Joint Q4 funder pipeline brief",
    "lead": "eva", "partners": ["echo"],
    "priority": "Connect funder pipeline for Q4",
    "project": {"agent": "eva", "name": "Q4 funder pipeline", "new": false},
    "task": {"agent": "eva", "ext_id": "T41"},
    "cosign": {"echo": "co-sign"},
    "why": "<one line>",
    "fate": "filed | held: <reason> | declined: <principal's reason>"}],
 "not_reached": [{"member": "ace", "why": "timed out"}]}
```

No messages, prompts or round state. The next huddle of the same team reads the last few records
(and the tasks they point at) so a declined or held item is not re-raised without new evidence,
and work already in flight is known.

## 2. Engine — `canopy huddle …` (canopy)

Stateless between steps; canopy-web is the state.

```
canopy huddle plan     --team fleet --type work [--days 7]     # → huddle id + context packs
canopy huddle prompt   --huddle H --member eva --round N --out eva-rN.md [--critique c.json]
canopy huddle dispatch --huddle H --member eva --round N --prompt-file eva-rN.md [--attempt k]
canopy huddle await    --huddle H --round N                     # foreground; exit 3 = again
canopy huddle status   --huddle H                               # derived from canopy-web turns
canopy huddle resume   [--team fleet]                           # unfinished huddle < 72h → next steps
canopy huddle file     --huddle H --outcomes outcomes.json      # tasks + Drive record
canopy huddle view     --huddle H                               # prints the canopy-web URL
```

- **plan** builds one context pack per member (turns, harness outcomes, open board tasks,
  **its canopy projects and their state**, merged PRs, last huddle records). It is generalised
  from `ada-sync`'s `build_pack`.
- **dispatch** goes through `orchestrator.canopy_web` (so provenance headers ride along) and
  sets `parent` + `origin_ref` explicitly. Thread key and idempotency key are per
  (huddle, member, round, attempt).
- **await / status / resume** query `GET /api/harness/turns/?origin_ref__huddle=H` and read each
  reply from the turn's session (or close-out report). Deadline = 90 min after the earliest
  round-1 dispatch's `created_at` (derived, never stored); stragglers are `timed_out`.
- **Huddle types** live in `huddle-types/<type>/` (canopy ships them; a team repo may add its own
  under the same layout): `round<N>.md` templates, `schema.json` for the reply block, and
  `gates.py` (pure, testable) for the type's refusals.
- The leader's skill orchestrates: canopy `agent-core/huddle.md` is the procedure; a team binds
  it with a thin skill (Ada: `skills/huddle`), the same way `task-tracker` is bound.

## 3. The `work` huddle type

**R1 — report** (every member). Read-only turn. Reply block ```` ```huddle ````:

```json
{"round": 1, "member": "eva",
 "worked_on": ["≤5 lines, each with a link/id"],
 "priorities": ["what you understand the principal's / Dimagi's priorities to be now, each with
                 where you learned it (thread, doc, goal, meeting)"],
 "projects": [{"name": "…", "state": "…", "next": "…"}],
 "offers": ["what you could do for a teammate"],
 "needs": ["what you need from a teammate or the principal"]}
```

The template quotes the team's sharing rule. For `fleet`: Jonathan, 2026-10-06 — share freely
in-fleet; if something must be excluded he will teach the owning agent (e.g. Eva's goals process).

**R2 — roundtable** (every member that replied). The prompt carries **every** R1 block. Each
member proposes ≤3 pieces of work, solo or joint:

```json
{"round": 2, "member": "eva",
 "proposals": [{"title": "≤10 words", "lead": "eva", "with": ["echo"],
                "priority": "<one of the R1 priorities, verbatim>",
                "project": {"name": "…", "new": false},
                "why": "evidence", "plan": ["…"], "effort": "S|M|L",
                "success_measure": "…", "confidence": 0.0,
                "ask_of_partners": {"echo": "what echo would do"}}],
 "feedback": "what would make this huddle more useful"}
```

The leader dedupes and merges near-identical proposals across members (recording the merge),
and critiques each surviving one (evidence, overlap with prior huddle records / open tasks,
value vs cost, owner, and — for joint work — whether the split makes sense).

**R3 — co-sign + critique answer.** Each named partner receives every joint proposal that names
it, with the proposer's text verbatim and the leader's critique, and answers per proposal:
`co-sign | amend (with the change) | decline (with why)`. The lead answers the leader's critique
in the same round. Max 3 rounds.

**Gates** (`huddle-types/work/gates.py`, enforced by `file`):
- a joint outcome is filed only if every partner `co-sign`ed (an `amend` needs the lead's
  acceptance in the same round, else it is held "amend unresolved");
- every filed outcome names a priority that appears in some R1 block, and a project;
- ≤5 outcomes filed per huddle; nothing that matches a declined outcome in the team's last
  records unless it cites that record with new evidence;
- no outcome without an answered critique.

**Outcome.** `canopy huddle file`:
1. for each surviving proposal: find-or-create its project on the lead's canopy-web board (both
   halves — Drive `Projects/<name>/` + canopy-web project, per `agent-core/task-tracker.md`), add a
   `suggested` task (owner = principal, rationale/plan from the proposal, link = the huddle page,
   `origin_ref.huddle` = H); partners get a linked `suggested` task for their part;
2. writes the Drive record (§1);
3. the leader then sends the principal ONE short email via its sanctioned send path: ≤5 lines,
   each linking the task, plus one link to the huddle page. **The principal decides by Accepting
   / Declining on the board** — the existing control surface; the lead drains the command and
   starts. A decline reason flows back into the Drive record on the next huddle's `plan`.

Huddle outcomes are visible on boards as soon as they are filed (unlike the health sync, which
stays invisible until its email) — this type is meant to be seen.

## 4. canopy-web — a derived view, no new storage

- **Turn filters**: `GET /api/harness/turns/?parent_turn=<id>` and `?origin_ref__huddle=<id>`
  (plus `origin_ref__kind=huddle` for listing), tenant-filtered like the rest of `list_turns`.
- **Close-out `origin_ref`**: `AgentTurnIn` (the `canopy agent turn` payload) accepts an
  optional `origin_ref`, so a hand-run leader can tag its huddle turn; tasks accept/keep one too.
- **Task filter**: `GET /api/agents/<slug>/tasks/?origin_ref__huddle=<id>` (or a cross-agent
  `GET /api/tasks/?huddle=<id>`), so outputs are found by tag, not by a stored list.
- **`/huddles`** — huddles derived from leader turns tagged `kind=huddle`: type, team, date,
  phase (derived: rounds dispatched / replied), outcome count.
- **`/huddles/<id>` — the conversation**: one column per member, rounds as rows. Each cell is a
  bubble with the reply block rendered (not raw JSON); the leader's critique shows inline as a
  reply above the next round. Joint proposals draw arcs between member columns, coloured by
  co-sign state (pending / co-signed / amended / declined). Rounds in flight animate. Clicking a
  bubble opens the full prompt + transcript (existing session view). Missing transcript →
  "no longer available", the cell still shows the close-out block if there is one.
- **Outputs panel** (same page): the tasks/projects carrying the huddle id, with LIVE status
  (suggested → accepted → in progress → done / declined), lead + partners, priority served.
- **Agent page**: "huddles I was in" + tasks that came out of them.

The block parser for rendering lives in canopy-web (the schema is published by canopy in
`huddle-types/<type>/schema.json`; canopy-web renders unknown types as plain text).

## 5. Ada (first team binding)

- `skills/huddle/SKILL.md`: binds `agent-core/huddle.md` to team `fleet` (members = fleet agents
  on canopy-web minus `NON_FLEET`), sharing rule, principal = Jonathan, send path = `bin/ada-email`
  (a deny rail keeps a huddle email off the raw path, like the sync's).
- The existing `fleet-sync` keeps running unchanged until the `health` port (§6).

## 6. Sequencing

1. **canopy-web**: turn + task filters; `/huddles`, `/huddles/<id>`, outputs panel.
2. **canopy**: `canopy huddle` engine, `agent-core/huddle.md`, the `work` type + gates, tests.
3. **ada**: `skills/huddle` binding + rail → first real `work` huddle (manual, from a session),
   viewed at `/huddles/<id>`. Then a weekly schedule.
4. Later: port `fleet-sync` to a `health` huddle type (its ledger/email semantics as the type's
   outcome rule) and retire `ada-sync`'s round driver. Only syncs run after the port carry the
   tags, so older syncs do not appear on `/huddles`.

## Error handling

- A failed / unclaimed round turn: `await` reports `failed`; the leader checks remediation
  (unclaimable turns, runner health) and re-dispatches with `--attempt k+1`.
- Malformed block: the next round's prompt quotes the schema errors; a member malformed twice
  is `not_reached`.
- Deadline passes: outstanding members are `timed_out` → `not_reached`; their proposals are held
  "deadline before co-sign / critique".
- Drive write fails: `file` refuses to report success; the tasks it created are listed so a
  re-run is idempotent (find-or-create by `origin_ref.huddle` + title).
- canopy-web unreachable: the huddle cannot run (it is the state); the leader says so in its close.

## Testing

- canopy: pure tests for templates rendering, schema validation, gates (co-sign, priority tie,
  declined-repeat, ≤5), id/idempotency keys, `origin_ref` stamping; `await`/`status` against a
  fake canopy-web turn list (no network).
- canopy-web: API tests for the new filters (incl. tenant isolation); a page test rendering a
  fixture huddle (3 members × 3 rounds, one joint proposal per co-sign state, one expired
  transcript).
- End-to-end: the first real fleet `work` huddle is the acceptance test — every member's rounds
  visible on `/huddles/<id>`, outcomes visible as tasks with live status.

## Out of scope

- Live agent-to-agent chat (rounds are relayed, one-shot turns).
- Storing transcripts or prompts anywhere new.
- Migrating `fleet-sync` (step 4, its own spec).
