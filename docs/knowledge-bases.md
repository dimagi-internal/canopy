# Knowledge bases (`canopy kb`)

A **knowledge base** is everything one publisher has made public (a funder's grants, reports, blog,
podcast transcripts, cost-effectiveness models) kept as dated, cited copies, with the tables you compute
with alongside. The engine is canopy-web's `packages/canopy_kb` (stdlib-only; its README and module
docstrings state the bundle format, the curation model and the tool list), loaded here by
`src/orchestrator/kb_engine.py`. Start reading at `src/orchestrator/kb_cli.py` (its module docstring: the
four commands, which `--kb` forms exist, the cache), `kb_remote.py` (a curated refresh's canopy-web store)
and `kb_mcp.py` (stdio MCP server).

## Who owns what

| | Owner | Where |
|---|---|---|
| The library | a **workspace project** (e.g. `connect/givewell-kb`), its curator as steward | canopy-web: a version log of its kb.sqlite (a Drive copy only when the project turns its replica on) |
| Adapters (where the publisher puts things, how its items are typed, its tables and guide) + the library's skill | the **curator** agent | the curator's repo (e.g. Eva's `bin/eva-kb-givewell`, `lib/kb_givewell`, `skills/givewell-kb`) |
| Preprocessing every curator needs: raw cache, a reader per format, OCR of scans, dedupe, catalog cards, aliases, originals | **canopy** | `canopy_kb.prep` (canopy-web `packages/canopy_kb`, extra `[prep]`) |
| Build, diff, store, serve (MCP) | **canopy** | this module |
| Readers | **every agent** in the workspace | the MCP tools (`canopy kb mcp`) |

A knowledge base is not an agent's project in the board sense: it has no end, and many agents use it. So
it is a canopy-web **workspace project**: owned by the workspace, addressed `<workspace>/<handle>`, with
the curator agent as its steward (the steward can change without the address changing, and any agent in
the workspace files tasks into it). Put it in the workspace whose agents use it, which need not be the
curator's: the steward may be an agent of that workspace or of one above it (`connect/givewell-kb`
belongs to Connect, where ACE reads it, and is kept by dimagi's Eva). **Public material only**: what you
know about the publisher from the relationship stays in your CRM and never merges in, so a KB is always
safe to hand to any agent or teammate.

## The bundle (what a curator writes)

`manifest.json`, `items.jsonl`, `tables/<name>.jsonl` and optional `aliases.json`; the field list is
`packages/canopy_kb`'s (`kb.py`'s docstring). What the code does not make obvious:

- `text` is **markdown, extracted completely** (headings as `#` lines, tables as rows, no size cap). What
  the extraction drops no search can find; this is where a KB is won or lost.
- `id` is stable across refreshes (it is how changes are detected); `sha256` is of the content, so an
  unchanged page is not reported as changed. `url` is the citation: every answer carries it.
- `card` is written by the curator (a model reading the document); cards are what an agent browses before
  it reads, and the source of the entity index.
- `original` is the **file the text was read from**, as the publisher served it. The text is a reading
  copy, made so search, `read` and `grep` work; the original stays the record (the workbook a reader
  recomputes, a PDF's charts, a version the publisher has since changed or taken down). It never leaves
  the curator's machine except as the upload. Items with no file behind them leave it out.

## Curating with no local state

A refresh runs from an empty disk on any machine: canopy-web keeps everything it needs from the last one,
so no runner's disk matters. A curator's refresh builds a `canopy_kb.curate.Curation` over
`orchestrator.kb_remote.RemoteStore`; `packages/canopy_kb`'s `curate.py` docstring is the model (step
results keyed by their inputs and code, `check_lock` refusing a code change without a REVISION bump, one
refresh wins). A refresh with nothing new publishes nothing. A large KB's refresh moves its whole file
down and up (GiveWell's is ~760 MB), so run it on a cloud runner.

## Build and publish (curators)

```bash
canopy kb build bundle/ --out kb.sqlite [--previous last.sqlite]   # diff -> change history
canopy kb push --kb connect/givewell-kb kb.sqlite --originals bundle/   # originals, then the whole file
```

- **canopy-web never builds an index** (building in its web process runs it out of memory). The machine
  that refreshes builds the file and `push` publishes it whole, as a version made from the one served now
  (`src/orchestrator/kb_cli.py::publish_to_canopy`); canopy-web stores and serves it and refuses a
  publish made from a version that is no longer the head (push again). Skipping an unchanged publish is a
  curated refresh's job, not `push`'s. Nothing goes to Drive unless the project turns its replica on.
- `build` needs the engine in canopy's environment (the command prints the install line when it is
  missing: `kb_engine.py::INSTALL_HINT`); `push` does not. `--previous` carries change history and
  originals forward, so `whats_new` is a dated history across refreshes.
- `--originals bundle/` uploads each item's original to canopy-web's store before the build is published,
  only the ones it lacks: they are stored by sha256 and never deleted, so every earlier version stays
  downloadable. A missing file on disk refuses the push rather than publish a build that points at it.

**The curator's toolkit** is `canopy_kb.prep` (`pip install 'canopy-kb[prep] @
git+ssh://git@github.com/dimagi-internal/canopy-web.git#subdirectory=packages/canopy_kb'`), so a curator
writes only what is particular to its publisher. Eva's `lib/kb_givewell` is the worked example.

## Read (everyone): the project's own MCP server

A workspace project that publishes a KB serves it from canopy-web as its own remote MCP server, carrying
only that KB's tools. Readers add one URL and download nothing:

```bash
claude mcp add --transport http givewell-kb https://canopy.dimagi.com/api/mcp/projects/connect/givewell-kb
```

(claude.ai / Claude Desktop: paste the same URL as a custom connector.) The first call signs in with your
canopy account (OAuth); the project page on canopy-web shows both lines. An agent repo with no person at a
browser adds it to `.mcp.json` with the agent's own identity:

```json
{"mcpServers": {"givewell": {"type": "http",
  "url": "https://canopy.dimagi.com/api/mcp/projects/connect/givewell-kb",
  "headersHelper": "canopy mcp-headers"}}}
```

The tool list is the engine's (`kb_info`, `search`, `entity`, `doc`, `read`, `grep`, `sql`, `get`,
`list_items`, `whats_new`, `table`, one tool per declared table, and `original` when the build carries
originals): `packages/canopy_kb` (its `tool_specs`). Readable by members of the project's workspace
and of its steward's. There are no read subcommands (no search, no get): they would only duplicate the
tools. A script that needs a query imports `canopy_kb.KB`.

**Publishing it** is `canopy kb push --kb <workspace>/<handle>`: every canopy instance serves the new
version from its next call.

**Offline / local:** `canopy kb mcp --kb <ref>` serves the same tools over stdio from a copy cached at
`~/.canopy/kb/<ref>/kb.sqlite` (re-checked after `--max-age-hours`). `--kb` is a workspace project
address, an agent project ref (`<agent>/<Pnn>`), a Drive folder id, or a local `kb.sqlite`. Offline,
`original` describes the file and points at the publisher's copy; only canopy-web hands out the stored one.

## A skill per library

The tools are generic; how to use ONE library well is not. Its curator ships a skill alongside the
adapters (Eva's `givewell-kb`): what it covers and what it does not, which source wins when they disagree
(a grants table beats a podcast's rounded figure), what to cite (the row's url, never the KB), and how the
curator refreshes it.

## How canopy-web serves it

`/api/mcp/projects/<workspace>/<handle>/` (canopy-web `apps/mcp/projects.py`): one Streamable-HTTP server
for every project, whose tool list is the named project's, behind the same sign-in as `/api/mcp/`. The
engine there is `packages/canopy_kb`, the one this CLI imports. **Finding one:** the MCP directory, a list
you browse and choose from, covering a workspace and the ones below it that you belong to: Projects, MCP
servers on canopy-web (`/w/<ws>/mcp`), or, for an agent, the `list_mcp_servers` tool on canopy's main MCP
server. Nothing adds a project server to a session for you.
