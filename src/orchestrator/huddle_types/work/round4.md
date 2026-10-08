Huddle {{huddle}} — round 4 (resolve). You are {{member}}; {{leader}} leads.

{{brief}}READ-ONLY TURN (same rules as before: no sends, replies, PRs or board writes; the one write is
filing your block as the close-out).

## What you said earlier (verbatim)

{{earlier}}

## Amends to resolve

In round 3 a teammate answered `amend` on joint work you lead (or proposed). An amend is
neither a yes nor a no: until you resolve it the proposal is HELD and never reaches the board.
Each proposal below is quoted verbatim, followed by every amend on it — who sent it and their
note, verbatim:

{{resolve}}

For EACH proposal answer one of:
- `accept` — fold the amendment(s) in. Put the WHOLE revised proposal, same title and lead and
  the same fields as above, in `proposal`. The amending teammate's answer then counts as a
  co-sign of the revised version, so the revision must actually carry what they asked for.
- `reject` — keep the original; say why in `note`. The proposal stays held (the amending
  teammate never agreed to the original), and the record says you rejected the change.

```huddle
{"huddle": "{{huddle}}", "round": {{round}}, "member": "{{member}}",
 "resolutions": [{"title": "<proposal title>", "lead": "<its lead>", "resolution": "accept|reject", "note": "",
                  "proposal": {"title": "<same title>", "lead": "<same lead>", "…": "every other field of the proposal, revised"}}]}
```

(`proposal` is required with `accept`; leave it out with `reject`.)

Put the block LAST in your final message, then file it as your close-out (block in a file):

    canopy agent turn --slug {{member}} --session-id "huddle:{{huddle}}:{{member}}:r{{round}}" --title "huddle {{huddle}} round {{round}}" --summary "$(cat <file>)"
