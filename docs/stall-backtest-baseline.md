# Stall-classifier backtest baseline

## ⚠️ CONTAMINATED — superseded by a re-run after fixes X, Y, and Z

The run recorded below was measured **before** two bugs in
`collect_handbacks` / `human_text` (`src/orchestrator/stall_backtest.py`)
were fixed. Do not treat the `0.330` overall precision figure, or any
per-class number below, as the real baseline — the class taxonomy and the
classify prompt cannot be judged against a corrupted answer key. A clean
re-run (`--hours 168 --show-errors`, no `--limit`) is needed to produce the
number this task is actually for; that re-run has NOT happened yet as of
this file being written (the coordinator will run it, not this task — see
"What happens next" below).

A THIRD bug (Fix Z, below) was found on the first attempt at that clean
re-run: it died partway through on a single flaky chunk, discarding ~20
minutes and ~40 successful model calls. That is now fixed too (retry +
skip-and-continue, never silent). The clean re-run still has not happened
as of this file being written.

**Fix X — duplicate-reply contamination in `collect_handbacks` (35% of the corpus).**
When several `end_turn` records handed back to a human in a row with no reply
in between, the old forward-scan attached the SAME next human reply to
*every* one of them. Measured corpus-wide: 337 of 962 handbacks (35%) shared
a reply with another handback. In the false-positive list from the run
below, 30 of 120 rows were duplicates — `"Go fix ace web as well, but not
labs yet"` appeared 6 times, `"okay great, finalize the loose ends..."` 4
times. One human decision was being scored as up to six independent cases,
which distorts every count derived from this data — precision, recall, and
the per-class breakdown all inherited the distortion. Fixed by making
`collect_handbacks` a single forward pass that tracks at most one *pending*
(unpaired) handback at a time: each new handback REPLACES whatever was
pending, so only the LAST handback immediately preceding a reply consumes
it; earlier handbacks in the same unanswered run are dropped, not
back-filled. Test: `test_consecutive_handbacks_share_the_reply_only_via_the_last_one`.

**Fix Y — skill/command payloads scored as human replies (~2% of the corpus).**
`human_text`'s harness-prefix filter caught `<command-message`, `Base
directory for this skill`, etc., but not a bare skill/command markdown body
landing in its own `user` record with no prefix of its own (the harness's
preamble line and the skill body are separate records). Measured: ~22 of 962
(2%). Two real examples confirmed against the live corpus:
`"# DDD Upload\n\nUploads a converged DDD run's artifacts..."` (the
ddd-upload skill body) and `"Run the labs walkthrough login script:\n\n\`\`\`bash..."`
(the ace:labs-login skill body). The second example has **no markdown
heading anywhere** — its YAML frontmatter and any heading were already
stripped before the body landed in the transcript as a plain `text` block —
so a heading+fence-only rule does not catch it; this was verified directly
against the raw transcript JSONL, not just the truncated snippet quoted in
chat. Fixed with a fourth, still-structural (position-of-syntax, not
content) signal in `_is_skill_or_command_payload`: a fenced code block
within the first 300 characters of the text, in addition to the three
originally specified (`# /` heading, `ARGUMENTS:` line, heading+fence
anywhere). Tests: `test_skill_and_command_payloads_are_not_human_replies`
(both real examples, verbatim).

**Fix Z — one flaky chunk killed the entire run (found on the first clean
re-run attempt, 626 handbacks / 21 chunks).** `classify_tails` correctly
raises `ValueError` when the model drops an item from a batch response
(`parse_batch_json` refuses to fabricate a verdict for it) — that fail-loud
contract is correct and was NOT weakened. The bug was in `grade`: an
uncaught `ValueError` from any one chunk propagated straight out of `grade`
and killed the whole run. At 10 chunks (the earlier 300-handback run) the
odds of hitting a flake stayed low enough to not show up; at 21 chunks they
caught up — one bad chunk discarded ~20 minutes and ~40 already-successful
model calls. Fixed in `grade()` only:
- `retries: int = 2` — each chunk's `classify` and `judge` calls are now
  retried independently (via `_call_with_retries`) on `ValueError`, up to
  `retries` additional attempts. Only the pass that actually failed is
  retried; a pass that already succeeded for that chunk is never redone.
- If a chunk still fails after retries, it is **skipped**, not
  fabricated and not fatal: the run continues with the remaining chunks,
  and results stay in input order.
- Any exception other than `ValueError` (e.g. `RuntimeError` from a broken
  `claude` subprocess) still propagates immediately and unretried — that
  means the model path itself is broken, which a retry cannot fix and
  swallowing would hide.
- `stats: dict | None = None` — when a dict is passed, `grade` populates
  `chunks`, `chunks_failed`, and `handbacks_skipped` so a caller can refuse
  to present a silently-incomplete measurement. `sessions_stall_backtest`
  now passes `stats` and prints a `WARNING: N of M chunks failed after
  retries — K handbacks excluded from this measurement.` line above the
  results whenever `chunks_failed > 0`, and includes the same three fields
  in `--json-output`. Left at the default `None`, every pre-existing caller
  (including every pre-existing test) is unaffected.
Tests: `test_grade_retries_a_chunk_that_fails_once_then_succeeds`,
`test_grade_skips_a_chunk_that_never_recovers_others_still_land`,
`test_grade_does_not_retry_or_swallow_a_runtime_error`,
`test_grade_default_stats_is_none_and_still_works`.

---

## Measured run (CONTAMINATED — pre-fix)

- **Date:** 2026-07-30
- **Model:** haiku
- **Command:** `uv run canopy sessions stall-backtest --hours 168 --limit 300 --show-errors`
- **Note:** this was a tracked/backgrounded run the coordinator re-ran after
  my own backgrounded run died when my subagent process exited (a
  backgrounded job does not outlive the subagent that started it). It graded
  300 of 962 handbacks found in the 168h window (newest-first), not the full
  962 — `--limit 300` was in effect.

```
Graded 300 handbacks from 105 sessions over the last 168h.
  Would auto-send: 179
  True positives:  59
  False positives: 120
  Precision:       0.330
  Recall:          0.590

Per-class:
  awaiting_continue    n=126   tp=46    fp=80    precision=0.365
  blocked_human        n=23    tp=0     fp=0     precision=0.000
  done_claimed         n=28    tp=7     fp=21    precision=0.250
  errored              n=19    tp=0     fp=0     precision=0.000
  gate_outbound        n=10    tp=0     fp=0     precision=0.000
  plan_pending         n=25    tp=6     fp=19    precision=0.240
  question_open        n=69    tp=0     fp=0     precision=0.000
```

(The three `0.000` rows — `blocked_human`, `errored`, `gate_outbound`,
`question_open` — are classes outside `AUTO_SEND_CLASSES`. They never fire a
would-send decision, so `tp=fp=0` and precision is undefined-as-zero, not a
failure of those classes.)

### Post-fix smoke runs (harness sanity check only — NOT a baseline)

To confirm the harness still runs end-to-end, one smoke run was made after
fixes X+Y landed, and another after fix Z landed (both `--limit 25`, both
below). Neither is a replacement baseline — `--limit 25` is far too small a
sample, and each draws from a different, later 24h window than the
contaminated run above and than each other, so none of these numbers are
comparable to one another or to the contaminated run. They only demonstrate
the classify/judge/grade pipeline still executes correctly, including (for
the second) that no chunk failed/retried so no `WARNING` line fired — this
sample was simply too small (1 chunk) to exercise the retry path itself.

**After fixes X+Y:**
- **Date:** 2026-07-30
- **Model:** haiku
- **Command:** `uv run canopy sessions stall-backtest --hours 24 --limit 25 --show-errors`

```
Graded 25 handbacks from 37 sessions over the last 24h.

  Would auto-send: 10
  True positives:  5
  False positives: 5
  Precision:       0.500
  Recall:          0.500

Per-class:
  awaiting_continue    n=6     tp=4     fp=2     precision=0.667
  blocked_human        n=1     tp=0     fp=0     precision=0.000
  done_claimed         n=4     tp=1     fp=3     precision=0.250
  errored              n=6     tp=0     fp=0     precision=0.000
  gate_outbound        n=1     tp=0     fp=0     precision=0.000
  question_open        n=7     tp=0     fp=0     precision=0.000

False positives (5) — a nudge would have talked over the human:
  [awaiting_continue] its not that I don't care about UCSF in general, I mean while on vacation
  [awaiting_continue] is there somethign we need to improve about the google write skills?  that sounds odd
  [done_claimed] clarify what you think is happening on 1039?
  [done_claimed] where are we on 1041 and should it be merged?
  [done_claimed] i got hte amazon notice that is turned on, want to flip it all on?  note that nothing should actually be sendign email yet (i didn't implement any features intentionally that use it)
```

**After fix Z (retry + skip-and-continue, plus the CLI's loud-warning wiring):**
- **Date:** 2026-07-30
- **Model:** haiku
- **Command:** `uv run canopy sessions stall-backtest --hours 24 --limit 25 --show-errors`

```
Graded 25 handbacks from 36 sessions over the last 24h.

  Would auto-send: 12
  True positives:  5
  False positives: 7
  Precision:       0.417
  Recall:          0.455

Per-class:
  awaiting_continue    n=9     tp=5     fp=4     precision=0.556
  blocked_human        n=1     tp=0     fp=0     precision=0.000
  done_claimed         n=3     tp=0     fp=3     precision=0.000
  errored              n=6     tp=0     fp=0     precision=0.000
  gate_outbound        n=2     tp=0     fp=0     precision=0.000
  question_open        n=4     tp=0     fp=0     precision=0.000

False positives (7) — a nudge would have talked over the human:
  [awaiting_continue] its not that I don't care about UCSF in general, I mean while on vacation
  [awaiting_continue] is there somethign we need to improve about the google write skills?  that sounds odd
  [awaiting_continue] I'm lost, do I need to do anything or can you fix it all?
  [awaiting_continue] yeah add the skill wrapper I think.  Note that I dont' about you to be a zealout about cli vs. skill, but use strong judgement on which goes where.  so in claude.md, make sure you instruct yourself acrrodingly.
  [done_claimed] clarify what you think is happening on 1039?
  [done_claimed] where are we on 1041 and should it be merged?
  [done_claimed] i got hte amazon notice that is turned on, want to flip it all on?  note that nothing should actually be sendign email yet (i didn't implement any features intentionally that use it)
```

`--json-output` confirmed to carry the new fields on this run:
`{'chunks': 1, 'chunks_failed': 0, 'handbacks_skipped': 0}`.

## Analysis (of the contaminated run above — read with that caveat)

**Which class contributes the most false positives?** `awaiting_continue` by
raw count (fp=80, more than the other two auto-send classes combined:
`done_claimed` fp=21 + `plan_pending` fp=19 = 40), but `plan_pending`
(0.240) and `done_claimed` (0.250) actually have *worse* precision than
`awaiting_continue` (0.365) — `awaiting_continue` just has far more volume
(n=126 vs. 28 and 25), so it dominates the false-positive count without
being the worst-calibrated class. All three auto-send classes are firmly
below any usable bar; none is a standout "mostly fine" class dragged down by
one bad sibling.

**Do the false positives share a property the classify prompt could be told
about?** From the `--show-errors` lists in both runs, yes, consistently: the
false positives are agent tails that state a next step or a claim of
completion, but whose actual next-turn semantics were a genuine question or
correction, not a confirmation. Examples from the smoke run: "clarify what
you think is happening on 1039?" and "where are we on 1041 and should it be
merged?" were classified `done_claimed`/`awaiting_continue`, yet both are
literally questions the human needed answered, not "keep going" moments.
This is exactly the `question_open`-scored-as-`awaiting_continue` (or
`done_claimed`) failure the module docstring calls out as the error this
backtest exists to catch — the classify prompt is reading a tail's
surface-level "I did X / next I'll do Y" phrasing as safe-to-continue
without weighing whether the tail also poses a question or asks for a
decision. The classify prompt could plausibly be tightened by making
`question_open` a stickier default whenever the tail contains an explicit
interrogative, and by warning it explicitly that "stated a next step" and
"asked a question" are not mutually exclusive in the same message.

**Is `awaiting_continue` being used as a catch-all?** By volume, yes:
126/300 = 42% of all classifications landed in `awaiting_continue`, more
than double any other class, and its precision (0.365) is not meaningfully
better than the other two auto-send classes (`done_claimed` 0.250,
`plan_pending` 0.240) — all three are in the same failing band, not "one bad
apple" pulled down by the others. That pattern — one class taking a
disproportionate share of the volume without a correspondingly better
precision — is the signature of a catch-all default rather than a
well-separated category the model is confidently choosing.

## What happens next

Per the task-6 brief's decision table: precision **0.330** overall is well
under the 70% floor, which would ordinarily mean "the class taxonomy itself
is probably wrong, revisit spec §3.2." But this number is **contaminated**
and must not be used to make that call — the duplicate-reply bug alone
inflated the false-positive count by counting some single human decisions
up to six times over. A clean `--hours 168 --show-errors` run (uncapped by
`--limit`) against the fixed `collect_handbacks`/`human_text` is the number
this decision should actually be made on.

The first attempt at that clean run (626 handbacks, 21 chunks) hit Fix Z's
bug instead — one flaky chunk killed the whole run — before it could
produce a number at all. With Fix Z in place the run should now either
complete cleanly, or complete with a loud `WARNING: N of M chunks failed
after retries — K handbacks excluded from this measurement.` line that
makes any partial-data risk visible rather than silent. Both the fixed
answer-key run and the fixed run-survives-a-flake behavior were explicitly
reserved for the coordinator to run, not repeated here.
