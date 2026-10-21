# Knowledge bases (`canopy kb`)

A **knowledge base** is everything one publisher has made public — a funder's grants,
reports, blog, podcast transcripts, cost-effectiveness models — kept as dated, cited
copies, with the tables you compute with alongside. Engine: canopy-web's
`packages/canopy_kb` (stdlib-only; loaded here by `src/orchestrator/kb_engine.py`)
(build + read), `kb_cli.py` (CLI, Drive pull/push), `kb_mcp.py` (stdio MCP server).

## Who owns what

| | Owner | Where |
|---|---|---|
| The library | a **workspace project** (e.g. `connect/givewell-kb`), its curator as steward | its Drive folder holds `kb.sqlite.gz` |
| Adapters (where the publisher puts things, how its items are typed, its tables and guide) + the library's skill | the **curator** agent | the curator's repo (e.g. Eva's `bin/eva-kb-givewell`, `lib/kb_givewell`, `skills/givewell-kb`) |
| Preprocessing every curator needs: raw cache, a reader per format (HTML, PDF, xlsx/xls, docx/doc), OCR of scans, dedupe, catalog cards, aliases, originals | **canopy** | `canopy_kb.prep` (canopy-web `packages/canopy_kb`, extra `[prep]`) |
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
  manifest.json          {"name", "publisher", "license", "tables": {...}, "guide": [...]}
  items.jsonl            {"id", "type", "title", "url", "date", "text", "meta", "sha256", "fetched_at",
                          "card": {...}, "original": {...}}
  tables/<name>.jsonl    one JSON object per row
  aliases.json           optional: {"org"|"program"|"country": [{"canonical", "names": [...]}]}
```

- `text` is **markdown, extracted completely**: headings as `#` lines (they become each
  passage's section path and the document's outline), tables as rows, no size cap. What
  the extraction drops no search can find; this is where a KB is won or lost.
- `card` is the item's **catalog card**, written by the curator (a model reading the
  document): `summary` (what it is, what it says, its headline figures), `kind`, `orgs`,
  `programs`, `countries`, `period`, `superseded`, `answers` (questions it answers).
  Cards are what an agent browses before it reads, and the source of the entity index.
- `original` is the **file the text was read from**, as the publisher served it:
  `sha256`, `filename`, `content_type`, `bytes`, `source_url`, `fetched_at`, and `path`
  (where the curator's copy sits; it never leaves the curator's machine except as the
  upload). The text is a reading copy, made so search, `read` and `grep` work; the original
  stays the record: the workbook a reader recomputes, a PDF's charts and layout, a version
  the publisher has since changed or taken down. `canopy_kb.prep.originals.record` makes
  one from a raw fetch. Items with no file behind them (an API's posts, a table's rows)
  leave it out.
- `meta.linked_from` (ids or urls of the items that link to this one) and `meta.parent`
  become the link graph; `meta.also_at` lists other urls of the same document.

- `id` is stable across refreshes (it is how changes are detected); `sha256` is of the
  content, so an unchanged page is not reported as changed.
- `url` is the citation: every answer carries it.
- `tables.<name>`: `description`, and optionally `date_column`, `amount_column`,
  `filter_columns` — each declared table also becomes a named MCP tool whose arguments
  are its `filter_columns`.
- `guide`: publisher-specific notes `kb_info` shows (which source wins, whose view a
  document type is, e.g. "charity-supplied PDFs are not the publisher's view").
- `aliases.json`: names that are one organisation or program (renames, synonyms), so
  `entity` gathers them; normalised spellings and acronyms merge without it.

## Build and publish (curators)

```bash
canopy kb build bundle/ --out kb.sqlite [--previous last.sqlite]   # diff → change history
canopy kb push --kb connect/givewell-kb kb.sqlite --originals bundle/   # originals, Drive, publish
```

`build` needs the engine in canopy's environment (`uv tool install … --with
'canopy-kb @ git+ssh://git@github.com/dimagi-internal/canopy-web.git#subdirectory=packages/canopy_kb'`;
the command prints this when it is missing). It splits each item into 3,200-char passages
with 400 of overlap along its headings, indexes them (FTS5) with the cards, builds the
entity index, outlines and links, stores each declared table as SQL, and carries the
previous build's change rows forward, so `whats_new` is a dated history across
refreshes. It carries originals forward the same way: a file no item points at any more
stays listed as a superseded version. A build with no previous is a baseline. `push`
needs no engine.

`--originals bundle/` uploads each item's original to canopy-web's store before the build
is published, only the ones it lacks: they are stored by sha256 (S3 checks each upload
against it) and never deleted, so an unchanged file uploads once and every earlier version
stays downloadable. A missing file on disk refuses the push rather than publish a build
that points at it.

**The curator's toolkit.** `canopy_kb.prep` (`pip install 'canopy-kb[prep] @
git+ssh://git@github.com/dimagi-internal/canopy-web.git#subdirectory=packages/canopy_kb'`)
is the preprocessing every library needs, so a curator writes only what is particular to
its publisher: `raw` (an HTTP cache keeping every response's bytes), `extract` (a reader
per format, cached by sha256: a page's whole content region, PDFs in reading order,
spreadsheets row by row with headers, formulas and notes, Word files), `ocr` (scanned PDFs
transcribed by Gemini), `dedupe` (identical documents once, a spreadsheet tab copied into
many workbooks once), `cards` and `aliases` (Gemini), `originals`. Eva's
`lib/kb_givewell` is the worked example: GiveWell's sources and crawl, how its items are
typed, its tables and guide, and calls into `prep` for the rest.

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
`whats_new`, `table`, plus one per declared table (`grants`, …), and `original` when the
build carries originals (a 15-minute download link to the file, and its earlier
versions). Readable by members of
the project's workspace and of its steward's.

**Publishing it** is part of `canopy kb push --kb <workspace>/<handle>`: after the
originals (with `--originals`) and the Drive copy, it uploads the gzipped file straight
to storage (a presigned PUT) and publishes it,
and every canopy instance serves the new build from its next call. Drive keeps the copy
people browse and the source for the offline path below.

**Offline / local:** `canopy kb mcp --kb <ref>` serves the same tools over stdio from a
copy cached at `~/.canopy/kb/<ref>/kb.sqlite` (pulled from the project's Drive folder,
re-checked after `--max-age-hours`). `--kb` is a workspace project address, an agent
project ref (`<agent>/<Pnn>`), a Drive folder id, or a local `kb.sqlite`.

Tools, for an agent doing research: `kb_info` (what the library holds, the SQL tables,
how to work it), `search` (documents with their cards and best passages), `entity`
(everything about an organisation or program, with its grants), `doc` (outline, links,
other versions), `read` (a section, or from an offset), `grep` (a figure or quote in a
long document, or across an entity's documents), `sql` (read-only over the tables),
`original` (the file a document was read from), plus `get`, `list_items`, `whats_new`,
`table` and one tool per declared table. Served offline (`canopy kb mcp`), `original`
describes the file and points at the publisher's copy; only canopy-web hands out the
stored one. Measured on 70
GiveWell questions with the same agent, this set answered 35.5 of 40 fact questions
against 28.5 with one-shot passage search over embeddings, and won 18.5 of 30 research
questions head to head.

There are no read subcommands (`canopy kb search` …): they would only duplicate the tools.
A script that needs a query imports `canopy_kb.KB`.

## A skill per library

The tools are generic; how to use ONE library well is not. Its curator ships a skill
alongside the adapters (Eva's `givewell-kb`): what it covers and what it does not, which
source wins when they disagree (a grants table beats a podcast's rounded figure), what to
cite (the row's url, never the KB), and how the curator refreshes it.

## How canopy-web serves it

`/api/mcp/projects/<workspace>/<handle>/` (canopy-web `apps/mcp/projects.py`): one
Streamable-HTTP server for every project, whose tool list is the named project's, behind
the same sign-in as `/api/mcp/`. The engine there is `packages/canopy_kb`, the one this
CLI imports. `search` is words over passages and catalog cards, ordered by a reranker
(Vertex AI ranking API); no embeddings are involved. canopy-web keeps a vector path
dormant (`KB_VECTORS`, off) for a corpus that ever needs search by meaning.
**Finding one:** the MCP directory, a list you browse and choose from. It covers a
workspace and the ones below it that you belong to: Projects ▸ MCP servers on
canopy-web (`/w/<ws>/mcp`), or, for an agent, the `list_mcp_servers` tool on canopy's
main MCP server. Each entry has what it is, its steward, when it was published, and the
line that adds it. Nothing adds a project server to a session for you.
