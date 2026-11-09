# Agent-client REST contract (operator plane)

The shared client for canopy-web's agent workspace (`/api/agents`). It covers the **operator plane**:
identity, syncs, skills, projects, tasks, and the action drain (what people did to the agent's tasks,
which the agent carries out). The one run-lifecycle write it carries is `AgentClient.record_verdict`.

**The routes, bodies and filters are stated by the client**: read the method bodies of
`src/orchestrator/agent_client.py::AgentClient` (the typed client; module docstring is the model) and the
`canopy agent …` commands in `src/orchestrator/agent_cli.py` (`canopy agent --help`). A non-Python agent
(e.g. ACE, TS) conforms to the same routes directly; the rules below are what the routes do not make
obvious.

## Auth + base URL

- **Auth header:** `Authorization: Bearer <PAT>`.
- **Which token:** `src/orchestrator/canopy_web.py::resolve_token` decides, and is the only place that
  does (its docstring holds the order). An agent's scripts ask it through `canopy agent token --slug
  <slug>` (`plugins/canopy/agent-core/agent-runtime.md` § Session identity) rather than reading a token
  env var or file; `canopy agent doctor` checks that. A person's own token is minted with
  `/canopy:canopy-web-pat-mint`.
- **Base URL (precedence):** explicit arg, then `CANOPY_WEB_API_URL`, then `canopy_web.DEFAULT_API`.
- **Content-Type:** `application/json` on bodies. Non-2xx responses are errors.

## Provenance headers (who made this request, from which session)

The bearer names an *identity* (every hal turn uses hal's PAT), not a *caller*, so every request canopy
sends also carries provenance, built in one place: `src/orchestrator/provenance.py::provenance_headers`
(its module docstring has the header table and the source order). It is attached by `canopy_web.call` /
`call_text`, `session_upload`, `shareout`, the DDD / share-session / walkthrough-share uploaders and the
canopy-web MCP `headersHelper` (`plugins/canopy/scripts/canopy-web-mcp-headers.js`, only beside a bearer).

- **The parent turn is the whole contract.** canopy-web derives the runner (type and host), chat session,
  origin and initiator from the turn id. Nothing runner-specific goes on the wire, so a new kind of runner
  only has to export `CANOPY_TURN_ID` (and `CANOPY_SESSION_ID`).
- **Provenance never fails a request.** A header that cannot be computed is left off, and canopy-web
  tolerates any of them missing.
- **The laptop path is a record, not env.** emdash launches laptop sessions, so no env can be set; the
  `UserPromptSubmit` hook (`plugins/canopy/hooks/caller_context.py`) claims the runner's one-shot caller
  pointer and writes `~/.canopy/caller/by-task/<task>.json`. The task name read off the worktree path is
  only the local key for finding the turn id and is never sent. The MCP helper runs once per connect, so
  its parent is whatever was knowable at connect time.

## Route gotchas the client enforces

- **Tasks are addressed by `ext_id` only** (`T3`); canopy-web exposes no other task id. Creating takes a
  BARE list and is **create, not upsert**: `create_tasks` keys a named `ext_id` as `<slug>:<ext_id>`, so a
  repeat returns the task the first call made, UNCHANGED, and an `ext_id` already on the board under
  another key is a 409. Change an existing task with `patch_task`. Omit `ext_id` and the server assigns
  `T<n>`.
- **`post_turn` is idempotent per `(agent, cli_session_id)`**; the transcript link (`session_slug` +
  `share_token`) is optional, and an `emdash_task_id` that matches no dispatched turn still records the
  report standalone.
- **Actions** are drained oldest first (`pending_actions`) and each is closed with `mark_action_applied`.
  What approve / nudge / reply start on the server is canopy-web's rule, not the client's.

## Reference implementation

- **Transport + auth:** `src/orchestrator/canopy_web.py` (stdlib `urllib`, injectable transport, single
  source of PAT and base-url resolution).
- **Typed client:** `src/orchestrator/agent_client.py` (`AgentClient`, `catalog_from_repo`).
- **CLI:** `src/orchestrator/agent_cli.py`; `canopy agent turn --upload` reduces and uploads the session
  transcript via `src/orchestrator/session_upload.py` and hangs the `/share/<token>` link off the turn.
- **Repo-identity convenience layer:** `src/orchestrator/agent_web.py` (resolves identity from an agent
  repo's `.claude-plugin/plugin.json` + `config/agent.json`) and the `canopy agent-publish` CLI, both on the
  same `canopy_web` core.
