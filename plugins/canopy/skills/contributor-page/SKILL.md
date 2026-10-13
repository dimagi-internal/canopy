---
name: contributor-page
description: Write or refresh a person's contributor page on an agent's canopy-web project — what they are trying to achieve, in their own words with a link to where they said each thing, and which of the project's ideas (tasks with kind "idea") are theirs, built versus open. Use when asked to "write <person>'s contributor page", "refresh the contributor pages on <project>", "what is <person> after on <project>", or before building on (or against) someone's idea.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
```

# Contributor page

When several people build one product with one shared agent, each idea has to
say whose it was, and each person needs a page that says what they are after.
canopy-web holds both:

- **Ideas** are board tasks with `kind: "idea"`. Every other task is
  `kind: "work"`: agent work, never shown as an idea. An idea must carry at
  least one `human_why` quote, a person's own words, and the server rejects one
  without. Work toward an idea is that idea's subtasks (`parent`). canopy-web
  derives each idea's stage (proposed / started / shipped) and who it came from.
  You do not maintain a second list. (Jonathan, 2026-10-12: ideas and tasks must
  not be conflated. Before this, every task showed as an idea, and an agent's
  process work read as people's product ideas.)
- **The contributor page** is the part that takes judgment: which of someone's
  hundreds of prompts say what they want. **You write it and canopy-web renders
  it** at `/w/<ws>/agents/<agent>/projects/<P>/people/<person>`.

The server enforces what can be enforced. It rejects a quote without a link, an
idea that points at a task outside the project, and a page the person has not
allowed (next section). Everything below is the judgment it cannot make.

## 0. Is it allowed? Ask first.

A contributor page is something an agent learned about a person, so it follows
that person's own memory switches:

- **Writing** needs their "Learn from me" (record) switch on, and their grant to
  this agent.
- **Reading** (as an agent) needs "Use my memory" (use) on, plus the grant.
- Humans in the workspace always see the page.

Read the page first:

```
GET /api/agents/<agent>/projects/<P>/contributors/<email-or-id>/
```

The MCP tool is `contributor_page`. If `may_write` is false, **stop**. Report
`why_not` to whoever asked. Do not route around it by putting the same content
into a task note, a doc or a fact. The person changes their switches on
`/people/me`; you never do it for them, and you never ask them to in an
unsolicited message.

`summary_state: withheld` means the person has not let agents use what they
learned. Do not build on their page's content in that case. Their ideas on the
board stay readable.

## 1. Gather only their words

- **Project-history bundle (preferred).** If the project already has one
  (canopy `project-history` skill), read the person's prompts from it:
  `uv run --project "$CANOPY_ROOT" canopy project-history read --bundle bundle.json`.
  Only prompts tagged `person` count. Dispatch and handoff prompts are agents
  talking.
- **No bundle.** Walk their sessions:
  1. `search_sessions` (filter by `repo`, `q`, `since`).
  2. `list_human_inputs` per session.
- **Other places they spoke.** Add doc comments, email threads and task
  `human_why` quotes.

Run from the requesting agent's repo root, so the canopy-web identity is that
agent's.

## 2. Judge

Write a JSON body:

```json
{"goal": "2-4 sentences: what they are trying to achieve on THIS project, built from their words.",
 "quotes": [{"quote": "verbatim", "source_url": "https://canopy.dimagi.com/w/<ws>/chat/<session>",
             "said_at": "2026-09-27T14:02:00Z", "where": "Supply session, Sep 27"}],
 "ideas": [{"task": "T71", "quote": "their words about this idea", "source_url": "…", "said_at": "…"}],
 "open":  [{"text": "a want no task covers yet", "quote": "…", "source_url": "…"}],
 "sources": [{"label": "Supply two-pager", "url": "…"}],
 "basis": "458 prompts across 22 sessions, Sep 10 to Oct 9"}
```

**Rules:**

- **Quotes are verbatim.** Trim to the sentence that carries the point. Never
  correct their spelling and never paraphrase inside quotation marks. Choose
  6–10 quotes that a teammate would need before building on their work. Prefer
  the later view where they changed their mind.
- **`goal` is the only place your words appear.** Keep it short, and keep it
  recognisably theirs. A goal they would not sign is a wrong page.
- **`ideas` claims an idea task as theirs** (`kind: "idea"`; the server refuses
  a claim on a work task). Do it only when their words asked for it,
  shaped it, or decided it. Do not claim work the agent originated that they
  merely approved with "go". Each claim should carry the words that make it
  theirs. canopy-web derives the stage from the task, so don't restate it.
- **`open` holds wants nobody has put on the board yet.** It is the useful
  list for a teammate looking for something to pick up. If one of them clearly
  should be on the board, file it as a `kind: "idea"` task carrying their quote
  as `human_why`, and claim it in `ideas`. Never file your own process work
  (reviews, docs, outreach) as an idea.
- **Before filing an idea, check whether it already shipped.** A quote's date is
  not its status: people often ask for something in the same session that
  builds it. Search the owning repo's merged PRs (`gh pr list --state merged
  --search …`) and look at the live product. If it shipped, file it `done` with
  the PR links; if part of it shipped, file it `in_progress` with the links and
  ask the person whether the rest is still wanted. *(2026-10-12: hal P5's
  "per-programme and per-organisation Labs pages" was filed as proposed. It had
  shipped on 2026-10-09, the day it was said, and its owner had to ask why.)*
- **Links.** Every quote links to where they said it. A canopy session link is
  `https://<canopy>/w/<ws>/chat/<session-id>`.
- **Privacy.** Leave out anything personal, health-related or performance-related.
  The page is work context, the same rule as facts.

## 3. Write it, then read it back

```
PUT /api/agents/<agent>/projects/<P>/contributors/<email-or-id>/summary/
```

The MCP tool is `put_contributor_summary`. Then load the page as the agent's
own identity and check that it renders:

- the goal;
- every quote, each with its link;
- the ideas under the right stage.

Re-writing replaces the whole page, so it is safe to re-run after new sessions.
The person, the agent's admins or the writing agent can delete it
(`DELETE …/summary/`).

## Why it is built this way

- **Their words, not the agent's view.** The brief that started this (Supply,
  2026-10-09) asked for exactly that: "seeing your idea move from proposed to
  shipped with your own words still attached". A page in the agent's voice
  pulls everyone toward the agent's reading.
- **Stage and attribution are computed, the summary is stored.** Stage and
  attribution are deterministic facts about tasks, so canopy-web derives them
  and they cannot go stale. The summary is judgment, so an agent writes it and
  says what it rests on (`basis`, `sources`).
- **Attribution evidence ranks.** The task's own `human_why` comes first, then
  the human whose turn raised the task, then a page's claim. A page cannot
  override what the task itself records.
