---
name: project-status
description: Re-entry survey for "where do I stand on this project?" — current branch, worktrees, open PRs, recent merges, stale branches. Read-only. Use when returning to a project after time away, or before risky operations.
---

# Project Status

Run this when you're returning to a project after a hiatus, picking up after
a context switch, or auditing a worktree before something risky. It answers:

- Where am I (worktree, branch, vs main)?
- What's uncommitted or stashed?
- What other worktrees are open?
- What PRs are still in flight?
- What landed recently?
- Which branches have gone stale?

The skill is **strictly read-only**. It runs only `git`, `gh pr list --json`,
and arithmetic on timestamps. Nothing here mutates state. Safe to run before
any other operation, in any directory, with no preconditions beyond "it's a
git repo."

## Process

1. Run the script:

   ```bash
   _CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
   CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
   bash "$CANOPY_ROOT/scripts/canopy-project-status.sh"
   ```

2. Read the output to the user verbatim — it's already formatted as a
   stakeholder-ready report. Don't summarize, paraphrase, or reorder
   sections; the user wants the survey, not your interpretation.

3. After printing, if anything looks load-bearing for the user's next move,
   surface it as one short follow-up:
   - **Many uncommitted changes** → suggest `/save` or asking what they're
     in the middle of.
   - **Branch is many commits behind main** → suggest pulling before new work.
   - **Stale branches with closed PRs** → offer to clean up if appropriate.
   - **An open PR matching the current branch** → mention its number so the
     user can link there directly.

4. **Do not auto-take any action.** This skill surveys; it never mutates.
   If the user wants to act on the report, they'll say so.

## When NOT to use this skill

- Not for one-off git inspection (just use `git status`).
- Not for "what's in this PR" (use `gh pr view`).
- Not for committing or pushing (use `/ship`, `/canopy:improve`, etc.).
- Not as part of an agent's pre-flight — it's user-facing context, not
  machine-readable health.
