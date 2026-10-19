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

## Read (everyone): the project's own MCP server

A workspace project that publishes a KB serves it from canopy-web as its own remote MCP
server, carrying only that KB's tools. Readers add one URL and download nothing:

```bash
claude mcp add --transport http givewell-kb https://canopy.dimagi.com/api/mcp/projects/connect/givewell-kb
```

(claude.ai / Claude Desktop: paste the same URL as a custom connector.) The first call
signs in with your canopy account (OAuth). The project page on canopy-web shows both lines.
An agent repo with no person at a browser adds it to `.mcp.json` with the agent's own
identity:

```json
{"mcpServers": {"givewell": {"type": "http",
  "url": "https://canopy.dimagi.com/api/mcp/projects/connect/givewell-kb",
  "headersHelper": "canopy mcp-headers"}}}
```

Tools: `kb_info`, `search`, `get` (with `grep` to verify a quote or number), `list_items`,
`whats_new`, `table`, plus one per declared table (`grants`, …). Readable by members of
the project's workspace and of its steward's.

**Publishing it** is part of `canopy kb push --kb <workspace>/<handle>`: after the Drive
copy, it uploads the gzipped file straight to storage (a presigned PUT) and publishes it,
and every canopy instance serves the new build from its next call. Drive keeps the copy
people browse and the source for the offline path below.

**Offline / local:** `canopy kb mcp --kb <ref>` serves the same tools over stdio from a
copy cached at `~/.canopy/kb/<ref>/kb.sqlite` (pulled from the project's Drive folder,
re-checked after `--max-age-hours`). `--kb` is a workspace project address, an agent
project ref (`<agent>/<Pnn>`), a Drive folder id, or a local `kb.sqlite`.

There are no read subcommands (`canopy kb search` …): they would only duplicate the tools.
A script that needs a query imports `orchestrator.kb.KB`.

## A skill per library

The tools are generic; how to use ONE library well is not. Its curator ships a skill
alongside the adapters (Eva's `givewell-kb`): what it covers and what it does not, which
source wins when they disagree (a grants table beats a podcast's rounded figure), what to
cite (the row's url, never the KB), and how the curator refreshes it.

## How canopy-web serves it

`/api/mcp/projects/<workspace>/<handle>/` (canopy-web `apps/mcp/projects.py`): one
Streamable-HTTP server for every project, whose tool list is the named project's, behind
the same sign-in as `/api/mcp/`. The engine there is `packages/canopy_kb` — the same
engine as `orchestrator/kb.py`; keep them in step until this CLI depends on that package.
Discovery is the workspace's project list and each project page; a generated marketplace
entry per project is a possible later addition, for finding them from Claude Code.
