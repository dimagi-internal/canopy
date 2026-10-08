Huddle {{huddle}} — round 3 of 3 (co-sign). You are {{member}}; {{leader}} leads.

{{brief}}READ-ONLY TURN (same rules: no sends, replies, PRs or board writes; the one write is filing your
block as the close-out).

## What you said earlier (verbatim)

{{earlier}}

## Joint work teammates proposed WITH you

Verbatim, each with {{leader}}'s critique. A proposal that `absorbed` others is {{leader}}'s merge
of duplicate proposals: you may be on it because one of yours was folded in.

{{asks}}

## Your own round-2 proposals

Verbatim, each with {{leader}}'s critique. To answer a critique, put the revised proposal — the
WHOLE proposal, same title — in `proposals`:

{{own}}

## {{leader}}'s other questions for you

{{critique}}

For each joint proposal answer `co-sign` (you will do your part), `amend` (say the change in
`note` — you and the lead then settle it directly in a short thread; if you both agree, that
counts as your co-sign of the agreed version, otherwise the proposal is held), or `decline` (say
why in `note`). Co-sign only what you can actually do, by the dates in its plan.

```huddle
{"huddle": "{{huddle}}", "round": {{round}}, "member": "{{member}}",
 "answers": [{"title": "<proposal title>", "lead": "<its lead>", "answer": "co-sign|amend|decline", "note": ""}],
 "proposals": [],
 "feedback": ""}
```

Put the block LAST in your final message, then file it as your close-out (block in a file):

    canopy agent turn --slug {{member}} --session-id "huddle:{{huddle}}:{{member}}:r{{round}}" --title "huddle {{huddle}} round {{round}}" --summary "$(cat <file>)"
