Huddle {{huddle}} — round 1 of 3 (report). You are {{member}}; {{leader}} leads this huddle.

A huddle is how this team works out what it can push forward for {{principal}} and Dimagi —
alone or together. Round 1: tell the team what you have been doing and what you understand the
priorities to be. In round 2 you will see EVERY teammate's report and propose work; in round 3
you co-sign (or not) work teammates propose with you.

THIS IS A READ-ONLY TURN. Do not run your turn procedure's inbox or board steps. No sends, no
replies, no PRs, no board writes. Read as much as you need. The one write allowed: filing your
reply block as this turn's close-out (end of this prompt).

SHARING: {{sharing_rule}}

1. Review your own last {{days}} days (since {{since}}): your turns, board, projects, repo, inbox
   threads, docs. {{leader}}'s survey of you (correct it if it is wrong):

{{context}}

2. What earlier huddles of this team already decided (do not re-raise a declined item without
   new evidence):

{{prior}}

Reply with exactly this block as the LAST thing in your final message:

```huddle
{"huddle": "{{huddle}}", "round": {{round}}, "member": "{{member}}",
 "worked_on": ["≤5 one-line items, each with a link or id"],
 "priorities": ["what you understand {{principal}}'s / Dimagi's priorities to be RIGHT NOW — each with where you learned it (thread, doc, goal, meeting)"],
 "projects": [{"name": "…", "state": "…", "next": "…"}],
 "offers": ["what you could do for a teammate"],
 "needs": ["what you need from a teammate or from {{principal}}"]}
```

Then file it as your close-out: write the fenced block (fences included) to a file and run

    canopy agent turn --slug {{member}} --session-id "huddle:{{huddle}}:{{member}}:r{{round}}" --title "huddle {{huddle}} round {{round}}" --summary "$(cat <file>)"
