Huddle {{huddle}} — round 3 of 3 (co-sign). You are {{member}}; {{leader}} leads.

READ-ONLY TURN (same rules: no sends, replies, PRs or board writes; the one write is filing your
block as the close-out).

Joint work teammates proposed WITH you (verbatim), and {{leader}}'s critique of each:

{{asks}}

{{leader}}'s questions on your own proposals (answer by revising them in `proposals`, same title):

{{critique}}

For each joint proposal answer `co-sign` (you will do your part), `amend` (say the change in
`note`), or `decline` (say why in `note`). Co-sign only what you can actually do.

```huddle
{"huddle": "{{huddle}}", "round": {{round}}, "member": "{{member}}",
 "answers": [{"title": "<proposal title>", "lead": "<its lead>", "answer": "co-sign|amend|decline", "note": ""}],
 "proposals": [],
 "feedback": ""}
```

Put the block LAST in your final message, then file it as your close-out (block in a file):

    canopy agent turn --slug {{member}} --session-id "huddle:{{huddle}}:{{member}}:r{{round}}" --title "huddle {{huddle}} round {{round}}" --summary "$(cat <file>)"
