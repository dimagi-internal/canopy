---
name: improve
description: Run a full canopy improvement cycle — analyze recent sessions, propose improvements, and implement via agents
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
_CANOPY_DIR="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# Improve

Run a canopy improvement cycle: analyze recent Claude Code sessions for friction
and gaps, propose concrete improvements, then dispatch agents to implement them.

## Arguments

- No args → full cycle (analyze + propose + implement via agents)
- `observe` → analyze only, write observations
- `dry-run` → analyze + propose, no implementation

## Phase 1 — Discover transcripts

Run this to get recent session metadata:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy sessions list --hours 168 --json-output 2>/dev/null
```

Then read the run logs from `~/.claude/canopy/runs/` to find already-processed
session IDs. Filter those out. Take the last 10 unprocessed sessions.

If fewer than 2 unprocessed sessions exist, tell the user and stop.

## Phase 2 — Analyze transcripts (direct, in-context)

1. For each transcript, read the JSONL file. Each line is a JSON object with
   `type` (user/assistant/summary) and `message` content. Extract:
   - User messages (what they asked)
   - Tool calls and results (what was attempted, what failed)
   - Assistant text (what Claude said — especially error explanations)

   For large transcripts (>500 lines), read only the first 100 and last 200 lines
   to stay within context limits.

2. Analyze ALL transcripts together. Look for:

   - **friction** — a tool was used but worked poorly (failed, retried, unhelpful)
   - **gap** — the user did something manually that could have been automated
   - **pattern** — a multi-step sequence that recurs and could become a workflow
   - **missing_capability** — the user needed something no tool/skill could handle

   Because you're analyzing all transcripts at once, you can spot cross-session
   patterns that isolated analysis would miss.

3. Read existing observations from `~/.claude/canopy/observations/` (glob for
   `*.yaml`, read each one). Deduplicate:
   - If a new observation matches an existing one (same type + related_servers +
     lifecycle_stage, status=pending), merge by incrementing frequency and adding
     the session ID
   - Otherwise create a new observation

4. Write observation YAML files. Format:

```yaml
id: <12-char hex>
type: friction|gap|pattern|missing_capability
description: "1-2 sentence description"
severity: low|medium|high
frequency: 1
sessions:
  - <session-id>
related_servers:
  - <server-name>
lifecycle_stage: null
status: pending
created: 'YYYY-MM-DD'
```

Save each to `~/.claude/canopy/observations/<id>.yaml`.

If mode is `observe`, show a summary table and stop here.

## Phase 3 — Propose improvements (direct, in-context)

Read all pending observations sorted by frequency (desc) then severity (high first).
Take the top 6.

For each observation (or group of related ones), generate a proposal:

```yaml
id: <12-char hex>
type: new_tool|tool_improvement|new_skill|new_workflow|hook_improvement
action: "Specific description of what to build/change"
target_repo: <short-repo-name>          # e.g. "ace", "canopy", "ace-web"
                                         # NOT a path — consumers resolve the
                                         # short name to the local checkout
                                         # via orchestrator.repo_paths.resolve_repo_path
ownership: self|team|external
motivation: "Why — reference the observation"
observation_id: <observation-id>
complexity: low|medium|high
verification:
  type: replay|tool_exists|integration_test|observational
  test_description: "One sentence"
  sample_inputs: {}
  expected_outcome: "What success looks like"
  confidence: high|medium|low
status: pending
failure_reason: null
created: 'YYYY-MM-DD'
```

**Guidelines:**
- Fix at the source, not metadata. If CLAUDE.md is missing info, fix CLAUDE.md.
- Prefer adding to existing servers over creating new ones.
- Be specific: "Add filter_by_status param to search_opportunities" not "improve search".
- Strongly prefer proposals with high verification confidence.
- For `ownership: external`, skip implementation — propose the change for the owning team to make.

Save each to `~/.claude/canopy/proposals/<id>.yaml`.

If mode is `dry-run`, show a summary table and stop here.

## Phase 4 — User approval gate

Present proposals in a summary table:

| # | Type | Target | Action (summary) | Complexity |
|---|------|--------|-------------------|------------|

Ask: "Which proposals should I implement? (all / numbers / none)"

Wait for user response. If "none", stop. If specific numbers, filter to those.

## Phase 5 — Implement via parallel agents

For each approved proposal, dispatch an Agent:

```
Agent(
  description: "Implement <short-summary>",
  isolation: "worktree",
  prompt: <see agent prompt below>
)
```

**Parallelism rules:**
- Proposals targeting DIFFERENT repos → dispatch in parallel (single message, multiple Agent calls)
- Proposals targeting the SAME repo → dispatch sequentially (worktrees share the repo)
- For `ownership: external` → skip (no implementation; propose for the owning team)

**Agent prompt template:**

> You are implementing an improvement to a codebase based on a proposal from
> usage analysis.
>
> ## Proposal
> <proposal YAML>
>
> ## Why This Is Needed
> <observation YAML>
>
> ## Instructions
>
> 1. Read the existing code to understand current structure
> 2. Implement the change described in the proposal
> 3. Write tests if the change is testable
> 4. Run existing tests to make sure nothing broke
> 5. Commit with a descriptive message on a feature branch
> 6. Push the branch and open a PR via `gh pr create`
>
> For team-owned repos: always create a PR, never merge directly.
> For self-owned repos: create a PR for visibility.
>
> If the implementation is not feasible (missing dependencies, would break
> existing functionality, or the proposal is unclear), explain why and exit
> without making changes.
>
> IMPORTANT: Your work must result in a git commit and a PR URL. If you
> cannot create a PR, clearly state why in your response.

## Phase 6 — Verify and report

After all agents complete, for each result:

1. Check if the agent returned a PR URL
2. If yes: update the proposal YAML — set `status: implemented`, add `pr_url: <url>`
3. If no: update the proposal YAML — set `status: failed`, set `failure_reason`
4. If proposal was implemented, update the observation — set `status: addressed`

Write a run log to `~/.claude/canopy/runs/run-<ISO-timestamp>.yaml`:

```yaml
started: '<ISO>'
completed: '<ISO>'
transcripts_analyzed: N
observations_created: N
observations_merged: N
proposals_generated: N
proposals_implemented: N
proposals_failed: N
processed_sessions:
  - <session-id>
  - ...
errors: []
```

Show a final summary table:

| Proposal | Status | PR |
|----------|--------|----|

## Important notes

- The canopy state directory is `~/.claude/canopy/`
- Observations live in `~/.claude/canopy/observations/`
- Proposals live in `~/.claude/canopy/proposals/`
- Run logs live in `~/.claude/canopy/runs/`
- Use 12-char hex IDs (e.g., from `uuid4().hex[:12]` or generate manually)
- All YAML files use `default_flow_style: False` (block style)
- Dates are ISO format: `YYYY-MM-DD`
- Timestamps are ISO format with timezone: `YYYY-MM-DDTHH:MM:SS+00:00`
