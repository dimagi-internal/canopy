---
name: brain-preview
description: Show the person what canopy's brain WOULD do with their recent sessions, persisting nothing — every project link it would store (by project and tier, with evidence), whether it could read each session now vs if everyone in it agreed (with reasons), and the improvements its system learner would recommend, ranked with evidence linking to each session page. Steps up as the person present (passkey, read scope), pages canopy-web's brain preview, runs the learner step locally with the shared review-* framework, and renders ONE local HTML report. Writes nothing to canopy-web. Use for "brain preview", "what would the brain do with my last day", "dry-run the brain", "what would canopy learn from my sessions".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
```

# Brain preview — what the brain would do, nothing kept

The person wants to see the brain run over a window of their sessions (default: the last 24
hours) **without it keeping anything**: no link stored, no read recorded, no recommendation filed.
The split of work:

- **canopy-web** (`GET /api/brain/preview`) computes the links and the
  eligibility with the same functions the real brain uses, and writes nothing.
- **The CLI** (`canopy brain preview`, `canopy brain report`; `src/orchestrator/brain_cli.py`,
  `brain_report.py`) pages it, lays it out on disk, counts, and renders the HTML. Never recount or
  re-render in prose what it computes.
- **This skill** holds the judgment: the learner step — what canopy's system learner would
  recommend from each session.

**This is a dry run in the sense of `review-dry-run`** (dimagi/canopy-support): read as the real
run would, render what would be sent, send and record nothing. Every call is a GET. Do not file an
issue, open a PR, post a review, email or post anywhere; the report is a local file the person opens.

## 1. Read as the person (step up if you are an agent)

The preview answers a PERSON only — never an agent login, a turn token or MCP.

- **You are in the person's own session** (no agent; `canopy act-as` would say "this session
  belongs to no agent") — your login already is theirs. Skip to step 2 without `--step-up`.
- **You are an agent** (Ada, say) and the person is in the conversation — step up, read scope,
  following `canopy:act-as-you` exactly: ask without waiting, SAY the link as a message, then wait
  in the same turn.

  ```bash
  uv run --project "$CANOPY_ROOT" canopy act-as request --scope read --no-wait \
    --reason "Run the brain preview over your canopy sessions of the last 24h: read them as you (read-only) to show what the brain would link, read and recommend. Nothing is stored."
  ```

  Then tell them, as a message, before the next command:

  > To preview the brain over your last day of sessions I need to read them as you — read-only,
  > 30 minutes at most, nothing is stored. **[Tap to approve — it asks for your passkey](approve-url)**.
  > canopy-web logs every read as "<agent> as you".

  ```bash
  uv run --project "$CANOPY_ROOT" canopy act-as status --wait --timeout 240
  ```

  Declined / expired → stop and say what you could not do. Still pending → ask once more with
  `status --wait`, then stop. The person is NOT there (scheduled or emailed turn) → do not ask; say
  this needs them present and end.

## 2. Pull the preview

```bash
OUT="$(mktemp -d "${TMPDIR:-/tmp}/brain-preview-XXXX")"
uv run --project "$CANOPY_ROOT" canopy brain preview --since 24h --out "$OUT" --step-up   # drop --step-up in the person's own session
```

`--since` takes `24h` / `7d` / an ISO date-time (`--until` the same; default now, frozen for the
walk); `--view full` is the default and the widest (typed lines, replies, tool calls and results,
secret-scrubbed); narrower views are `conversation` (user + assistant text), `userinput` (only what
people typed) and `opening` (the first few typed lines). The learner step wants `full`. It writes `$OUT/<session id>.json` per session and `$OUT/index.json`, and prints
the counts. A 404 means this canopy-web has no preview yet — say so and stop. Run
`canopy act-as stop` as soon as the preview is pulled: everything after this is local.

Tell the person the counts line in one or two sentences before the learner step (it takes a while).

## 3. The learner step — what the system learner would recommend

Use the shared review framework, the source of truth — read it, do not copy it into this skill
or into subagent prompts beyond naming it:

1. `open_project dimagi/canopy-support`, then `project_skill` for **`review-read-session`**,
   **`review-signals`**, **`review-qualify`** and **`review-route`** (and `review-dry-run`).
2. The session files ARE the full read `review-read-session` asks for: read the transcript from
   `$OUT/<id>.json` (`transcript.messages`), never canopy-web and never `~/.claude/projects`. A
   transcript with `truncated: true` is read as far as it goes and the note says so.
3. **Only sessions the brain could read.** A session whose `would_read` says no for every class,
   both now and if everyone agreed (a hard fence: sealed, sensitive, an exclusion, a guest), is
   NOT read by the learner: write its note with `read: false` and the server's reason. A session
   the brain could read only if everyone agreed IS read — that is the point of the preview — and
   its findings say so in `why`.
4. **Fan out in batches.** One reader subagent per session, about 6 at a time, sent in one message
   per batch. Give each the session file path, the project-skill names to load (`open_project`
   dimagi/canopy-support, `project_skill` review-read-session / review-signals / review-qualify /
   review-route), the window, and this output contract. Each writes `$OUT/learner/<id>.json`:

   ```json
   {"session_id": "…", "read": true, "not_read": "",
    "findings": [{"title": "short, specific", "signal": "review-signals code, e.g. F3",
                  "route": "review-route destination (skill PR, defect issue, design proposal, …)",
                  "target": "repo / skill / surface it would change",
                  "severity": "high|medium|low", "proof": "proven|inferred",
                  "what": "the change the learner would recommend",
                  "why": "what it would fix, and what it cost here",
                  "evidence": [{"index": 12, "quote": "verbatim from the transcript"}]}]}
   ```

   Every finding passes `review-qualify` first (real quote, a signal, already-fixed check,
   repetition or cost; default is not-a-finding). Nothing is routed — `route` says where it
   WOULD go. Readers write files only, never canopy-web.
5. **Consolidate.** When every batch is back, merge findings that are the same change across
   sessions (one recommendation, every session's evidence under it, `session_id` on each evidence
   item), drop what `review-qualify` would drop on the cross-session view, rank (severity, proven
   before inferred, how many sessions show it, cost), and write `$OUT/recommendations.json` as a
   list in rank order. If you skip this, the report merges by exact title and ranks mechanically.

## 4. Render the report

```bash
uv run --project "$CANOPY_ROOT" canopy brain report "$OUT"
open "$OUT/report.html" 2>/dev/null || true
```

ONE self-contained local HTML file: summary counts; links it would store grouped by project and
tier with evidence; per session, would-read now vs if everyone agreed with the reasons; the
recommendations ranked, each with evidence linking to the session page on canopy-web
(`/w/<workspace>/sessions/<id>`). Give the person the absolute path and a three-line read of it
(the counts, the top recommendation, the biggest now-vs-agreed gap). Do not publish it anywhere
unless they ask — it quotes their sessions.
