# Agent-client REST contract (operator plane)

The shared client for canopy-web's agent workspace (`/api/agents`). This is the
**operator plane** only — identity, syncs, skills, projects, tasks, and the
action drain (what people did to the agent's tasks, which the agent carries out). It deliberately carries **no** run/step/artifact/verdict
surface (that is a separate wave).

A non-Python agent (e.g. ACE, TS) can conform to this contract directly without
reading the Python client.

## Auth + base URL

- **Auth header:** `Authorization: Bearer <PAT>`.
- **PAT resolution (precedence):** explicit arg → `CANOPY_WEB_PAT` env →
  `~/.claude/canopy/workbench-token`. Mint one with `/canopy:canopy-web-pat-mint`.
- **Base URL (precedence):** explicit arg → `CANOPY_WEB_API_URL` env →
  `https://canopy.dimagi.com` (prod default).
- **Content-Type:** `application/json` on bodies. Non-2xx responses are errors.

## Provenance headers (who made this request, from which session)

The bearer names an *identity* (every hal turn uses hal's PAT), not a *caller*, so
every request canopy sends also carries provenance — built in one place,
`orchestrator/provenance.py::provenance_headers()`, and attached by
`canopy_web.call`/`call_text`, `session_upload`, `shareout`,
the DDD / share-session / walkthrough-share uploaders, the `post_tool_use` hook
(slim stdlib copy) and the canopy-web MCP `headersHelper`
(`plugins/canopy/scripts/canopy-web-mcp-headers.js`, only beside a bearer):

| Header | Value |
|---|---|
| `X-Canopy-Client` | `<tool>/<version>` — `canopy-cli/…`, `canopy-mcp/…` |
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
| PUT | `/api/agents/{slug}/skills/` | `{skills:[{name,description,url,improvement_note}]}` | replaces catalog |
| POST | `/api/agents/{slug}/tasks/` | a BARE list `[{ext_id?,title,next_action,status,owner,assigned,project,ask_kind,ask_body,origin,idempotency_key,…}]` | create; an `idempotency_key` already seen replays the task it made (the client keys a named `ext_id` as `<slug>:<ext_id>`). Omit `ext_id` and the server assigns `T<n>`. Not an upsert — change an existing task with PATCH |
| GET | `/api/agents/{slug}/tasks/` | — | list tasks; filters `project=P2\|none`, `status=a,b`, `waiting=me`, `ask=open\|closed`, `batch=` |
| GET | `/api/agents/{slug}/tasks/{ext_id}/` | — | one task with its actions |
| PATCH | `/api/agents/{slug}/tasks/{ext_id}/` | partial task fields | store context (rationale/plan/status/links/…). Tasks are addressed by `ext_id` only |
| POST | `/api/agents/{slug}/tasks/{ext_id}/actions` | `{action: approve\|decline\|reply\|nudge\|done, comment}` | what a person does to a task. approve always starts a turn (`on_approve`, or one written from the card); nudge (editor, in-progress only) starts one without a status change; an editor's reply starts one carrying the note. `turn_ids` names them. `dispatch` was removed 2026-10-08 (no alias) |
| GET | `/api/agents/{slug}/actions/?status=pending` | — | the agent's queue: `[{id,task_ext_id,action,comment,by,status,…}]`, oldest first |
| POST | `/api/agents/{slug}/actions/{id}/applied` | `{result_note}` | mark an action carried out |
| GET/POST | `/api/agents/{slug}/projects/` | `{name,outcome,drive_folder_url,links,…}` | list (`?status=`) / create (auto `P<n>`) |
| GET/PATCH | `/api/agents/{slug}/projects/{ref}/` | partial project fields | detail carries `tasks`, `recent_turns`, `links`. A project's `links` are where its deliverables go |
| GET | `/api/tasks/`, `/api/projects/` | — | fleet-wide reads (`agent=`, same task filters; projects `status=`, `repo_slug=`) |

## Reference implementation

- **Transport + auth:** `orchestrator/canopy_web.py` (stdlib `urllib`, injectable
  transport, single source of PAT/base-url resolution).
- **Typed client:** `orchestrator/agent_client.py` (`AgentClient` + `catalog_from_repo`).
- **CLI:** `canopy agent …` (`orchestrator/agent_cli.py`) — `register`, `sync`,
  `turn`, `skills`, `tasks-create`, `tasks`, `add`, `set`, `actions`, `applied`,
  `projects`, `project-add`, `project-set` (`--append-link` files a deliverable).
  `turn` packages a unit of work and (with `--upload`) reduces + uploads the
  session transcript via `orchestrator/session_upload.py`, hanging the
  `/share/<token>` link off the turn.
- **Repo-identity convenience layer:** `orchestrator/agent_web.py` (resolves
  identity from an agent repo's `.claude-plugin/plugin.json` + `config/agent.json`)
  and the `canopy agent-publish` CLI — both sit on the same `canopy_web` core.
