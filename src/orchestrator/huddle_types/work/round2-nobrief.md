Huddle {{huddle}} — round 2 of 3 (roundtable). You are {{member}}; {{leader}} leads.

READ-ONLY TURN (same rules as round 1: no sends, replies, PRs or board writes; the one write is
filing your block as the close-out).

Every teammate's round-1 report:

{{reports}}

{{leader}}'s questions for you:

{{critique}}

Propose AT MOST 3 pieces of work, best first — fewer is better; zero is fine. Work can be yours
alone or JOINT: name the teammates in `with` and say exactly what you ask of each in
`ask_of_partners`. Every proposal must serve one of the priorities stated in round 1 (quote it
verbatim in `priority`) and live in a project (an existing one of yours or a teammate's, or
`"new": true`). The bar: {{principal}} would rather this happen than not, given it costs his
attention to approve and your turns to build. Answer {{leader}}'s questions in `critique_answers`.

```huddle
{"huddle": "{{huddle}}", "round": {{round}}, "member": "{{member}}",
 "proposals": [{"title": "≤10 words", "lead": "{{member}}", "with": ["teammate"],
                "priority": "<verbatim round-1 priority>", "project": {"name": "…", "new": false},
                "why": "evidence (links, ids)", "plan": ["step"], "effort": "S|M|L",
                "success_measure": "what we count, and the bar", "confidence": 0.0,
                "ask_of_partners": {"teammate": "what they would do"}}],
 "critique_answers": [{"title": "<question topic>", "answer": "…"}],
 "feedback": "what would make this huddle more useful"}
```

Put the block LAST in your final message, then file it as your close-out (block in a file):

    canopy agent turn --slug {{member}} --session-id "huddle:{{huddle}}:{{member}}:r{{round}}" --title "huddle {{huddle}} round {{round}}" --summary "$(cat <file>)"
