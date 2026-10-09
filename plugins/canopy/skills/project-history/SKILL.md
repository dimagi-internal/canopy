---
name: project-history
description: Reconstruct a project's history from everything a person and canopy agents did on it, then publish a standard project package from ONE analysis — a session timeline, a useful-artifacts list (evidence required), a short human two-pager on how the agent reads the person's thinking, and an AI-facing package that leads with the evidence. Use when asked for "the story of <project>", "what did we build on <project>", "a project package / timeline for <project>", "how do you read my thinking on <project>", or to brief a teammate before they build on someone else's work.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
```

# Project history

One analysis, several outputs. The CLI (`canopy project-history …`, backed by
`src/orchestrator/project_history.py`) does everything that can be done wrong
*silently*: which sessions belong, splitting merged canopy records, which prompts
the person actually typed, which artifacts exist and what evidence bears on them,
and the rendering. **This skill holds the judgment**: candidates, summaries,
which artifacts were useful, and the reading of the person. Never re-derive in
prose what the CLI computes; never let the CLI's output stand in for a judgment
it cannot make.

Run every CLI call from the requesting agent's repo root, so canopy-web identity
resolves to that agent (`resolve_token`), and through the runtime:
`uv run --project "$CANOPY_ROOT" canopy project-history <cmd> …`.

## 1. Write the spec (parameters, not judgment)

A YAML file, kept with the project's notes so a re-run is identical:

```yaml
name: Supply
person: Jonathan            # whose prompts and thinking this is about
since: "2026-09-10"         # the pivot / start; earlier work is out
repo: connect-labs
paths: [connect_labs/supply_chain, connect_labs/templates/supply_chain, connect_labs/static/supply_chain]
exclude_paths: [connect_labs/supply]      # a retired predecessor, if any
mcp_prefixes: [supply_chain_]             # tool name after mcp__<server>__
name_terms: [supply]                      # WEAK signal only
artifact_terms: [supply, oes, sophie, rutf]
agent_project: hal/P5                     # the board project artifacts are stamped with
workspaces: [connect, dimagi]             # canopy-web workspaces to list artifacts from
```

Get the paths from `git log` of the repo, not from memory. If the person told you
what is in or out (Supply: "leave out the July OES site, keep OES as an audience"),
encode it as `exclude_paths` / `since`, and say it in `scope_notes` (step 4).

## 2. Select — then rule on the candidates

```bash
canopy project-history select --spec supply.yaml -o selection.json
```

The rule it applies, and why: a session is **in** when it *did* project work —
owns a project PR (created it, or names its head branch ≥3 times), edits project
paths, or calls the project's MCP tools (≥3). Keywords alone were noise: "supply"
matched 842 sessions, mostly briefings. A cross-cutting PR (most files outside
the project) and a name match are **weak**: they make a session a `candidate`.

**Your job: rule on every candidate.** Read its prompts
(`canopy project-history read --bundle …` after a collect, or the `select`
output's first lines) and decide include / adjacent / exclude. Typical calls:
the origin session that only *talks* (include — it is often the most important
one); a dispatch stub (include, it carries the run); a session that touched the
project on its way to something else (exclude, or adjacent if the project forced
the work). **Write the calls into the spec** (`include_sessions`,
`adjacent_sessions`, `exclude_sessions`, each line with a `# why`) and re-run
`select`. Judgment that lives only in your head is gone next run.

If a hand-checked list exists, measure before trusting the spec:
`canopy project-history compare --selection selection.json --truth truth.json`.

## 3. Collect

```bash
canopy project-history collect --selection selection.json -o bundle.json
```

Per conversation: the prompts verbatim, each tagged `person` / `dispatch` /
`handoff`; condensed turns (prompt + final reply); owned PRs. Plus the input
documents the person pasted, and every project artifact with its evidence:
comments, owner views, resolved-review decisions (tagged human or agent), the
run's verdict, the agent's own mentions, and the person's **next prompt after
the agent shared it** — the way people actually react.

Text comes from canopy unless a local transcript exists. That is deliberate:
canopy's human-inputs read returns subagent prompts, skill bodies and compaction
summaries as human input, and only the transcript's flags can separate them. Note
the split in coverage; do not "fix" it by trusting the larger count.

## 4. Judge — the part that is yours

Read everything the person typed: `canopy project-history read --bundle bundle.json`
(add `--turns` for a conversation you need the replies for). Then write
`judgments.json`:

```json
{"person": "Jonathan",
 "scope_notes": ["what is in and out, and who decided"],
 "phases": [{"name": "1. …", "summary": "one line", "conversations": ["<id>", …]}],
 "conversations": {"<id>": {"title": "plain words", "summary": "1-3 sentences"}},
 "artifacts": {"<id>": {"useful": true, "evidence": [0, 2], "title": "…", "why": "…"}}}
```

**Condensing a turn or a session:** say what the person asked for and what they
decided, in their terms. Their reversals ("stop working on the other demos")
matter more than the agent's work log. A session that is a stub or status checks
gets one line saying so.

**A useful artifact needs evidence, cited by index** (`read --artifacts` numbers
them). It counts if the agent judged it good at the time ("converged", "verdict:
yes"), or the person responded to it and implied it was good ("okay this is good",
an approval in their own words, a ruling they made on it). Things that do **not**
count:
- an approval the agent gave itself — a review decision "under the standing
  mandate" or "not a human approval" is agent-side, whatever field it sits in;
- "keep going", "go", a status question: that is attention, not approval;
- a critique ("walls of text"). It can be the most important moment in the story,
  but it goes in the summary, not the useful list.
`render` refuses an artifact marked useful with no evidence or no citation. Keep
the list short. A dozen run versions of one story are one story: pick the
version the person reacted to.

## 5. The reading of the person, and the two-pager

Write `reading.md`, the AI package's optional last part. It is a set of claims,
and each claim carries the person's verbatim words. Where they changed their mind,
give the later view. End with what the record cannot show.

Then write the human **two-pager** by hand, two pages at most, as **DRAFT** until
the person or the requesting agent has read it. It must not override the reader's
view: open by saying it is one agent's reading to argue with, point to the
evidence documents, and close with how to use it ("start from your problem;
where your experience disagrees, yours is the better evidence"). Do not include
anything the person said privately that is not in the record.

## 6. Render and publish

```bash
canopy project-history render --bundle bundle.json --judgments judgments.json \
  --reading reading.md --out-dir out/
```

That produces `timeline.md` (people: short), `artifacts.md`, and `ai-package.md`
(evidence first: scope, timeline, useful artifacts, input documents, every
person prompt verbatim; the reading last and labelled optional). Publish each as
a Google Doc into the project's Drive folder through your agent's `gdoc-writer`
(e.g. `canopy gdoc publish --md <abs path> --name "<Project>: Session timeline"
--project "<Project>" --share domain`), read each back, and load every link
before you hand it over. Keep the spec and the judgments next to the docs so the
next run starts from them.

## What not to do

- Don't select by keyword or by `canopy harvest map`. That is the exploratory
  map; this is the record.
- Don't paraphrase the person in the AI package's evidence parts. Quote.
- Don't lead the AI package with the reading. Someone else's AI should reason
  from what happened, not from one agent's summary of the person.
