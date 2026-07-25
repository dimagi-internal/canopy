# Manager sync — fleet-canonical process (canopy agent-core)

**Fleet-canonical process.** Your `skills/manager-sync/SKILL.md` stub carries your Identity
(name, `<slug>`, mailbox, who your advisor is, which gdoc/email tools you use) — apply this doc
bound to that identity. To change THIS process for the whole fleet, PR canopy
(`plugins/canopy/agent-core/manager-sync.md` + `canopy version bump`); agent-specific quirks go in
your stub's local-notes section.

A **manager sync** is a recurring, honest report to your advisor — the human whose scarce time
steers you. You report what you did since the last sync, **grade yourself harshly** on both the
work and how well you built reusable skills, and put a short list of already-decided next steps to
them to confirm or redirect. The sync is a first-class object on your canopy-web workspace
(`/agents/<slug>`), not a loose doc: it stores the doc link + a summary + your self-grades, and the
window state lives there — **not in a repo file.**

## The window (state lives on canopy-web)
- Read your last sync: `canopy agent syncs --slug <slug> --limit 1`. The window is that sync's
  `period_end` → today (`date +%Y-%m-%d`). First ever sync (empty list): from your project start.
- Do NOT keep `last_sync` in a repo file. The posted sync IS the record; a repo file drifts and
  splits state across the fleet.
- **An empty list means "no syncs", so sanity-check it before trusting a project-start window.**
  If your board or mailbox shows a sync you clearly sent, the window is wrong — don't re-report
  months of already-reported work. (A client bug once returned `[]` for every agent; the fix is
  in, but the cross-check is free and the failure is expensive.)

## Correcting a posted sync
- `canopy agent sync …` is **idempotent per (period_start, period_end, source)** — re-posting the
  SAME window overwrites it. So fixing a grade, summary, or doc URL is just posting again; it does
  NOT pile up duplicates.
- That also means a sync filed under the **wrong period** can't be corrected by re-posting — it
  lands as a second record. Remove it with `canopy agent sync-delete --slug <slug> --id <id>`
  (get the id from `canopy agent syncs`). Use it only for a wrong-period or stray row.

## Gather everything in the window
- **Completed work — from your board, the first-class source:** `canopy agent tasks --slug <slug>`.
  Every task `done` in the window is a completed item; its `score` + `review` were captured when it
  was marked done (see *Scoring at completion*). Tasks still open are your in-flight list.
- **Skills/code:** `git log --since=<period_end> --oneline` in your repo (skills built/changed, PRs).
- **Deliverables + comms:** your sent mail in the window; the gdocs/forms/decks produced; any backlog.
- Cross-check: work that shipped WITHOUT a board task is a tracking gap — name it in the sync and
  add the task now, so the next sync is complete by construction.

## Scoring at completion (not at sync time)
Grade each task **when you mark it done**, not weeks later at sync time:
`canopy agent set --slug <slug> --task-id <N> --status done --score <grade> --review "<one line>"`.
`score` is a short grade (`A-`, `B+`, `4/5`); `review` is the blunt one-liner. The sync then READS
those completion scores instead of re-grading from memory. If a task closed unscored, grade it in
the sync AND backfill it with the same `set` call so the board and the sync agree.

**The score needs its evidence tier and citation recorded WITH it** (see *Evidence rule*), because
at sync time you will no longer remember whether anyone actually used the thing — and the
reconstruction always flatters. Most work closes at `unproven`; that is fine and expected. When a
human later reviews or uses it, come back and re-score it upward with the citation — **the grade
is allowed to move as the evidence arrives, and a rising grade backed by a citation is the most
credible thing in the sync.**

## Write the sync — the structure
Grades are **merged into the items**, never a separate table. Sections, in order:

1. **Headline** — two or three lines: overall self-grades (work + skill-building) and the one thing
   that matters most this window. Blunt.
2. **Completed this window** — one entry per completed board task: **title (linked to the task /
   its deliverable) — score.** one-line review. Pull the score/review from the task's completion
   fields. **Each score carries its evidence tier and citation** (*Evidence rule*) so the reader
   can see what the grade rests on. Note any work that shipped untracked as an honest process gap.
3. **Open / in-flight** — a quick bulleted list of in-progress + queued tasks, each linked, with the
   next action. Don't grade these.
4. **Skills built / improved** — a bulleted list with **copyable source links** (the SKILL.md on
   GitHub), because people read the sync to reuse them. Add the honest skill-building ding here:
   foresight, or scar tissue from avoidable errors? what's still unbuilt?
5. **Questions / prototypes to share** — see *Asks discipline*. Concrete decisions to confirm, and
   any prototype/example worth a look.

## Harshness rule
Be your own toughest critic. **If your advisor could grade it lower than you did, you graded it
wrong.** Name failures specifically — the wrong recipient, the broken link, the doc no one could
open, the thing a human had to redo — not vaguely. Self-congratulation wastes their time.

**Being blocked on a human's review queue is not a work failure.** Many windows are gated on
someone finding time. Don't grade the window down for it, apologize for it, or call it "an infra
week." But don't grade it UP either — see the ceiling below. Blocked is neutral, not exculpatory.

## Evidence rule — a grade is a claim about CONSUMPTION, not about effort
**You cannot grade work a human has not yet used.** Producing an artifact earns no grade; a human
getting value from it does. So every grade must **cite the evidence that justifies it**, and the
strength of that evidence **caps** the grade:

| Tier | What you must be able to cite | Grade ceiling |
|---|---|---|
| `unproven` | nothing — it was produced and/or delivered, and no human has demonstrably opened it | **B−** |
| `acknowledged` | a human confirmed receipt or referenced that it exists | **B+** |
| `reviewed` | a human engaged with the CONTENT and gave substantive feedback | **A−** |
| `used` | a human used it for their goal (ran the interview from the guide, submitted the entry, published the piece, sent it onward) | **A** |
| `worked` | evidence of the outcome it was for (published, accepted, the partner replied, the number moved) | **A+** |

Rules that make this bite:

- **Cite it or don't claim it.** A citation names the **thread, the date, and what the human
  actually did or said** — not "positive feedback." If you cannot cite it, the tier is `unproven`.
- **`unproven` is the honest default,** and it is where most fresh work sits. "I worked hard on
  it," "it's high quality," "it's ready to run," and "I delivered it" are all `unproven`. So is
  your own confidence in it.
- **Grade the review's CONTENT, not the fact that a review happened.** If the feedback reset your
  premises or demanded a rewrite, that is evidence of a **miss** — it belongs below the `reviewed`
  ceiling, not at it.
- **Your skills grade is capped by the highest tier any of its OUTPUTS reached.** A skill is not
  good because it is well-written, well-factored, or newly built; it is good because what it
  produced served someone. A skill whose every output is `unproven` is **`Unproven`** — write that
  word, not a letter. **While you are still iterating and nothing has been consumed, you do not
  have a skills grade yet.** Say so.
- **Watch for the inversion.** Grades drift toward *your* effort and away from *their* use, which
  makes them run backwards: the elaborate un-read deliverable scores high, the plain one someone
  actually used scores low. When you list the window's grades, check that the highest ones sit on
  the highest-tier evidence. If they don't, you graded effort.

Evidence-based grading is symmetric — it raises grades too. When a human demonstrably *used*
something, say so and score it accordingly; that is the only kind of high grade worth reporting.

> Origin: Jonathan, 2026-07-23, on Echo's manager sync #3 — *"You can't possibly confidently rate
> your skills at A− … because we haven't yet really used the outputs you are working on yet you
> are in iteration mode. You should ALWAYS ALWAYS be skeptical of your skills until a human has
> really been able to consume the output and use it for their goal."* The audit that followed
> found the inversion above in that very window: the three deliverables with **zero** evidence of
> use were graded A−, while the one output a human actually used (an advisor submitting a drafted
> award entry herself) was graded B.

**Enforce this in your board, not just in prose.** Your task tracker's completion path should
refuse a score that has no evidence tier and refuse a grade above that tier's ceiling, so an
unearned A− is impossible rather than merely discouraged (Echo's `bin/echo_tasks.py set` is the
reference implementation). Prose you must remember loses to a rail that runs every time.

## Asks discipline (your advisor's scarce time)
Your advisor is the **advisor, not the requester**. Two rules:
- **Propose, don't poll.** If you can ask a question, you can propose an answer — so do. Every ask
  is a decision you've ALREADY made, stated with its alternative, for them to confirm or redirect.
  An open-ended question ("where should I focus?", "any feedback?") wastes their time: if you don't
  already know what you'd do with the answer, you have nothing to act on — so don't ask it.
- **Task-specific questions go to the requester, not the advisor.** A question about a particular
  deliverable — who owns a stat, which framing someone wants, whether a doc opened — belongs in that
  person's thread; route it there. What's left for the advisor is portfolio-level: where your hours
  go, how to sequence the big bets, and whether your self-grades are honestly calibrated.

## Pre-send review
The sync report (email/message to your advisor) is an outbound deliverable — run your
`agent-turn-review` gate on it before it goes: fidelity to this structure, grounded commitments
(every "next I'll…" names an executable mechanism), and presentation (lead with what you DID,
enumerate, link every skill as source, verify the recipient). Don't skip it because it's "just a
status update."

## Publish + record (this is the state)
1. Publish the sync as a shareable doc via your gdoc tool; verify it renders clean and is shared
   before the link goes out (no leaked markdown, no unshared doc — the same delivery hygiene you're
   grading yourself on).
2. Record it on canopy-web — this is what makes it first-class and what the NEXT window reads:
   `canopy agent sync --slug <slug> --doc-url <url> --title "<title>" --summary "<one-liner>"
   --grades '{"work":"<grade>","skills":"<grade>"}' --period-start <ISO> --period-end <ISO>`.
3. Refresh your skill catalog so the workspace mirrors reality: `canopy agent skills --slug <slug>
   --from-repo <skills-dir> --url-template <github-blob-url>` (your stub wraps this).
4. Draft the report to your advisor (gated send — a human approves it) leading with the headline and
   linking the doc + your **syncs feed** (`…/w/<workspace>/agents/<slug>/syncs`). Link the SECTION,
   not the bare `/agents/<slug>` — that redirects to the workspace default (`inbox`), not your
   syncs. Same for a board reference: link `…/agents/<slug>/tasks`.

## Related
`agent-turn-review` (per-deliverable pre-send gate; manager-sync is the periodic portfolio view),
`task-tracker` (the board this reads), `turn` (a sync is a kind of turn output).
