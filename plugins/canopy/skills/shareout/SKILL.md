---
name: shareout
description: Generate a teammate-facing work briefing for a date range (default = since the last shareout) — review your Claude Code sessions + your PRs per project, synthesize what shipped / why it matters / how teammates can leverage it, and post to the canopy-web /shareouts feed. Use when asked to "shareout", "what did I do yesterday", "briefing for the team", or "write up my week".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available — run `/canopy:update`." Then continue.

# Shareout

Turn "what I worked on over a date range" into a **teammate-facing briefing**: one
section per project (what changed, *why* it matters, how to leverage it) plus a short
cross-project roll-up. Publishes to the **/shareouts** feed on canopy-web.

This skill is autonomous by default — gather, synthesize, post, hand back the URL.
Only stop to ask if the range is genuinely ambiguous.

## Inputs

- Default range (no args): **from the end of the most recent existing shareout up to
  today** — i.e. the gap since the last one. If no shareout exists yet (or canopy-web
  is unreachable), it falls back to yesterday. The user may override with a range in
  natural language — map it to `--from/--to` (YYYY-MM-DD) or `--days N`.
- `--days N` = the last N full days ending yesterday.
- `--project <name>` = limit to one repo (matches resolved repo ending `/<name>`).

## Flow

### 1. Gather the corpus (deterministic)

Run against the canopy runtime (resolved via `scripts/canopy-runtime.sh`). Write the
corpus to a temp file so you can read it fully:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy shareout gather --json-out /tmp/shareout-corpus.json
# add --days 7 / --from 2026-06-01 --to 2026-06-03 / --project canopy as needed
```

This collects, per project that had activity in the window:
- **sessions**: your Claude Code prompts (the intent → the *why*), tool-usage summary, MCP servers
- **prs**: your GitHub PRs (via `gh --author @me`) created/merged/updated in the window — title, state, body

Read `/tmp/shareout-corpus.json`. If `projects` is empty, tell the user there was no
tracked activity in the range and stop.

### 2. Synthesize the briefings (you, the agent)

For **each project** in the corpus, write a briefing for a teammate who wasn't in the
room. Ground every claim in the corpus (prompts + PR titles/bodies) — do not invent work.

Each project briefing:
- **title**: a plain, specific headline (what shipped). Not marketing.
- **summary**: one-sentence TL;DR for the feed card.
- **content**: markdown, **three `## ` sections, each a short bulleted list** (not
  prose paragraphs — the feed renders each `## ` heading as a labeled, divider-separated
  block, so bullets read far more clearly than walls of text):
  - `## What shipped` — bullets, each a concrete shipped thing; bold the lead term, cite the PR (`- **X** — … (#123)`).
  - `## Why it matters` — 1–3 bullets: the problem / the decision (mine the prompts for intent).
  - `## How to leverage it` — bullets: what a teammate can now do, reuse, or build on; gotchas.
  Keep bullets to a line or two. Avoid multi-sentence paragraphs.
- **links**: the *highlight* PRs (`{"label": "PR #83 — title", "url": "..."}`), 2–4 most
  relevant first. The full PR list is attached automatically in step 3 (don't hand-copy it).

Then write **one roll-up** across all projects: the 2–4 threads that tie the period
together, what's worth a teammate's attention first. Keep it short.

Write an authoring doc to a temp file in this shape:

```json
{
  "period_start": "2026-06-03T09:00:00Z",
  "period_end": "2026-06-04T14:30:00Z",
  "author": "<your name, e.g. jjackson>",
  "rollup": { "title": "...", "summary": "...", "content": "## ...", "links": [] },
  "projects": [
    { "project_slug": "canopy", "title": "...", "summary": "...", "content": "## What shipped\n- **X** — … (#83)\n- …\n\n## Why it matters\n- …\n\n## How to leverage it\n- …", "links": [{"label": "PR #83 — ...", "url": "..."}] }
  ]
}
```

- `period_start`/`period_end` are **ISO timestamps** — copy them verbatim from the
  corpus `period` (don't truncate to dates; the window is precise to the second so
  consecutive shareouts chain exactly).
- `project_slug` must be a **canopy-web project slug**. Check available slugs:
  `curl -s -H "Authorization: Bearer $(cat ~/.claude/canopy/workbench-token)" "https://labs.connect.dimagi.com/canopy/api/projects/slugs/" | python3 -c "import sys,json;[print(p['slug']) for p in json.load(sys.stdin)]"`
  Map each corpus repo (e.g. `dimagi-internal/canopy`) to the matching slug (e.g. `canopy`).
  **Skip a project** (leave it out of the doc) if no slug matches — the server would
  skip it anyway.

### 3. Post

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy shareout post /tmp/shareout-authoring.json --corpus /tmp/shareout-corpus.json
```

`--corpus` auto-fills each project's full PR list (`all_prs`, rendered as a collapsed
"All N PRs" expander on the feed) by matching repo basename → project_slug — so you
only author the highlight `links`, not every PR. Print the returned `View: …/shareouts` URL.

**Running on behalf of the user (agent-aware).** If you are an **agent** (Eva, Ada, Hal,
…) running this report-out *for* your human, the report is still **about the human's
work** — it stays attributed to `author` (their name) and is posted with their PAT. That
is correct; do not attribute it to yourself. Just record that *you* assembled it by
passing your own slug:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy shareout post /tmp/shareout-authoring.json --corpus /tmp/shareout-corpus.json \
  --produced-by-agent <your-slug>   # e.g. eva — read it from your repo's config/agent.json
```

The feed then shows a subtle "· produced by <you>" byline while the briefing remains the
human's. Pass the flag explicitly: don't rely on the CLI auto-detecting your identity from the
working directory (it only falls back to `$CANOPY_AGENT_SLUG` or a `config/agent.json`
in `cwd`, and the cwd this runs from isn't guaranteed to be your agent repo). The reusable rule, for
any future "agent uploads on behalf of the user" action: **the author is the subject, the
producer is the agent, the auth is the subject's PAT.** A human running this themselves
omits the flag entirely.

**Idempotency / re-running:** by default each run stamps a fresh timestamped `source`,
so posting again **appends** a new set (different days never collide — they're different
periods). To **replace** a prior run for the *same* period (e.g. fixing a same-day typo),
first read that run's `source` from the GET endpoint
(`/api/shareouts/?date_from=<d>&date_to=<d>`) and pass it back via `--source "<that value>"`
— rows are keyed on `project+period+source`, so reusing the source overwrites in place
instead of duplicating.

## Rules

- **Ground everything in the corpus.** Prompts reveal intent (the "why"); PR
  titles/bodies reveal what shipped. No invented features, no filler.
- **Teammate-facing, not a changelog.** Lead with why it matters and how to leverage it.
- **Honest.** If a day was small or exploratory, say so briefly — don't inflate.
- Slugs that don't exist on canopy-web are skipped; mention any skipped repos to the user.

## When NOT to use

- For the internal strategic brief over orchestrator state, use `canopy:brief`.
- For short categorized portfolio one-liners, use `canopy:portfolio-review` (/insights).
