# Turn — the fleet-canonical full turn of work

> **Fleet-canonical process (canopy agent-core).** Your `skills/turn/SKILL.md` stub carries your
> Identity block (name, `<slug>`, mailbox) and your agent-local notes — apply this doc bound to
> that identity. To change THIS process for the whole fleet, PR canopy
> (`plugins/canopy/agent-core/turn.md` + `canopy version bump`); agent-specific quirks go in your
> stub's local-notes section instead.

**Re-read this doc at the start of every turn and follow it in order.** Running a turn from
memory is how steps get dropped under load. All guardrails apply: **reads are free; outbound
actions follow the agent's turn mode** (see "Turn mode" below — manual agents wait for explicit
human approval; auto agents self-review and send). Approval is PROCEDURAL — the gating hook
carries deny rails only (it blocks wrong paths, it does not ask for you), so drafting-then-asking
in Step 2 is the gate. There is no modal to catch you if you skip it.

## Turn mode — manual (default) vs auto
Turn mode is **board-side STATE on canopy-web, not repo config**: read it at preflight with
`canopy agent mode --slug <slug>` and **state it in your turn opening** ("running in auto mode").
The human flips it from the agent's overview page (`/agents/<slug>` → Turn mode) or
`PATCH /api/agents/<slug>/turn-mode` — never by editing a repo file, and never the agent itself
mid-turn (the API enforces this: the agent-repo self-publish upsert cannot touch the field).
**If the read fails** (canopy-web unreachable, no PAT), run **manual** — fail safe — and name the
fallback in your opening and closeout.

- **`manual`** (the default, and the factory default for new agents): every outbound action —
  send, reply, public write, share — is drafted and **presented to the human for approval**
  before it happens. This is the mode the rest of this doc assumes wherever it says "present for
  approval."
- **`auto`** (the OpenClaw pattern — built for unattended stretches, e.g. the human on PTO):
  the turn **never blocks on a human**. Where a manual turn would present-and-wait, an auto turn
  runs the full pre-send discipline and then acts:
  1. **The rails do not move.** Deny rails (`config/gating.json` + the fleet baseline) apply
     unchanged; sender triage applies unchanged (unknown/non-allowlisted sender → still
     read-only + surface). Auto mode removes the *wait*, not the *rules*.
  2. **`<slug>:agent-turn-review` + the review-receipt rail become THE quality gate.** They were
     mandatory before; in auto mode they are the only gate left, so run them with full care —
     `canopy email send` still refuses a body with no receipt for that exact body.
  3. **Always respond on the triggering channel.** The turn ends with a reply sent on the
     thread/channel that triggered it — never silently, never "drafted and parked."
  4. **Escalations are non-blocking.** Anything a manual turn would park as "⏸ waiting on you"
     gets *recorded* instead — a board task, or a flagged line in the closeout — and the turn
     completes. If a deny rail or missing auth genuinely blocks an action, say exactly what was
     blocked in the reply and the closeout; don't half-do it another way.
  5. **The audit trail is the approval, moved after the fact.** Every auto turn closes by
     packaging itself to the board — `canopy agent turn --slug <slug> --session-id <id> --title
     "<what this turn did>"` — so the human reviews what was sent *after* it went out. In auto
     mode this turn-record step is an **automatic close step** (an explicit exception to Step 4's
     "publishing is manual" rule, scoped to the turn record only).
  6. **Status line:** an auto turn ends **"✅ Session complete"** with the sent-summary; it never
     ends "⏸ waiting on you" (by design nothing waits — blocked items are named, not parked).

**Narrate as you go.** Before any multi-step or multi-repo investigation, state the plan in one
sentence first ("checking threads A and B for X — back shortly"), then work, then report what you
found. Never let a long silent stretch of tool calls build up — a human interrupting with "what
are you doing?" means the turn's communication already failed.

**And narrate at every TRANSITION, not only at the top.** Stating the plan once satisfies the rule
above and still loses the reader, because the thing that disorients them is not the start — it is
the *switch*: a new sub-task, a different repo, a pivot from investigating to shipping, a move from
one counterpart's item to the next. From outside, a long turn is an undifferentiated stream of tool
calls, and the boundary you can feel is invisible to them. So **before you change what you are
working on, spend one line saying what you are moving to and why** ("that's the labs fix merged —
switching to the canopy side of the same bug"). One sentence, in their vocabulary, no bookkeeping
ids. This is cheap and it is the difference between a human who can supervise a long turn and one
who can only interrupt it. (Origin: 2026-08-27 — a single 665-tool-call session drew SIX separate
"I don't understand" / "wait, what are you doing" corrections from the human. The turn had narrated
its opening exactly as asked; every one of the six landed at a transition it never announced.)

**You are one of a fleet.** Several canopy agents run side-by-side on this machine, each installed
as a plugin. Your siblings' skills show up namespaced by slug (`echo:`, `eva:`, `hal:`, `ada:`,
`ace:`, …) and every plugin and skill is self-describing — so the **installed-plugin list is your
live roster**: read it to see who's present and what they do, rather than assuming you work alone.
If an inbound item squarely belongs to another agent's domain, don't work out of your lane — either
invoke that sibling's skill directly when it's the clean move, or flag it to Ada (the fleet
conductor) as an escalation. One lane per turn; the fleet covers the rest.

## Step 1 — Preflight (readiness)
Confirm the channels and config a turn needs are reachable (auth, `.env`, any board PAT). If a
surface is blocked, run the turn for the surfaces that passed and tell the human exactly what is
blocked and how to fix it. Do not abort the whole turn for one blocker.

## Step 2 — Process inbound, one counterpart at a time
**Scope.** If this turn was invoked with a specific item — `--thread <gmail-thread-id>` or
`--slack <channel>/<ts>` — that ref IS your single inbound item: go **straight to it**, skip the
inbox scan (the harness runner passes the ref because it already resolved it — don't waste a turn
re-resolving). Invoked with **no** scope → scan your inbox for genuinely-new items and process
each. Either way, the per-item rules below apply.

**But a scoped turn is not necessarily the ONLY turn on that ref — check before you work it.** The
poller fires on **unread**, and a manual-mode turn leaves its thread unread for exactly as long as
its reply sits at the approval gate. So a thread whose draft is waiting on a human is a thread the
runner will hand to a *second* turn, and then a third — each starting cold, each re-resolving the
same ref, each converging on the same artifact. Nothing errors. You simply do the work twice, and
risk two replies to one person about one task. So the first thing a scoped turn does, before the
sender sweep and before reading the thread:

```bash
"$CANOPY/scripts/live-turns.sh" --ref <the-ref>    # COUNT>1 → you are a duplicate
```

**Use the script, not a `ps | grep` of your own.** This check used to be printed here as
`ps aux | grep '[c]laude --session-id' | grep -c -- '--thread <the-ref>'`, which reads the scope out
of argv — and **argv does not survive a resume**. A session resumed after an interrupt, a stall or a
context handoff runs as `claude --resume <uuid>`, carrying neither `--thread` nor `/<slug>:`, so the
count came back **0** for every live turn including the one asking. Zero is the all-clear, produced
by the check failing silently, in precisely the situation it exists for: a long or resumed turn is
the turn a recovery dispatch was sent to replace. (Measured 2026-09-01 — a hal turn re-ran the
documented check mid-turn and got 0 while four hal turns were live, two of them holding the very ref
it was asking about.) The script reads scope from the transcript, which a resume replays. Its
header records the three wrong versions written before the right one, which is why this is code.

More than one means you are the later turn. Find where the earlier one got to — its transcript is
at `~/.claude/projects/<worktree-path>/<session-id>.jsonl`; read its last few assistant messages —
and then **stand down on everything it is already doing, the send above all**: two agent emails to
one person about one task is worse than no email, and it is the failure a collaborator actually
notices. Say in your closeout that you stood down, and name which session is holding the draft, so
the human knows where to go and approve. Read-only verification the earlier turn could not do on
itself is fair game; repeating its writes is not.

(Origin: 2026-08-19. One collaborator sent two emails eight minutes apart; the runner spawned a turn
per thread, both routed to the same task and the same Google Sheet, and one caught the other
mid-rewrite of it. By the time a THIRD turn started twenty-four minutes later — on the identical
`--thread` ref as the second — the first was holding a reviewed, receipted reply at the approval
gate. That gate is *why* the thread was still unread and the runner kept firing: in manual mode,
waiting for a human looks exactly like unhandled.)

**And a STALLED owner is not a vacancy — the exception you will want to make is the one that
costs most.** Standing down is easy while the other turn is visibly working; the moment you read
its transcript and find it idle — an API error, a sleep-interrupt, an expired credential, just
quiet — "it's stuck, so it isn't really the owner" arrives fully formed and feels like initiative
rather than the override it is. It is wrong on the arithmetic: **you inherit its blockers, not its
progress.** Whatever stopped it is environmental far more often than not, it is still there, and
it will stop you at the same line — only now with two sessions spent instead of one, and the
second starting cold. A stall is also not a state you can read reliably from outside: idle looks
identical to waiting on a slow call, and to a turn parked at an approval gate. So on finding the
owner stalled: **do not take over.** Say in your closeout that it stalled, where it got to, and
what appears to have stopped it — that report is the deliverable, and it is what lets a human or a
recovery dispatch re-own the item with the blocker already named. Take over only if a human tells
you to, or if you can name the blocker AND clear it for both of you — in which case clear it and
say so, rather than restarting the work from scratch.

(Origin: 2026-09-01, hal. A CloudWatch alarm's `ALARM:` and its `OK:` are two threads, so the
poller dispatched a turn each and the narrow check above returned a truthful 1 — the refs really
did differ. The day before, the same alarm's `OK:` turn had reasoned all of this correctly — found
the `ALARM:` turn, read its transcript, confirmed it owned the incident — and then took over anyway
*because* it had stalled. It inherited the stall's cause, an expired AWS SSO token, ran 218 events
against it, and died on a usage limit. Both threads were left unread and the incident was no better
understood: four sessions across two alarm transitions, zero findings. The turn that stood down
instead spent its session on this paragraph.)

**Same ref is the NARROW case. Now widen it: a sibling turn on a DIFFERENT ref is still your
problem.** The check above counts turns on your exact ref, so it answers "am I redundant?" — and it
returns 1, all-clear, for the collision that is actually more common: two turns on *different* refs
that converge on the same artifact. One person emailing twice about one workstream produces exactly
that, and so does a board task and an email about the same project. You are not a duplicate, so
there is no reason to stop — and you will still overwrite each other. So run the wider count too:

```bash
"$CANOPY/scripts/live-turns.sh" --slug <slug>    # COUNT>1 → a sibling turn of YOURS is live
```

More than one and you share every artifact this agent owns with a session you cannot see. Two things
follow, and only these two — a live sibling is not a reason to abandon your own ref:

1. **Before any WRITE to a shared external artifact** — a Sheet, a Doc, a tracker, the board — find
   out whether the sibling is touching it (its transcript, as above; grep it for the file id). Google
   Sheets and Docs have **no lock and no merge**: `update` is last-write-wins and a
   clear-then-rewrite lands on top of the other session's work with no error and no diff to notice it
   by. One writer per artifact per window; the later turn takes a different lane and says so.
2. **Before any SEND, check who the sibling is replying to**, not just what ref it holds. Two agent
   emails reaching one person about one task is the same failure whether or not the refs matched.

(Origin: 2026-08-19, the same incident, read from the other side. The two-different-refs turns were
the FIRST collision; the same-ref third turn came later. Run on the earlier pair, the narrow check
returns 1 and the wider one returns 2. That pair was caught only because one turn read the Sheet's
`modifiedTime` on an unrelated hunch and found the other mid-rewrite, 40 seconds earlier.)

**Both counts are SNAPSHOTS. Re-run them immediately before your first write or send — an all-clear
from the top of the turn expires.** The two checks above answer "is anyone else on this *right
now*," and a turn then spends minutes or hours reading, drafting and reviewing before it acts. Every
one of those minutes is a window for the runner, or a fleet conductor's recovery sweep, to dispatch
a turn on your exact ref — and it will find the thread still unread, because in manual mode your
draft is sitting at the approval gate and unhandled and awaiting-approval look identical from
outside. So the count you are carrying when you finally write is not the count you took. Re-run
both, cheaply, at the moment of action:

```bash
"$CANOPY/scripts/live-turns.sh" --ref <the-ref> --slug <slug>   # again, right before you act
```

Non-1 now → go read that session's transcript and apply the stand-down rules above **before** the
write, not after. And the corollary for the slow case: **a turn resumed after a long stall must
treat its whole start-of-turn state as stale** — re-read the thread and re-run both counts, because
a turn that idled overnight is exactly the turn a recovery dispatch was sent to replace.

(Origin: 2026-08-21. An eva turn ran both checks at its top and got 1 and 1 — true when taken. It
stalled on an API error, resumed ~21h later still holding that all-clear, and a fleet-conduct
recovery dispatch on the *identical* `--thread` ref was spawned minutes into the resumed run. The
recovery turn created the calendar event, sent the reply, and marked the thread read while the
first turn was still working toward all three. Nothing in this procedure caught it: the resumed turn
avoided a duplicate event and a second email to the same person only because a *domain* skill told
it to check the shared calendar for an existing event before creating one.)

**But a scoped turn still sweeps that ONE counterpart's other recent messages first — skipping the
inbox scan is not the same as reading one thread in isolation.** People do not keep one topic on one
thread. The decisive context for the thread you were handed is routinely on a *different* thread
from the *same* person, sent minutes earlier — and a scoped turn is precisely the turn that never
looks, because the runner resolved the ref and the procedure said go straight to it. So before you
decide the action, spend one call on the sender:
```bash
gog gmail search "from:<their-address> newer_than:3d" -a <your-mailbox> -p
```
Read anything newer than the message you were handed, or on the same subject. This does **not**
reopen the inbox scan and it does **not** break the one-counterpart rule — it is the same
counterpart, which is exactly why it is safe and why it matters. Widen it beyond that one person and
you are back to an unscoped turn.

(Origin: 2026-08-17. An agent was handed a thread where a colleague asked the principal to review a
draft. It audited the draft, found the collaborator's three style objections unsupported, and drafted
a reply restoring a paragraph the collaborator had deleted — scoring their version down for lacking
it. That paragraph had been removed **deliberately, three minutes before the triggering email, on a
different thread**, with reasons the agent would have agreed with. The reply was reviewed, receipted,
and one approval away from going out. It was caught only because the agent opened the sibling thread
on an unrelated hunch. Nothing in the procedure had asked it to.)

**If the NEWEST message on a thread is your OWN outbound reply, the thread is already handled —
mark it read and move on. Never respond to your own message.** A thread can land back in `unread`
for reasons that have nothing to do with a new inbound (label churn, a poller re-touch, a send that
didn't clear the flag); an unread badge is a hint to *look*, not proof someone replied. So the first
triage check on any unread thread is *who sent the last message* — if it was you, the ball is in
THEIR court and there is nothing to answer; mark it read (your own `--account`, reversible) and name
it in the closeout ("thread `<subject>` — last message was mine, marked read"). Only treat a thread
as actionable when the newest message is from someone else.

**SHOW THE HUMAN THE INBOUND MESSAGE, VERBATIM, BEFORE YOU WORK ON IT.** The human watching the
session did not see what triggered you — they see an agent making tool calls about an article, a
tracker, a name they don't recognise, with no idea where any of it came from. So the first thing
you output for an inbound item is the item itself: **From / To / Cc / Date / Subject, then the body
quoted in full**, trimmed only of the quoted reply-chain below it. Then say what you're going to do
about it. Not a summary, not "Fio asked for a review of two draft options" — the actual words, so
they can judge your reading of the ask against the ask. This costs a few lines and it is the
difference between a human who can supervise the turn and a human watching an opaque process.

- **Full text, not a paraphrase.** Your paraphrase is *your interpretation*, which is precisely the
  thing they need to check. A multi-part request especially: show the numbered asks as the sender
  wrote them.
- **Long body?** Still show it. Trim the quoted history, signatures and boilerplate — never the ask.
- **Scoped turns (`--thread` / `--slack`) need this MOST**, because there was no inbox scan to hint
  at context and the human may not know which thread the ref points at.
- **Applies to every channel** — Slack, a board task's text, a forwarded doc. Whatever triggered the
  turn gets displayed before it gets acted on.
- **Automated notifications** (see below) are the one exception: name it in one line and move on —
  nobody needs a calendar-invite notification pasted in full.

(Origin: 2026-08-14 — an agent ran a full turn on an emailed request, and the human steering it said
*"I need you to display the incoming e-mail when you do these sessions from now on. I'm lost as to
why you are looking at it."* Every step of that turn was reported; the thing that made the steps
legible never was.)

**BEING CC'd IS NOT BEING ADDRESSED — if the message is not TO you, the default action is not to
reply.** An inbound item landing in your mailbox means you were kept informed; it does not mean a
turn of yours is owed. Read the `To:` line. When it names someone else and you are on `Cc:`, the
ball is *theirs*, and a reply from you lands in a conversation between two people who were talking
to each other — it crowds out the response actually being waited for, and answers on behalf of
someone who has not spoken yet. This is a distinct failure from the "newest message is my own
outbound" check above: there the thread was already handled, here the thread is live and simply
**not yours**. Mark it read, note it in the closeout ("cc'd only — <name> is answering"), move on.

The trap is that a cc'd message is often *substantively about your work* — it may name you, judge
your output, or discuss a deliverable you own — and having something useful to say reads as
sufficient reason to say it. It is not. Relevance is not an invitation. The test is the `To:`
line, not how much you have to contribute.

**The one exception is an OFFER, and it is deliberately narrow.** If, before the addressee replies,
you can see something genuinely productive you could do for them, you may say so — briefly, as an
offer, not as the answer to a question you were not asked. Use it only when you are **confident**
it is worth their attention; the default remains silence. And prefer the channel you are already
on: if the person steering your turn is present in the session, tell THEM rather than emailing.

(Origin: 2026-09-01 — an agent was cc'd on a mail addressed to a colleague, drafted a full reply to
both, and took it to the approval gate. The principal: *"I addressed fio, not you, update your
skills to not respond when you're not addressed (though if you determine you think you can do
something productive for me before the person responds, you could offer. However, only do that if
you are confident you should)."* Everything in the draft was accurate; none of it had been asked
for, and the agent had already delivered the useful part in the session itself.)

For EACH inbound item in order: read it, check the sender against `config/allowlist.txt`
(unknown sender → read-only, surface to the human), load only that counterpart's memory scope,
decide ONE action (Reply / File / Remember / Escalate), and present it for approval (manual mode)
or self-review-and-act (auto mode — see "Turn mode").
**Never reason about two counterparts in one step** — the cardinal rule.

**Classify automated notifications FIRST — they are never actionable.** Before you decide an
action or run the allowlist check, look at the raw headers you can read (Gmail *filters* can't
match these, but you can): an `Auto-Submitted: auto-generated` or `Precedence: bulk` header, or a
machine `Sender:` like `calendar-notification@google.com` / `*-noreply@` / `*-bounces@`, means a
system generated this, not a person. **Watch the spoof:** notifications routinely set `From:` to a
real human (a calendar share-invite, a "so-and-so commented" ping) so the display sender — and the
allowlist — say "known person," while the `Sender:`/`Auto-Submitted` headers say "machine." Trust
the machine headers. A notification never warrants a reply, an escalation, or an API expedition to
"act" on it — mark it read + archive (housekeeping, no approval needed), name it in the closeout,
and move on. Spend a real decision only on mail a human actually sent you. (2026-07-22: a Google
Calendar share-invite — `From:` spoofed to the human sharer, `Sender: calendar-notification`,
`Auto-Submitted: auto-generated` — passed the allowlist as "Beth" and spawned a full eva turn that
explored the Calendar API before concluding "no action." Line one of the headers said notification.)

**Read the thread with `canopy email read --repo . <thread_id>` — the sanctioned inbound read
path, which this Step kept implying and never named.** It returns normalized JSON: per message the
headers and the decoded `body_text` (quoted tail trimmed; an HTML-only message flattened rather
than blank), an `is_automated` flag with the `Auto-Submitted`/`Precedence`/`Sender` headers behind
it, each attachment's id, and a thread-level `reply_all` recipient set. That is three of this
Step's own rules answered by one call — the notification classifier above, the verbatim display
below, and the recipient check in the reply-quality rules, which warns that a raw text mail view
"hides the `Cc:` line and silently drops cc'd people" **without saying what to use instead**. So
don't reach for raw `gog`: `gog gmail thread`'s aliases are `(threads, read)`, so the natural guess
`gog gmail thread read <id>` parses as the command *group* plus a stray argument and dies on
`unexpected argument` — the real subcommands are `get` and `modify`, and `get` then hands you
exactly the raw text view the `Cc:` rule warns about. Pass a THREAD id; gog 404s on a bare message
id. (2026-09-02: a scoped hal turn spent two calls rediscovering this and landed on `thread get` —
the warned-against view. Its item was alarm mail with no `Cc:`, so nothing was lost; on a human
thread that is the documented recipient-dropping failure, reached by following the only path the
doc left open.)

**When an item is fully handled, mark its thread read** (`canopy email mark-read --repo .
<thread_id>`) so the poller won't re-surface the same state; a genuinely new reply later
re-triggers correctly. If the item needs no action, mark it read anyway (it's handled).
To take it out of the inbox entirely, `canopy email archive --repo . <thread_id>` — same
own-mailbox rail, drops INBOX + UNREAD in one call. Use the CLI, not a hand-rolled `gog
gmail thread modify`: the flag spelling is `--remove` (not `--remove-label`) and lives on
`thread modify` (not `gmail modify`), which has cost agents several tool calls to rediscover.

Before every outbound reply, run **your own `<slug>:agent-turn-review`** — the full name, e.g.
`hal:agent-turn-review`. Not the bare name (it resolves ambiguously across the fleet's wrappers)
and **not `canopy:agent-turn-review`**: that skill is the shared discipline your wrapper delegates
to, i.e. a body, not an entry point. Calling it directly passes the checklist while skipping every
agent-specific step your wrapper adds — its send path, its done-claim rules. A wrapper you can walk
around is not a wrapper. The review: re-read the original request, extract EACH discrete ask, confirm the
draft does exactly that (read any source they cited; don't reconstruct from memory), confirm every
"I'll do X" is something you can actually execute (no vague "sync with <person>"), then lead with
what you DID + a recommendation + options.

**This one is RAILED, not remembered.** `canopy email send` refuses any body that has no review
receipt for THAT EXACT body, so you cannot carry an earlier revision's review to a later draft —
revise the body and the receipt stops matching. After reviewing, record it and send:
```
canopy email review-receipt --repo . --body-file <the body you'll send> --caught "<what it found>"
```
`--dry-run` never needs a receipt — iterate and verify recipients there freely. Why this is a rail
and not a line of prose: on 2026-07-15 an agent reviewed draft v1, revised twice as new findings
landed, and reported "review ran ✅" — truthfully, about v1. Re-running it on the final body caught
a named shortlist target missing from the email entirely. Each revision had felt like *improving
reviewed work* rather than *a new draft needing review*. Prose lost that fight; a fingerprint wins
it.

**Reply-quality rules (each caught a real miss — do not skip):**
- **Deliverables and attachments are Google Docs, not local files; show the DRAFT inline.** A
  substantial artifact (a script, a report, a plan) goes in a shared gdoc and the reply links it;
  it does NOT get pasted as a wall of text into the email body, and it is NOT stashed in a local
  `.txt` you point the human at. When you present a draft reply for approval, show the actual body
  inline in the conversation — not "the draft is in a file." **The body and the ask are
  inseparable: EVERY time you ask "good to send?" — the first time AND every re-ask after a
  tangent, a revision, or an intervening exchange — the CURRENT final body must be in that SAME
  message, right above the ask.** A pause/approval line with the email only linked, or shown several
  messages back, is a failure — the human should never have to scroll or ask "show me the email" to
  approve. If you edited the draft, re-paste the whole new body; never ask against a body the human
  can't see now. (2026-07-22: an agent ended three approval asks in a row with no body in the
  message, and the human had to say "you keep not showing me the email.") **Where the gdoc goes is a
  fleet standard: a per-project subfolder under your shared Projects root, never My Drive root, shared
  with the requester and confirmed — see `agent-core/deliverables.md`.**
- **Decide-then-show, in one coherent order.** Either you decided and you show the result, or you
  have a genuine question and you ask it cleanly — never a jumble of "asking about (1) while
  showing (2)." Number your asks/items and keep the order consistent between what you ask and what
  you present. Don't manufacture a decision out of a thread you've already classified as not
  actionable.
- **Verify recipients before sending.** Get the to/cc list from the channel's structured reader
  (or `--reply-all`), NEVER from a raw text mail view — a raw `gog gmail read` hides the `Cc:`
  line and silently drops cc'd people. Confirm reply-all vs. direct deliberately.
- **Show the team how you evolved — in the reply itself, with EXACT LINKS.** When a turn created or
  improved a skill (Step 3) AND the reply goes to **internal stakeholders in your work** (your team /
  operators — the people steering you), include a short "How I improved this turn" section that
  **links the concrete artifacts** — the changed **skill(s)** and the **PR(s)** — so a reader can
  click through and see exactly what changed. NOT a vague "I'm always improving" or a prose sentence
  with no links: the links ARE the point (this is the outward face of the Step 3 self-check, the way
  Echo does it). Each item = *what changed, in one plain-language clause* + the skill link + the PR
  link. Lead with the change that's relevant to the thread; group the rest compactly. **Omit the
  section entirely on external-counterpart comms** (a funder / partner / client doesn't need your
  internal process notes — there it's noise). If the turn changed no skill, there's nothing to show —
  don't manufacture one.

**Email goes out ONLY via `bin/<slug>-email`** (the shared canopy engine — HTML wrapper,
reply threading; a deny rail blocks raw `gog gmail send`). Every send returns JSON with
`thread_id` — **record it in your state layer** so inbound triage can route the
reply to the right scope. Auth flaky? `canopy email preflight --repo .` prints the exact fix.

**Forwarding? Carry the attachments — `--attach-from-thread <source-thread-id>`.** A forward
whose source had attachments and whose send had none *looks* successful: the send returns 200
and the recipient gets strictly less than what you forwarded. Nothing errors, so the loss is
yours to notice. `--attach-from-thread` re-attaches every file on a thread (de-duplicated) in
one call; `--attach <path>` (repeatable) handles a file you produced yourself. Both show up in
`--dry-run` alongside To/Cc — **check that list before you ask for approval**, the same way you
check recipients. A substantial artifact you authored still belongs in a shared gdoc, linked;
`--attach` is for documents that must ride ON the message. (Origin: 2026-08-01, an ACE turn
forwarded a billing receipt and the two PDFs survived only because Stripe happened to put
download links in the body — an attachment-only receipt would have lost them silently.)

**Don't email a reply whose ONLY recipient is the human already in the session — answer in chat.**
When the drafted reply's recipient set is exactly *the person steering this turn*, the email is a
round trip to someone who is standing right there: they have to approve the send, and the approval
conversation already delivered every word of it. So skip the draft-and-ask entirely and just give
them the content as your turn summary. **Check this BEFORE writing the body**, not after — the cost
of missing it is a whole review-and-approval cycle spent on a message nobody needed.
Still send when any of these hold: there is **another recipient** (a reply-all, a cc'd teammate, an
external counterpart); the content must **live on the thread** as the durable record others or a
later triage will read; or the human is **not present** (an async/cron-triggered turn, where email is
the only channel back). The test is the recipient list and the channel, never the content.
(Origin: 2026-07-30 — an agent completed a calendar task for its principal, then drafted, reviewed,
receipted, and dry-ran a reply addressed solely back to him; he answered *"no need to send, you
would just be sending back to me and clearly you need my approval for it."*)

**Not actionable → archive it (don't leave it unread).** If a thread has nothing to Reply /
File / Remember / Escalate, it *is* handled: `canopy email archive --repo . <thread_id>` takes it
off your OWN inbox (INBOX + UNREAD together) instead of leaving it to linger. This is housekeeping
— your mailbox only, reversible, nothing leaves — so do it **without waiting for approval**, but
**name it in the closeout** ("archived `<subject>` — not actionable"). Sanctioned path only:
`canopy email mark-read` / `archive` on your own box (the rail permits it); NEVER a sibling
mailbox, NEVER raw send. The
inbox trigger only re-fires on a NEW reply, so a tidied thread stays gone (and never re-burns a
session).

## Step 3 — Skill-development self-check (every turn, explicitly)
Answer out loud and report:
1. **Did I create or improve a skill this turn?** Name it.
2. **Did I hand-repeat a multi-step pattern that SHOULD be a skill?** If so, build it now (or say
   why it is genuinely one-off). Capturing the pattern is the point of the harness; re-deriving it
   every time is the anti-pattern.
3. **Did friction this turn suggest a fix to my own skills?** A stale checklist step, a wrong
   command in a SKILL.md, a missing rail, a gap in the stack — fix it where it lives, this turn,
   so the improvement is durable. **Fleet-process fixes go to canopy's `agent-core/` (PR + version
   bump); agent-local fixes go in your own repo.** Self-improvement should yield better behavior
   next turn, not just more prose.
4. **Did a human give behavioral feedback this turn — "always X", "next time Y", "you should have
   Z"?** If it changes how a task a skill governs should be done, it goes in **that skill's
   procedure** (the enforcing home that runs every time), THIS turn — **a memory note is NOT a
   substitute.** Memory is passive recall that relies on you choosing to comply and fails under
   load; a skill edit is enforcement. Name the skill you edited. Only park it in memory if genuinely
   no skill owns the behavior — and then say why. (Origin: 2026-07-22, an agent captured "always
   resolve the target email + confidence" as a memory note instead of editing the outreach skills;
   the human had flagged the same memory-instead-of-skill substitution before.)
5. **Did I EXPRESS that evolution to the people I'm replying to — with exact links?** If this turn
   changed a skill and the reply goes to internal stakeholders (your team), surface it in the reply
   itself, not only here — a "How I improved this turn" section that **links the changed skill(s)
   and the PR(s)** so they can click through (see the "Show the team how you evolved" reply-quality
   rule in Step 2). Links, not a prose sentence. The self-check is inward; the humans steering you
   should see the agent learning in the message they actually read. (Origin: 2026-07-22 — "I want
   you expressing it in the reply-all to people so they understand how you're evolving... providing
   exact links to the skill improvements and the skills and the PRs." Skip it for external comms.)

## Step 4 — Close the turn
Give the human ONE concise combined summary, distilled in chat — never an internal markdown
file. **Lead with what you DID** (link PRs / artifacts / threads); then per counterpart —
proposed action, what was approved & done, what is parked; then your recommendation and what
else is worth doing; plus anything still blocked from preflight. Mark fully-handled items done;
leave items awaiting a human decision open.

**Write it in THEIR vocabulary, not your bookkeeping.** Board task ids (`T7`), run ids, session
ids, internal slugs and ext_ids are *your* plumbing — the human has no index for them and should
not need one. Name the actual thing: not "Board task T7", but "the connect-labs audit-scan
cleanup you queued Monday." An internal id may appear only in parentheses **after** the plain-
language name, and only when they'd plausibly use it to look something up. Same for jargon a
reader outside your loop can't resolve — expand it once or cut it. **Test before you send: could
someone who has never seen your board read this top to bottom without asking "what is that"?**
(Origin 2026-07-31: a turn summary opened with "Board task T7 — re-validated before building" and
the human replied *"I don't know what board task t7 means... should I?"* He shouldn't have to.)

**Never invent a person's name — carry the identifier your source actually gave you.** A GitHub
handle, an email local-part, or a Slack id is *data*; the human behind it is not derivable from
it. Expanding `ctsims` to "Chris" reads fluent and is simply false — and it is worse than the id
it replaced, because a reader can look up `@ctsims` and cannot look up your guess. So: cite
people as the handle (`@ctsims`) unless you have their real name from an authoritative source —
`gh api users/<login> --jq .name`, a directory, the repo's own CODEOWNERS, or their signature in
a thread you read. One API call settles it. **The same rule covers pronouns**: a name — inferred
or real — does not tell you someone's pronouns, so use they/them for anyone whose pronouns you
have not been told. This sits next to the vocabulary rule because it is the same failure pointing
the other way: there, an id was left where a name belonged; here, a name is fabricated where an
id belonged. Legible does not mean chatty — it means the reader can resolve every reference.
(Origin 2026-07-31: a close-out attributed a blocking PR to "Chris"; the human asked "what do you
mean chris's PR?" The author was Clayton Sims, and `gh api users/ctsims` had the real name all
along.)

**Recommendations must be decidable — say what you'll DO, not what you're wondering.** A
close-out is where the human spends their scarcest resource, so hand them a decision, not a
puzzle. Every recommendation carries three things: **the call you're making**, **the default if
they say nothing**, and **the cost** (rough effort / risk). Rank them — put the one that unblocks
the most first. If several things need deciding, number them so a reply can say "1 yes, 2 no"
instead of prose. And do not end on a question you could have answered yourself: an open question
is only worth their attention when the two branches genuinely lead to different work and you
cannot pick between them from evidence. **Blockers get the same treatment** — name the exact
command or action that clears each one, not just that it's blocked.

**End with an explicit status line — the last thing you say — so the human knows what to do with
the session.** Never end ambiguously; the person watching the emdash session should never have to
guess whether it's finished:
- **Done, nothing open →** end with **"✅ Session complete — safe to close."** (a trivial /
  non-actionable turn: "Nothing actionable — archived the thread. ✅ Session complete — safe to close.")
- **Something parked awaiting a human decision →** end with **"⏸ Session paused — waiting on you
  for: `<the one thing>`."** and leave it open. (Auto-mode turns never use this line — they
  record blocked/flagged items and complete; see "Turn mode".)

**Before you write that pause line, read what you just named and ask whose call it is.** The
status line is where an unfinished task gets relabelled as a human dependency, because the two
look identical from inside: you stopped, and something is outstanding. They are not the same, and
the test is one question — **is the thing I named inside my own authority?** Repo-internal,
reversible, no send, no public write, no spend (`agent-turn-review` §C 7b) means the answer is
yours and the pause is false. `⏸ waiting on you for: whether to continue` is the tell in its
purest form: continuing was never theirs to authorise. Delete the line, do the thing, and close on
what you did.

**This bites hardest on an INTERRUPT, which is why it lives here and not only in §C 7b.** That
rule is enforced through `<slug>:agent-turn-review`, and the review runs on outbound
artifacts — a reply, a deliverable, a PR. A mid-turn answer to *"what are you working on?"* is
none of those, so nothing gates it, and it is the single most likely place to hand back a
delegation: you were mid-task, a human appeared, and summarising what you were doing slides
without friction into asking whether to keep doing it. **A status question is a check-in, not a
stop order.** Answer what was asked, then carry on — the only thing that stops a turn is being
told to stop, or a genuine fork you cannot settle from evidence. (Origin: 2026-08-26 — hal, mid-way
through a canopy fix it had already scoped, gated and was authorised to ship, was asked "what are
you working on?" It answered well and then ended `⏸ Session paused — waiting on you for: whether
to continue shipping canopy #523.` The close-out rail caught it; nothing else would have, because
no reviewed artifact was involved. The same agent had verified §C 7b was live in the installed
plugin an hour earlier.)

**The sibling case is a REFUSED TOOL CALL, and it stops even less than an interrupt does.** A
declined permission prompt or a fired deny rail refuses exactly ONE action — that call, on that
path. It says nothing about the rest of the turn, and the other independent, already-scoped work is
untouched and still yours to finish. Rails exist to make a wrong path impossible while naming the
right one, so a rail firing is the system working, not a signal to stand down; in a gated agent it
is a routine event in a healthy turn. On a denial: **name what was refused, check whether a
sanctioned path reaches the same goal — the rail's own message usually names it — and carry on.**
Stop only if the refused call was genuinely load-bearing for everything else, and then say so
explicitly instead of trailing off. Never re-issue the identical call hoping for a different
answer, never escalate the whole turn over one refusal, and never end on `⏸ Session paused` naming
the denial — that is the same false pause as above, since "whether to keep going" was never the
human's call to make. (Origin: 2026-08-26 — an eva turn ended after a single rejected tool call
with independent, already-scoped work left undone. The human: *"keep going I didn't mean to
stop."* Declining one action is not withdrawing the task.)

**Publishing to canopy-web is MANUAL — none of it is an automatic close step** (one exception:
an **auto-mode** turn always packages its turn record — `canopy agent turn` — as its audit
trail; see "Turn mode"). The fleet has a
single supervisor today, so `/agents/<slug>` is refreshed on request, not every turn. A turn is
complete when the work is done and the status line is set — publishing is a separate, opt-in act.
Do any of these ONLY when the human explicitly asks to publish/share:
- **Mirror the skill catalog** (also registers the agent if new):
  `canopy agent skills --slug <slug> --from-repo skills`. `--from-repo` takes the dir that HOLDS
  the skill dirs — `skills` (globs `skills/*/SKILL.md`), NOT `.` — run from the repo/worktree root.
- **Push a deliverable:** `canopy agent work <items.json>`.
- **Package / share this turn:**
  ```
  canopy agent turn --slug <slug> --title "<what this turn did>" \
    --session-id <claude-session-id> \       # REQUIRED — one of --session-id or --upload
    --task <ext_id> [--task <ext_id> …]      # the board task(s) this turn advanced
    # --work-product-url <url> per deliverable produced this turn
    # --upload   share the transcript instead of just naming the session: publishes a
    #            /share/<token> link (an outbound action; rides the same approval gate as a
    #            send). Use ONLY if the human asked to share — but pass it OR --session-id,
    #            never neither (the CLI errors "pass --session-id … or --upload").
  ```
Turn recency is no longer a readiness signal (`canopy agent health` reports it as info only, never
a flag). The board at `/agents/<slug>` stays the shared trigger + approval surface — where a human
queues work and approves outbound actions — independent of whether you publish above.

**CLOSE CHECKLIST — confirm each in the summary (these get silently skipped under load):**
0aa. **On a scoped turn, you checked you are not a DUPLICATE turn on that same ref** (Step 2 Scope)
   — one `ps` count. If another session was already live on it, say so and say what you stood down on.
0ab. **You checked for a live SIBLING turn of yours on a different ref too** (Step 2 Scope) — the
   wider `ps` count. If one was live, name it and say how you kept off its artifacts and its send.
0ac. **Both counts were RE-RUN immediately before the first write/send** (Step 2 Scope), not only at
   the top of the turn. A start-of-turn all-clear expires; if the turn stalled or ran long, say that
   you re-checked and what the re-check found.
0a. **On a scoped turn, that counterpart's other recent mail was swept before the action was
   decided** (Step 2 Scope) — one `from:<them> newer_than:3d` search. If you cannot point to it, you
   read one thread in isolation and the context that changes the answer is exactly what you skipped.
0b. **You checked the `To:` line before drafting any reply** (Step 2) — if the message addressed
   someone else and you were only cc'd, the default was silence, and a reply needed a confident,
   stated reason. "I had something useful to add" is not one.
0. **Every inbound item was DISPLAYED verbatim before it was worked on** (Step 2) — headers plus
   full body, not a summary. If you cannot point to where in this session you pasted it, you skipped
   it, and the human has been watching an opaque turn.
1. `<slug>:agent-turn-review` — your own wrapper, by full name — ran on every outbound reply
   (Step 2).
2. Skill-development self-check answered (Step 3).
3. Published to canopy-web (skills / work / turn) ONLY if the human asked — otherwise skip; none of
   it is an automatic close step.
4. **Summary is legible + decidable:** no bare internal ids (`T7`, run/session ids) as the name of
   anything; no invented human names (cite `@handle` unless you looked the name up); and every
   recommendation states the call, the default, and the cost.
5. **If you are ending on `⏸`, the thing you named is genuinely THEIRS** (Step 4, status line) —
   not work inside your own authority, and never "whether to continue". A pause that names your own
   next step is an unfinished turn wearing a status line.

**Shipping anything — the ship loop lives in `agent-core/shipping.md`.** Branch -> PR -> wait ->
merge -> verify it landed -> state the merge state. Read that file (via your `shipping` stub)
rather than improvising here; it carries the per-repo check table, the **backgrounded** wait (a
foreground `sleep` used to wait is blocked by the harness Bash contract), the ambiguous-`gh`-error
case, the merge-queue double false-negative, and the unconditional ship checkpoint.

The one turn-specific rule: **a turn that opened a PR does not close without a read merge state**
(`MERGED` / `OPEN` / `CLOSED`, from `gh pr view <n> --json state`) and the next planned action.
"Auto-merge armed" / "checks running" / "PR queued" is not a close.

## Related skills
- `<slug>:agent-turn-review` — YOUR wrapper, by full name — gate every outbound reply against the
  original request AND against what you can actually execute, before sending. It delegates to the
  fleet-wide `canopy:agent-turn-review`; invoke the wrapper, never the fleet skill directly.
- `shipping` — the ship loop (`agent-core/shipping.md` via your stub): per-repo check
  table, the backgrounded wait, and the merge-state checkpoint a turn cannot close without.
- `task-tracker` — durable multi-turn state (`agent-core/task-tracker.md` via your stub); drain
  board commands at turn start, package advanced tasks at close.
- `deliverables` — the fleet filing standard for Drive work products (`agent-core/deliverables.md`):
  per-project subfolder under your shared Projects root, never My Drive root, shared + confirmed.
  Your `gdoc-writer` stub implements it.
- canopy plugin (installed alongside every agent) — `create-agent`, `agent-publish`, `improve`, and
  the fleet self-improvement loop. Use them.
