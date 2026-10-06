# Agent-client REST contract (operator plane)

The shared client for canopy-web's agent workspace (`/api/agents`). This is the
**operator plane** only — identity, syncs, work-products, skills, tasks, and the
board-command drain. It deliberately carries **no** run/step/artifact/verdict
surface (that is a separate wave).

A non-Python agent (e.g. ACE, TS) can conform to this contract directly without
reading the Python client.

## Auth + base URL

- **Auth header:** `Authorization: Bearer <PAT>`.
- **PAT resolution (precedence):** explicit arg → `CANOPY_WEB_PAT` env →
  `~/.claude/canopy/workbench-token`. Mint one with `/canopy:canopy-web-pat-mint`.
- **Base URL (precedence):** explicit arg → `CANOPY_WEB_API_URL` env →
  `https://labs.connect.dimagi.com/canopy` (prod default).
- **Content-Type:** `application/json` on bodies. Non-2xx responses are errors.

## Provenance headers (who made this request, from which session)

The bearer names an *identity* (every hal turn uses hal's PAT), not a *caller*, so
every request canopy sends also carries provenance — built in one place,
`orchestrator/provenance.py::provenance_headers()`, and attached by
`canopy_web.call`/`call_text`, `session_upload`, `shareout`, `portfolio_discover`,
the DDD / share-session / walkthrough-share uploaders, the `post_tool_use` hook
(slim stdlib copy) and the canopy-web MCP `headersHelper`
(`plugins/canopy/scripts/canopy-web-mcp-headers.js`, only beside a bearer):

| Header | Value |
|---|---|
| `X-Canopy-Client` | `<tool>/<version>` — `canopy-cli/…`, `canopy-mcp/…`, `canopy-hook/…` |
| `X-Canopy-Parent-Turn` | canopy-web turn uuid this session is running |
| `X-Canopy-Parent-Session` | that turn's canopy-web chat session uuid |
| `X-Canopy-Claude-Session` | Claude Code session id |
| `User-Agent` | `canopy-cli/<version> (python/<x.y>; <platform>)` |

**The parent turn is the whole contract.** A canopy-web turn already records which
runner claimed it (so its type and host), its chat session, origin and initiator; the
server derives all of that from the id. Nothing runner-specific goes on the wire, so a
new kind of runner only has to export `CANOPY_TURN_ID` (+ `CANOPY_SESSION_ID`).

Sources, first value wins per field: env **`CANOPY_TURN_ID` / `CANOPY_SESSION_ID`**
(the cloud runner exports them into every session it launches)
→ the envelope **`CANOPY_CALLER`** names (`turn_id`, `conversation.session_id`) →
**`~/.canopy/caller/by-task/<task>.json`**, the laptop record. emdash launches laptop
sessions so no env can be set; the `UserPromptSubmit` hook (`caller_context.py`), on
claiming the runner's one-shot caller pointer, writes that record
(`{turn_id, session_id, claude_session_id, updated_at}`, atomic, 0600), and the task is
read back off the worktree path (`…/worktrees/<repo>/emdash-<task>-<suffix>/`) — the
task name is only the local key for finding the turn id, never sent. The
Claude session id comes from `CLAUDE_CODE_SESSION_ID` (or `CLAUDE_SESSION_ID`), else the
record. A header that cannot be computed is left off — provenance never fails a request,
and canopy-web tolerates any of them missing. The MCP helper runs once per connect, so
its parent is whatever was knowable at connect time.

## Endpoints

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/agents/` | `{slug,name,email,description,persona,avatar_url}` | upsert identity |
| POST | `/api/agents/{slug}/syncs/` | `{period_start,period_end,title,summary,doc_url,self_grades,source}` | idempotent per period+source |
| GET | `/api/agents/{slug}/turns/` | — | list packaged turns |
| POST | `/api/agents/{slug}/turns/` | `{cli_session_id,title,summary,task_ext_ids,work_product_urls,session_slug,share_token,started_at,ended_at,source}` | package a turn; idempotent per `cli_session_id`. Transcript link (`session_slug`+`share_token`) optional |
| POST | `/api/agents/{slug}/work-products/` | `{work_products:[{title,kind,url,description,tags,source}]}` | upsert by url |
| PUT | `/api/agents/{slug}/skills/` | `{skills:[{name,description,url,improvement_note}]}` | replaces catalog |
| POST | `/api/agents/{slug}/tasks/sync` | `{tasks:[{ext_id,title,next_action,status,owner,assigned,…}]}` | non-destructive upsert |
| GET | `/api/agents/{slug}/tasks/` | — | list board tasks (e.g. to compute the next `ext_id`) |
| GET | `/api/agents/{slug}/commands?status=pending` | — | drain queued board actions |
| POST | `/api/agents/{slug}/commands/{id}/apply` | `{result_note}` | mark a command applied |
| PATCH | `/api/agents/{slug}/tasks/{id}/` | partial task fields | store context (rationale/plan/status/…) |

## Reference implementation

- **Transport + auth:** `orchestrator/canopy_web.py` (stdlib `urllib`, injectable
  transport, single source of PAT/base-url resolution).
- **Typed client:** `orchestrator/agent_client.py` (`AgentClient` + `catalog_from_repo`).
- **CLI:** `canopy agent …` (`orchestrator/agent_cli.py`) — `register`, `sync`,
  `turn`, `work`, `skills`, `tasks-sync`, `tasks`, `commands`, `apply`, `set`.
  `turn` packages a unit of work and (with `--upload`) reduces + uploads the
  session transcript via `orchestrator/session_upload.py`, hanging the
  `/share/<token>` link off the turn.
- **Repo-identity convenience layer:** `orchestrator/agent_web.py` (resolves
  identity from an agent repo's `.claude-plugin/plugin.json` + `config/agent.json`)
  and the `canopy agent-publish` CLI — both sit on the same `canopy_web` core.
