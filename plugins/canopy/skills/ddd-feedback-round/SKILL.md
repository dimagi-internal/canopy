---
name: ddd-feedback-round
description: Consume a closed storyboard feedback round: read every note, cluster, decide each note's disposition, draft and publish vN+1, then apply + consume (which opens the next round). Use when a turn's task says 'Run ddd-feedback-round for storyboard <slug> round <n>'.
---

# ddd-feedback-round

A closed storyboard round is the human input for this DDD iteration. Every note gets an answer.

1. **Read the round.** From the canopy repo root:
   `python -m scripts.ddd.feedback_round read <slug> <n> --out round.json`
2. **Read `clusters` and `decisions`.** Notes are grouped by `<target_ref>#<anchor_id or *>`. Decision rulings with status `human-decided` or `overridden` are BINDING.
3. **Decide each cluster.** Either change the narrative (`answered`, naming the scene that changed) or decline (`declined`, with a reason a reviewer can accept). Write `dispositions.json` as `{ "<note id>": {"state": "answered|declined", "note": "<one sentence>"} }` covering every note, no exceptions.
4. **Draft vN+1** through the normal `ddd-narrative-review` / render / upload path. Re-send decision rows with the ruled values (a 409 means you tried to move a ruling: supersede it instead and say why).
5. **Apply.**
   `python -m scripts.ddd.feedback_round apply <slug> <n> --dispositions dispositions.json --version <vN+1> --run-id <run>`
   It refuses (exit 2) before writing anything if any note lacks an answer; otherwise it resolves each note, records the round on the run doc, and consumes the round, which opens the next one.
6. **Report** the next round's number and the board link.

To publish a storyboard with decision rows in the first place: `python -m scripts.ddd.demo_set publish <board.json>`.
