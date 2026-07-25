---
name: agent-turn-review
description: >
  The fleet-wide pre-send review every agent runs before ANY outbound deliverable / reply / PR.
  Audits the draft against the ORIGINAL request (fidelity) AND against your own capabilities
  (grounded commitments): extract each discrete ask, confirm the draft does exactly that, and
  confirm every "I'll do X" is something you can actually execute — and every "I did X" actually
  happened (verify done-claims, don't assert them). Fix gaps before sending.
  Agents keep a thin `agent-turn-review` skill that invokes this and adds their own send-path +
  paired-reviewer specifics — the general discipline lives here so the fleet stays DRY.
---

# Agent turn review — does the deliverable match the brief, AND can you do what you claim?

The canonical fleet self-review. Run before EVERY outbound action (it is the thing that gets
dropped under load). Agent repos keep a thin `agent-turn-review` extension that invokes this and
adds agent-specifics (send path, paired reviewers); the general discipline lives here once.

## A. Fidelity — does the work match what was asked?
1. **Re-read the original request** — the actual message, not your memory of it.
2. **Extract EACH discrete ask** as a numbered checklist, verbatim where possible. Multi-part
   requests hide dropped items — list them ALL.
3. **For each ask, confirm the draft does EXACTLY that** — not a near-miss. Read any source/link
   they cited (open it; don't reconstruct from memory); never substitute your own summary for the
   thing they asked for (the classic: linking your scan when they asked for the report).
4. **Rate it, tough** — faithfulness-to-each-ask / source-verification / completeness / clarity,
   1–5 each, default 3. Anything under 5 on faithfulness → fix before sending.

## B. Grounded commitments — can you actually do, AND have you actually done, what you're claiming?
5. **Scan the deliverable for every forward-looking claim** — "I'll X", "next I'll Y", "before Z
   I'll…". For EACH, name the concrete, executable mechanism (a command, a tool, a specific step)
   and confirm you can actually perform it — this turn or as a real, nameable next action. No
   mechanism and no rough WHEN → it's not a commitment, it's vapor; cut it.
5a. **Past-tense DONE-claims get the same test as forward-looking ones — verify, don't assert.**
   "I did X", "I've already X'd", "X is done / handled / fixed", "I updated the skill", "I've noted
   that" — each asserts COMPLETED work, and each must be checked against evidence (the file diff, the
   command output, the merged PR, the live link) BEFORE it ships, exactly as you'd ground an "I'll
   X". A done-claim is *more* dangerous than a promise: it reads as finished, so no one follows up
   and the gap ships silently. If you cannot point to the evidence right now, either DO it before
   sending or cut the claim. Origin: 2026-07-21, an agent wrote "I've noted that in the skill" in a
   turn-back when it had made no such edit — true only as an intention, and it would have shipped
   unnoticed had the human not asked. (This is §13's "evidence before assertion" applied to the
   deliverable's own claims, not just the review's.)
6. **Vague coordination verbs about a PERSON are the red flag** — "sync with / coordinate with /
   loop in / check with / align with / run it by <someone>". They almost always hide a human
   dependency you have NO channel to execute, dressed up as a plan. Either **(a) convert it to
   something you can run** — the specific dedup check (open PRs + branches touching the exact files
   you'll change), or an explicit **draft-then-ask** message you will actually send via your
   sanctioned send path — or **(b) cut the claim.** Never promise a human-in-the-loop step you
   have no way to perform.
   - Running the concrete check often **CORRECTS a false premise** — grounding a claim surfaces
     facts, not just honesty. Origin: an agent wrote "I'll sync with Sarvesh, audit's a hot area";
     the check showed the feature lived in a different repo with zero in-flight PRs — wrong repo
     AND no conflict. (Jonathan, 2026-07-08.)

## C. Presentation
7. **Lead with what you DID** + your recommendation + what else we could do — not junior questions.
8. **Enumerate multiple asks**, one line each, showing how each was handled (✓ done / link / status).
9. **Substantial artifacts go in a shared doc the reply links** — never a wall of pasted text,
   never a loose local file; present the reply body itself inline for approval. **The final body
   must sit in the SAME message as the "good to send?" ask, every time you ask — including re-asks
   after a revision or a tangent.** Before you post an approval/pause line, confirm the current full
   email body is right there in the message; if it's only linked or was shown earlier, paste it
   again. The human should never have to say "show me the email" to approve.
10. **Verify recipients** from the structured reader / `--reply-all`, never a raw text mail view
    (it hides `Cc:`). Confirm reply-all vs. direct on purpose.
10a. **Strip session-internal framing — write the copy COLD, as the recipient reads it.** Your
    outbound goes to people who have ONLY the thread's context, not the in-session exchange with the
    operator who steered this turn. So any phrasing that only makes sense relative to that private
    exchange is a leak: agreeing with a correction the recipient never saw ("you're right", "good
    call", "as you noted"), announcing a "fix" to a draft they never received ("fixed on both
    counts", "the corrected version"), or narrating your own process ("after I regressed to inline
    here", "per your steer"). Reread every sentence asking *"would this parse for someone who only
    read the thread?"* — if it references a state or a remark outside the thread, cut it or restate
    it as a plain fact ("drafted to send from Neal", not "you're right, so it's Neal's note"). This
    also applies to the "How I improved this turn" bullets: state what changed as a capability, not
    relative to a mistake the recipient didn't witness. (Origin: 2026-07-23 — a reply carried "you're
    right, Neal asked, so it's his note" and "fixed on both counts," both answering the operator's
    in-session correction, into an email whose recipients had never seen that exchange; the operator
    flagged it as a turn-review miss.)
10b. **Politeness is fine; manufactured value is not — never attribute a benefit, feeling, or worth
    you can't back up.** Thanking, welcoming, and acknowledging are allowed and good. What is banned
    is dressing a courtesy up as a substantive claim the agent has no basis to assert. Two forms,
    both banned: **(i) unbackable benefit** — *"it's genuinely useful to know you're a message
    away"*, *"this will be a huge help"*, *"great to have you onboard"*, *"your input has been
    invaluable"*; **(ii) effusive emotion / flattery** — *"that genuinely means a lot coming from
    you"*, *"I'm so grateful for the careful reviews throughout"*, *"I'm honored"*, *"that's the best
    example"*. An agent has no feelings to be moved and no standing to flatter, and it cannot vouch
    for a value it hasn't observed — so these read as performed warmth, not substance. Keep the
    courtesy **plain and objective** — *"thank you for offering it."* / *"thank you for the careful
    review."* full stop — and let a value, benefit, or praise statement stand ONLY where it's
    grounded in something specific you actually observed (*"your note caught a line that over-read the
    data"* — you can point to the line; *"the field definitions let us wire the indicator directly"*
    — you read them). Grounded-and-specific is fine; effusive-and-general is filler. This is the
    presentation-twin of §B/§5a grounding: same rule — don't assert what you can't substantiate —
    applied to warmth rather than to commitments and done-claims. (Origin: Jon, 2026-07-24 — TWO
    same-day ACE replies: one padded a call-decline with "it's genuinely useful to know you're a
    message away," another opened to a reviewer with "that genuinely means a lot coming from you …
    Grateful for the careful reviews throughout." Both passed the rest of the review because no lens
    tested for unbackable warmth; the fix in each was to cut to the plain thank-you.)

## D. Revision check — full re-review + repetition pass, EVERY revision

> **§D is enforced for email, because it is the step that fails.** `canopy email send` blocks any
> body without a review receipt fingerprinted to THAT body, so a review of v1 cannot satisfy a send
> of v3 — revise, and the receipt stops matching. Record yours after reviewing:
> `canopy email review-receipt --repo . --body-file <the body you'll send> --caught "<findings>"`.
> Dry-runs are exempt. The `caught` list is the fleet's evidence about which reviews earn their
> keep — an honest "none" is fine, but only per §13 (after you have actually read it back).
>
> **§B is enforced the same way — the receipt REFUSES to issue while any commitment-class
> phrase is unruled.** `review-receipt` scans the body for offers and human-dependencies
> ("happy to", "walk you through", "hop on a call", "sync with", "loop in", "in person"),
> prints every hit with its context, and blocks until you rule each one:
> `--commitment "<substring>=grounded:<mechanism>"` or `--commitment "<substring>=cut"`.
> GROUNDED for an agent = re-render, reply on the thread, open a PR, produce a doc.
> NOT grounded = anything needing you to be a person in real time. Why a gate and not a
> line of prose: on 2026-07-23 a review ran, caught three real body defects, recorded
> clean — and still shipped *"Happy to walk anyone through it live"*, a session the agent
> cannot hold. The RULE (§6) was already written; what failed was applying it to every
> instance, and completeness is exactly what prose cannot enforce. So the tool enumerates
> and the send stays blocked until each is ruled — you cannot skip the sign-off line.
11. **Re-run this whole review on every revision of a draft, not only the first.** A "delta check"
    of just your latest edits is how edit-introduced defects ship — the requester's corrections
    change the draft's context, so the whole thing gets re-reviewed.
12. **Then read the FULL draft top to bottom with fresh eyes for repetition:** the same phrase or
    fragment twice ("proposal… My proposal"), the same content announced twice (intro names the
    extras AND a later section re-introduces them), the same term leaned on 3+ times. Fix by
    varying or cutting — one announcement, one detail pass, per fact.
13. **Evidence before assertion — quote, don't claim.** This pass is trivially faked as a checklist:
    reporting "collapsed X, de-duped Y" without doing the read is how the misses survive. Quote each
    repetition you find verbatim; "none found" is valid only after you have read the final paragraph
    back to yourself. (Origin: an agent reported its repetition fixes done, then the requester
    immediately caught four it had missed — Jonathan, 2026-07-13.)

## E. Counterpart framing — how the reply LANDS with an external counterpart
Run this whenever the recipient is an external counterpart (a partner, funder, or client),
especially the decision-maker, and especially on a group thread. §A–D can all pass while the reply
still lands wrong on the person — this is the lens that catches that.
14. **Don't re-litigate the counterpart's own claims — translate, don't audit.** If you researched
    a figure or fact THEY supplied, present it as translating to the decision in front of us ("what
    that means for the rural villages we'd actually build for"), sourced and collaborative ("points
    the same way you did") — never as correcting them ("your 50% is really the male rate").
    Reframing a decision-maker's own number in front of their team reads as a gotcha, however right
    you are. Verify quietly, then apply it to our case.
15. **Sourced, not asserted, for external facts.** For any external factual claim, write "I went
    looking for public data on this and here's what I found — X (link); the sources vary by year and
    definition" — NOT "X is the number." Link the source, name the uncertainty, and defer to the
    counterpart's own field data as better than any public source.
16. **Own initiative honestly.** Describe extra work you chose to do as your own idea, in your own
    voice — never imply you were asked or assigned to do something you weren't (and never that you
    weren't, when you were). (Origin: 2026-07-22, a Spark reply passed §A–D three times while the
    human had to catch, on each revision, that it audited the partner's own figures, asserted
    contested numbers as fact, and implied assigned research — none of which the
    fidelity/grounding/presentation/repetition lenses test for.)

## Adopting it in an agent (the thin extension)
Keep a repo-local `skills/agent-turn-review/SKILL.md` that (a) says "invoke `canopy:agent-turn-review`
and apply it", and (b) lists only agent-specifics: the sanctioned send path (§B's draft-then-ask
target), where the turn gates it, and any paired reviewer (e.g. a `story-review`). The factory
(`canopy create-agent`) scaffolds this thin extension by default.
