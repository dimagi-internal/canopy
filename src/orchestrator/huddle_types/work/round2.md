Huddle {{huddle}} — round 2 of 3 (roundtable). You are {{member}}; {{leader}} leads.

{{brief}}READ-ONLY TURN (same rules as round 1: no sends, replies, PRs or board writes; the one write is
filing your block as the close-out). Today is {{today}}.

Every teammate's round-1 report (yours included):

{{reports}}

{{leader}}'s questions for you:

{{critique}}

Propose AT MOST 3 pieces of work, best first — fewer is better; zero is fine. The bar:
{{principal}} would rather this happen than not, given it costs his attention to approve and your
turns to build. Each proposal:
- serves ONE priority in the brief — its NUMBER in `priority` — and is not something the brief's
  `Not now:` line rules out (if it is, drop it, however useful);
- moves it in a way that would not happen without this huddle. `kind`: `new` (nobody is doing
  it), `unblock` (frees something stuck) or `existing` (an open task — only with `why_huddle`:
  what the huddle adds, e.g. a partner; your own open task re-wrapped is not a proposal);
- is JOINT when a teammate's report shows they hold part of it: name them in `with` and say what
  you ask of each in `ask_of_partners`. If a teammate's lever is the same work, propose it WITH
  them — the leader merges duplicates before round 3;
- has dated steps in `plan` ("YYYY-MM-DD: step", the first no earlier than {{today}}). If it
  cannot land before a hard date in the brief, set `"too_late": true` and say so in `why` — or
  drop it;
- marks every claim in `why` "(checked)" (you verified it this turn) or "(unchecked)";
- says what it costs {{principal}} in `cost_to_jonathan` — `none`, `yes` (one approval),
  `decision` (a choice only he can make — say which) or `time` (how many minutes) — and in `fails_if`
  the one thing most likely to make it fail;
- lives in a project (an existing one of yours or a teammate's, or `"new": true`).
Answer {{leader}}'s questions in `critique_answers`.

```huddle
{"huddle": "{{huddle}}", "round": {{round}}, "member": "{{member}}",
 "proposals": [{"title": "≤10 words", "lead": "{{member}}", "with": ["teammate"],
                "priority": 1, "kind": "new|unblock|existing", "why_huddle": "<only for kind existing>",
                "project": {"name": "…", "new": false},
                "why": "evidence (links, ids), each claim (checked) or (unchecked)",
                "plan": ["YYYY-MM-DD: step"], "too_late": false, "effort": "S|M|L",
                "success_measure": "what we count, and the bar",
                "cost_to_jonathan": {"kind": "none|yes|decision|time", "detail": "what exactly / how many minutes"},
                "fails_if": "one line",
                "ask_of_partners": {"teammate": "what they would do"}}],
 "critique_answers": [{"title": "<question topic>", "answer": "…"}],
 "feedback": "what would make this huddle more useful"}
```

Put the block LAST in your final message, then file it as your close-out (block in a file):

    canopy agent turn --slug {{member}} --session-id "huddle:{{huddle}}:{{member}}:r{{round}}" --title "huddle {{huddle}} round {{round}}" --summary "$(cat <file>)"
