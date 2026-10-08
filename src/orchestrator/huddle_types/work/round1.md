Huddle {{huddle}} — round 1 of 3 (report). You are {{member}}; {{leader}} leads this huddle.

{{brief}}A huddle is how this team works out what it can push forward for {{principal}} and Dimagi —
alone or together. The priorities above are given: work toward them, do not re-derive or re-rank
them. Round 1: tell the team where YOU can move them. In round 2 you will see EVERY teammate's
report and propose work; in round 3 you co-sign (or not) work teammates propose with you.

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

3. For each priority in the brief, name your LEVER: the most valuable thing you could do toward
   it in the next 2 weeks (today is {{today}}). At most one per priority; skip a priority you
   cannot move. `kind`: `new` (nobody is doing it), `unblock` (frees something stuck — name the
   task) or `existing` (a task already open — name it). `verified`: true only if you checked the
   facts it rests on THIS turn. Something that cannot land before a hard date in the brief is
   not a lever.

Reply with exactly this block as the LAST thing in your final message (`priority` is the
brief's number):

```huddle
{"huddle": "{{huddle}}", "round": {{round}}, "member": "{{member}}",
 "state": ["≤3 one-line items: what you are mid-way through that matters to the brief, with ids"],
 "levers": [{"priority": 1, "move": "the most valuable thing you could do toward it in the next 2 weeks",
             "kind": "new|unblock|existing", "task": "<task id for unblock/existing, else ''>",
             "blocked_by": "<who or what, or ''>", "verified": false}],
 "offers": ["what you could do for a teammate"],
 "needs": [{"from": "<teammate slug, or {{principal_key}}>", "ask": "exactly what you need"}]}
```

Then file it as your close-out: write the fenced block (fences included) to a file and run

    canopy agent turn --slug {{member}} --session-id "huddle:{{huddle}}:{{member}}:r{{round}}" --title "huddle {{huddle}} round {{round}}" --summary "$(cat <file>)"
