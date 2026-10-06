Huddle {{huddle}} — round 3 of 3 (co-sign). You are {{member}}; {{leader}} leads.

READ-ONLY TURN (same rules: no sends, replies, PRs or board writes; the one write is filing your
block as the close-out).

Joint work teammates proposed WITH you (verbatim), and {{leader}}'s critique of each:

{{asks}}

Your own round-2 proposals (verbatim), each followed by {{leader}}'s critique of it. To answer a
critique, put the revised proposal — the WHOLE proposal, same title — in `proposals`:

{{own}}

{{leader}}'s other questions for you:

{{critique}}

For each joint proposal answer `co-sign` (you will do your part), `amend` (say the change in
`note` — the lead then accepts it, which counts as your co-sign of the revised proposal, or
rejects it, which holds the proposal), or `decline` (say why in `note`). Co-sign only what you
can actually do.

```huddle
{"huddle": "{{huddle}}", "round": {{round}}, "member": "{{member}}",
 "answers": [{"title": "<proposal title>", "lead": "<its lead>", "answer": "co-sign|amend|decline", "note": ""}],
 "proposals": [],
 "feedback": ""}
```

Put the block LAST in your final message, then file it as your close-out (block in a file):

    canopy agent turn --slug {{member}} --session-id "huddle:{{huddle}}:{{member}}:r{{round}}" --title "huddle {{huddle}} round {{round}}" --summary "$(cat <file>)"
