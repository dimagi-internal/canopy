# Knowledge bases (`canopy kb`)

A **knowledge base** is everything one publisher has made public — a funder's grants,
reports, blog, podcast transcripts, cost-effectiveness models — kept as dated, cited
copies, with the tables you compute with alongside. Code: `src/orchestrator/kb.py`
(build + read), `kb_cli.py` (CLI, Drive pull/push), `kb_mcp.py` (stdio MCP server).

## Who owns what

| | Owner | Where |
|---|---|---|
| The library | a **workspace project** (e.g. `connect/givewell-kb`), its curator as steward | its Drive folder holds `kb.sqlite.gz` |
| Adapters (where the publisher puts things) + the library's skill | the **curator** agent | the curator's repo (e.g. Eva's `bin/eva-kb-givewell`, `skills/givewell-kb`) |
| Build, diff, store, serve (MCP) | **canopy** | this module |
| Readers | **every agent** in the workspace | the MCP tools (`canopy kb mcp`) |

A knowledge base is not an agent's project in the board sense: it has no end, and many
agents use it. So it is a canopy-web **workspace project**: owned by the workspace,
addressed `<workspace>/<handle>`, with the curator agent as its steward (the steward can
change without the address changing, and any agent in the workspace files tasks into it).
Put it in the workspace whose agents use it, which need not be the curator's: the steward
may be an agent of that workspace or of one above it (`connect/givewell-kb` belongs to
Connect, where ACE reads it, and is kept by dimagi's Eva). Public material only: what you know about
the publisher from the relationship stays in your CRM and never merges in, so a KB is
always safe to hand to any agent or teammate.

## The bundle contract (what a curator writes)

```
bundle/
  manifest.json          {"name", "publisher", "license", "tables": {...}, "search_boost": {...}}
  items.jsonl            {"id", "type", "title", "url", "date", "text", "meta", "sha256", "fetched_at"}
  tables/<name>.jsonl    one JSON object per row
```

- `id` is stable across refreshes (it is how changes are detected); `sha256` is of the
  content, so an unchanged page is not reported as changed.
- `url` is the citation: every answer carries it.
- `tables.<name>`: `description`, and optionally `date_column`, `amount_column`,
  `filter_columns` — each declared table also becomes a named MCP tool whose arguments
  are its `filter_columns`.
- `search_boost`: `{type: factor}`. A publisher's site usually hosts thousands of
  third-party documents; boost its own analysis above them or they drown every search.

## Build and publish (curators)

```bash
canopy kb build bundle/ --out kb.sqlite [--previous last.sqlite]   # diff → change history
canopy kb push --kb connect/givewell-kb kb.sqlite             # → Drive, gzipped
```

`build` stores each passage once (an external-content FTS5 index over ~1.8k-char
passages; `get` rejoins them), and carries the previous build's change rows forward, so
`whats-new` is a dated history across refreshes. A build with no previous is a baseline.

## Read (everyone): the MCP tools

```json
{"mcpServers": {"givewell": {"command": "canopy", "args": ["kb", "mcp", "--kb", "connect/givewell-kb"]}}}
```

Tools: `kb_info`, `search`, `get` (with `grep` to verify a quote or number), `list_items`,
`whats_new`, `table`, plus one per declared table (`grants`, …). Stdlib JSON-RPC over
stdio, no SDK dependency.

`--kb` is a workspace project address (`<workspace>/<handle>`), an agent project ref
(`<agent>/<Pnn>`) — either resolved to its Drive folder through canopy-web — a Drive
folder id, or a local `kb.sqlite`. Drive is read as the agent whose repo you run from
(`--repo`, default cwd). The copy is cached at `~/.canopy/kb/<ref>/kb.sqlite` and
re-checked after `--max-age-hours` (24); if Drive is unreachable the cache is served with
a warning.

There are no read subcommands (`canopy kb search` …): they would only duplicate the tools.
A script that needs a query imports `orchestrator.kb.KB`.

## A skill per library

The tools are generic; how to use ONE library well is not. Its curator ships a skill
alongside the adapters (Eva's `givewell-kb`): what it covers and what it does not, which
source wins when they disagree (a grants table beats a podcast's rounded figure), what to
cite (the row's url, never the KB), and how the curator refreshes it.

## Where this is going

Today an agent registers the stdio server in its own `.mcp.json`, and each machine holds a
copy. The intended end state: the **workspace project declares its server** (a typed field
on the project, e.g. `{"kind": "kb"}`, with the tool list coming from the KB's own
manifest), and **canopy-web serves it remotely** as a Streamable-HTTP MCP endpoint per
project (`/api/workspace-projects/<ws>/<handle>/mcp/`), behind the same tokens and OAuth as
canopy-web's own `/api/mcp/`. Then claude.ai, ACE on any runner and other people's Claude
Code add one URL, with nothing to install or download. This module's read API is the
contract that server implements.
