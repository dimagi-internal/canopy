# Knowledge bases (`canopy kb`)

A **knowledge base** is everything one publisher has made public — a funder's grants,
reports, blog, podcast transcripts, cost-effectiveness models — kept as dated, cited
copies, with the tables you compute with alongside. Code: `src/orchestrator/kb.py`
(build + read), `kb_cli.py` (CLI, Drive pull/push), `kb_mcp.py` (stdio MCP server).

## Who owns what

| | Owner | Where |
|---|---|---|
| The library | a **workspace project** (e.g. `dimagi/givewell-kb`), its curator as steward | its Drive folder holds `kb.sqlite.gz` |
| Adapters (where the publisher puts things) | the **curator** agent | the curator's repo (e.g. Eva's `bin/eva-kb-givewell`) |
| Build, diff, store, query, MCP | **canopy** | this module |
| Readers | **every agent** in the workspace | `canopy kb …` / `canopy kb mcp` |

A knowledge base is not an agent's project in the board sense: it has no end, and many
agents use it. So it is a canopy-web **workspace project**: owned by the workspace,
addressed `<workspace>/<handle>`, with the curator agent as its steward (the steward can
change without the address changing, and any agent in the workspace files tasks into it). Public material only: what you know about
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
canopy kb push --kb dimagi/givewell-kb kb.sqlite             # → Drive, gzipped
```

`build` stores each passage once (an external-content FTS5 index over ~1.8k-char
passages; `get` rejoins them), and carries the previous build's change rows forward, so
`whats-new` is a dated history across refreshes. A build with no previous is a baseline.

## Query (everyone)

```bash
canopy kb search "oral cholera vaccine" --kb dimagi/givewell-kb [--type grant_page] [--since 2024-01-01]
canopy kb get <id-or-url> --kb dimagi/givewell-kb [--passage N]
canopy kb table grants --kb dimagi/givewell-kb --match Recipient=Dimagi --sort -Amount
canopy kb whats-new --kb dimagi/givewell-kb --since 2026-10-01
canopy kb info --kb dimagi/givewell-kb
```

`--kb` is a workspace project address (`<workspace>/<handle>`), an agent project ref
(`<agent>/<Pnn>`) — either resolved to its Drive folder through canopy-web — a Drive
folder id, or a local `kb.sqlite`. Drive is read as the agent whose
repo you run from (`--repo`, default cwd). The copy is cached at
`~/.canopy/kb/<ref>/kb.sqlite` and re-checked after `--max-age-hours` (24); if Drive is
unreachable the cache is served with a warning.

## As MCP tools

```json
{"mcpServers": {"givewell": {"command": "canopy", "args": ["kb", "mcp", "--kb", "dimagi/givewell-kb"]}}}
```

Tools: `kb_info`, `search`, `get`, `list_items`, `whats_new`, `table`, plus one per
declared table (`grants`, …). Stdlib JSON-RPC over stdio, no SDK dependency.

## Where this is going

Today an agent registers the MCP server in its own `.mcp.json`. The intended end state
is that the **workspace project exposes its tools** itself — every agent in the workspace gets a
KB the moment it is attached to the project, with canopy-web serving the queries instead
of each machine holding a copy. This module's read API is the contract that server would
implement.
