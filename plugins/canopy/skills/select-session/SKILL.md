---
name: select-session
description: Menu-driven session picker — select a project, browse session history, analyze, and propose fixes
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# Select Session

Interactive menu-driven flow to pick a session, analyze it, and propose improvements.

## Arguments

- `hours` (optional): Time window for recent sessions. Default: 24. Example: `/select-session 72`

## Flow

### Step 1: Fetch session data

Run this command, substituting the hours argument (default 24):

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy sessions list --json-output --hours <HOURS>
```

Parse the JSON output. If no sessions found, tell the user and suggest increasing the hours window.

### Step 2: Project selection

Group sessions by their `repo` field (fall back to `project_key` if repo is null). Sort by most recent activity.

Present a numbered text menu:

```
Select a project:

  1  dimagi-internal/canopy    (5 sessions)
  2  jjackson/connect-labs           (3 sessions)
  3  dimagi/commcare-connect         (2 sessions)
  4  jjackson/connect-search         (2 sessions)
```

Wait for the user to pick a number.

### Step 3: Session selection

Show sessions for the chosen project, sorted newest-first.

**Before displaying, clean each `first_msg`:**

1. Strip wrapper blocks (in order): `<local-command-caveat>...</local-command-caveat>`, `<local-command-stdout>...</local-command-stdout>`, `<command-message>...</command-message>`, `<command-name>...</command-name>`, `<command-args>...</command-args>`, `<stdin>...</stdin>`
2. If the cleaned result contains a slash-command invocation (e.g. `/canopy:pm-scout`), prefix the display with `[<slash-command>] ` so the session's purpose is obvious
3. Collapse whitespace (replace newlines/tabs with single spaces, trim)
4. Truncate to 120 characters, appending `…` if truncated
5. If the cleaned result is empty after stripping, fall back to the raw `first_msg` truncated to 120 chars

**Format the list like this:**

```
dimagi-internal/canopy — recent sessions:

  1  [03-23 15:08]  (4 msgs)   I would like a way to quickly navigate between the worktrees in emdash and see which ones need…
  2  [03-23 14:25]  (88 msgs)  I think we are ready to test, is that your read? Can you walk me through the full pipeline…
  3  [03-23 14:17]  (2 msgs)   [/canopy:pm-scout] quick scout of the new proposal backlog
```

Keep message count before the text so the longer preview has room to breathe.

Wait for the user to pick a number.

### Step 4: Analyze and propose

Run the analyzer with `--propose` to get both observations and implementation suggestions:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy analyze --propose <PATH>
```

**Show the full output directly to the user** — do not summarize or hide it in a tool call. Present each observation with its severity, then each proposal with its implementation plan.

### Step 5: Disposition

After showing the analysis and proposals, present each proposal using `AskUserQuestion` so the user can decide what to do. Bundle up to 4 proposals into a single `AskUserQuestion` call (one question per proposal):

```
AskUserQuestion({
  questions: [
    {
      question: "Proposal: <title>\n\n<what/why/how summary>",
      header: "<short label>",
      options: [
        { label: "Implement", description: "Fix this now" },
        { label: "Backlog", description: "Good idea, not now" },
        { label: "Skip", description: "Not worth doing" }
      ],
      multiSelect: false
    }
    // ... one per proposal, up to 4
  ]
})
```

If there are more than 4 proposals, batch them into multiple `AskUserQuestion` calls.

### Step 6: Implement

For any proposals the user marked "Implement":

1. Identify the target repo from the session data
2. Tell the user what will be implemented and in which repo
3. Use the `canopy analyze` output as the implementation spec
4. Create a branch, implement the fix, and verify

If no proposals were marked for implementation, summarize what was backlogged/skipped and end.

## Rules

- Always invoke the orchestrator CLI via `uv run --project "$CANOPY_ROOT" canopy …`, resolving `CANOPY_ROOT` with the canonical two-liner at the top of each bash block (blocks don't share variables)
- Use plain text numbered menus for project and session selection (no item limit)
- Use `AskUserQuestion` only for proposal disposition (fixed 3 options per proposal)
- Show analysis and proposal output directly to the user, not hidden in tool results
- The full flow is: select → analyze → propose → disposition → implement
- If the user types `b` or `back`, go back to the previous menu
- If the user types `q` or `quit`, exit the flow
- Clean wrapper tags from first_msg and truncate to 120 characters in the session list (see Step 3)
- Keep the menus clean and minimal — no extra decoration
