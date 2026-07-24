---
name: portfolio-review
description: Sweep all curated projects on canopy-web and produce categorized insight one-liners (ship_gap, hygiene, pattern, stale, opportunity). Output goes to the /insights feed at canopy-web.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`, mention the upgrade once and continue.

# Portfolio Review

For each active project on canopy-web, produces 2–5 short categorized insights and POSTs them as `ProjectContext` rows with `context_type=insight`. The **/insights** page on canopy-web aggregates them across all projects with category badges, dismiss buttons, and project links.

## Why this shape

The insight cards on the /insights feed must do one job each: surface one specific, actionable observation with a category and evidence. Long-form prose belongs in CLAUDE.md or a project's docs, not in this feed. **If you're tempted to write a paragraph, you're writing the wrong thing — split it into multiple tagged insights or drop it.**

## Required state

- **Token file:** `~/.claude/canopy/workbench-token` must exist and be non-empty.
- **Canopy-web reachability:** `https://labs.connect.dimagi.com/canopy/health/` returns 200. Override with `CANOPY_WEB_API_URL` env var.

## Step 1 — sanity check

```bash
TOKEN_FILE=~/.claude/canopy/workbench-token
test -s "$TOKEN_FILE" || { echo "ERROR: $TOKEN_FILE missing or empty"; exit 1; }
CANOPY_WEB="${CANOPY_WEB_API_URL:-https://labs.connect.dimagi.com/canopy}"
curl -s -o /dev/null -w "%{http_code}\n" "$CANOPY_WEB/health/" --max-time 8
```

Expect `200`. Otherwise stop.

## Step 2 — fetch the curated project list from canopy-web

canopy-web is the source of truth — never hardcode the list locally.

```bash
TOKEN=$(cat ~/.claude/canopy/workbench-token)
curl -s -H "Authorization: Bearer $TOKEN" "$CANOPY_WEB/api/projects/slugs/" --max-time 10 \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(p['slug'] for p in d))"
```

That prints the slugs of every active project. If the user passed a specific slug as an argument, use only that one (verify it's in the list).

## Step 3 — clear stale insights from this source

Insights are an inbox. Before generating fresh ones, clear the previous run's output so old, dismissed, or no-longer-true cards don't pile up.

**Preferred — the canopy-web MCP `clear_insights` tool.** When the canopy-web MCP server (registered by the canopy plugin; Streamable HTTP, per-user PAT auth) is connected this session, call `mcp__plugin_canopy_canopy-web__clear_insights` with `{ "source": "canopy:portfolio-review" }` (args `source`/`category`/`project`/`older_than_days`, all optional); it returns `{ "cleared": N }` and runs as your authenticated user. One typed contract — no hand-maintained URL/verb to drift.

**Fallback — REST POST** (only if the canopy-web MCP tool isn't available this session):

```bash
CLEAR_HTTP=$(curl -s -o /tmp/pr-clear.json -w "%{http_code}" -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data '{"source":"canopy:portfolio-review"}' \
  "$CANOPY_WEB/api/insights/clear/" --max-time 10)
[ "$CLEAR_HTTP" = "200" ] && echo "cleared: $(head -c 120 /tmp/pr-clear.json)" \
  || echo "WARN: clear returned HTTP $CLEAR_HTTP — skipping; new insights pile alongside old (user can dismiss)."
```

The clear endpoint takes a JSON **body** (`{source?, category?, project?, older_than_days?}`); filters AND-combine and an empty body clears all, so always send `{"source":"canopy:portfolio-review"}` to scope deletion to this skill's cards. Clearing is best-effort: on any non-200 the step warns and continues.

## Step 4 — for each slug, gather + classify + post

### 4a. Skip slugs without a local repo

```bash
REPO=~/emdash-projects/$SLUG
if [ ! -d "$REPO/.git" ]; then
  echo "⊘ $SLUG (no local repo at $REPO)"
  continue
fi
```

Note in the final summary which slugs had no local repo.

### 4b. Gather signals (read-only, ~2 seconds total)

```bash
cd "$REPO"
echo "=== branch ==="
git status -sb 2>&1 | head -3
echo "=== last 12 commits ==="
git log --oneline -12 2>&1
echo "=== open PRs ==="
gh pr list --json number,title,headRefName,updatedAt,isDraft --limit 8 2>&1 | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if not d: print('(none)')
    for p in d:
        print(f\"#{p['number']} [{'DRAFT' if p['isDraft'] else 'open'}] {p['title']} (branch={p['headRefName']}, updated={p['updatedAt']})\")
except Exception as e:
    print(f'(gh error: {e})')
"
echo "=== uncommitted (top 15) ==="
git status --short 2>&1 | head -15
echo "=== recent branches ==="
git for-each-ref --sort=-committerdate --count=6 --format='%(committerdate:short) %(refname:short)' refs/heads/ 2>&1
```

### 4c. Classify into 2–5 insights

Read the gathered signals. For each thing worth flagging, pick **one** category and write **one sentence** with evidence and an action.

**Categories** (pick the best fit):

- `[ship_gap]` — Commits or merged work that haven't shipped. Evidence: N commits ahead of last deploy, CI green, no open release PR. Action: deploy or open release.
- `[hygiene]` — A repeatable maintenance action is overdue. Evidence: doc-regen N days stale, unmerged PRs sitting >2 weeks, tests not run since X. Action: run the skill or land the PR.
- `[pattern]` — A pattern observable in this project that's worth flagging — duplicated code, divergent approach across repos, brittle structure. Evidence: cite the file/branch/commit. Action: a one-line "consider X."
- `[stale]` — A branch, PR, or whole project has been quiet long enough to need a decision: revive, archive, or close. Evidence: last commit / PR update date.
- `[opportunity]` — Something this project could borrow or share with another project, or a low-effort high-leverage move that the signals make obvious. Evidence: cite both sides where relevant.

**Rules** (read carefully — the insights feed lives or dies on these):

1. **One claim per insight.** Multiple claims = multiple insights. Resist combining.
2. **Cite the handle.** Branch name, PR number, commit SHA, file path, deploy date. Insights without evidence are noise.
3. **Action verb at the end.** "Ship", "merge", "rebase", "delete", "consider", "audit", "tag." If you can't end with an action, you're probably writing a status update — drop it.
4. **One sentence.** Hard cap. If it doesn't fit, you have either too much rope or too little signal.
5. **Skip the obvious.** "Has open PRs" is not an insight. "PR #432 open since 2026-04-16, two weeks stale, blocks the Phase 10 release" is.
6. **Empty is allowed.** If a project has nothing worth flagging, generate zero insights for it. Don't pad.

**Format** for each insight:

```
[category] one sentence ending with an action verb
```

Examples (good):

- `[ship_gap] ace-web has 6 commits ahead of last deploy on Apr 8, including the AWS migration (#9); ship to verify nginx sidecar in prod.`
- `[stale] commcare-ios PR #432 (biometric login + Connect API + nav drawer) sitting since 2026-04-16; split the bundled scope or close.`
- `[hygiene] canopy has 5 emdash worktrees from 2026-04-27 still around; prune or land the work.`
- `[opportunity] connect-search just shipped a Drive-folder AI Context paradigm; ace-web could adopt the same shape for its credential-upload flow.`

### 4d. POST each insight

For each insight string `LINE`:

```bash
PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'context_type':'insight','content':sys.argv[1],'source':'canopy:portfolio-review'}))" "$LINE")

HTTP=$(curl -s -o /tmp/insight-resp.json -w "%{http_code}" \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "$PAYLOAD" \
  "$CANOPY_WEB/api/projects/$SLUG/context/" \
  --max-time 15)

if [ "$HTTP" = "201" ]; then
  echo "✓ [$SLUG] $LINE"
elif [ "$HTTP" = "401" ]; then
  echo "✗ STOP: bearer token rejected on first POST. Fix token before continuing."
  exit 1
else
  echo "✗ [$SLUG] HTTP $HTTP — $(cat /tmp/insight-resp.json | head -c 200)"
fi
```

**Stop on first 401.** Don't burn through every project re-trying. Tell the user the token is bad.

## Step 5 — final summary

```
Portfolio review complete.
  insights posted: <N>
  projects with no insights flagged: <list>
  no local repo (skipped): <list>
  failed: <list with HTTP codes>

View at: <CANOPY_WEB>/insights
```

## Rules

- **Token never echoed.** Never print `$TOKEN` or include it in any output.
- **POST, not PUT.** Insights are append-only context entries; `/api/projects/<slug>/context/` is POST.
- **Don't push or commit anything.** This skill only reads local repos and writes context entries to canopy-web.
- **Quality > volume.** A project with 1 sharp insight is better than the same project with 4 mediocre ones.

## When NOT to use

- For deep per-project orientation, use `/canopy:project-status` (it's read-only and faster).
- For a strategic narrative across all activity, use `/canopy:brief`.
- For "what changed in the last week," use `/canopy:patterns`.
