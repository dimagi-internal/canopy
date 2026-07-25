---
name: portfolio-guide
description: Generate AI "what to do next" guidance per project and upload it to canopy-web for browser viewing. Run when the user wants a fresh portfolio sweep across all their repos.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`, mention the upgrade once and continue.

# Portfolio Guide

For each active project, generates a 200-400 word markdown guide answering **"what could the user be doing next on this project?"** and uploads it to canopy-web. The guide is then viewable at `https://labs.connect.dimagi.com/canopy/projects/<slug>/guide`.

## Required state

- **Token file:** `~/.claude/canopy/workbench-token` must exist and be non-empty. If missing, stop and tell the user to mint one (it should already be there from earlier hook setup).
- **Canopy-web reachability:** the deploy URL must respond. The default is `https://labs.connect.dimagi.com/canopy`; override with the `CANOPY_WEB_API_URL` env var.

## Step 1 — sanity check

```bash
TOKEN_FILE=~/.claude/canopy/workbench-token
test -s "$TOKEN_FILE" || { echo "ERROR: $TOKEN_FILE missing or empty"; exit 1; }
CANOPY_WEB="${CANOPY_WEB_API_URL:-https://labs.connect.dimagi.com/canopy}"
curl -s -o /dev/null -w "%{http_code}\n" "$CANOPY_WEB/health/" --max-time 8
```

Expect `200`. If anything else, stop and report.

## Step 2 — fetch the curated project list from canopy-web

canopy-web is the source of truth — never hardcode the list locally.

```bash
TOKEN=$(cat ~/.claude/canopy/workbench-token)
SLUGS_JSON=$(curl -s -H "Authorization: Bearer $TOKEN" "$CANOPY_WEB/api/projects/slugs/" --max-time 10)
echo "$SLUGS_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(p['slug'] for p in d))"
```

That prints the slugs of every **active** project canopy-web has been seeded with. If the user passed a specific slug as an argument, use only that one (still verify it's in the list).

For each slug, check whether a local repo exists at `~/emdash-projects/<slug>`. Note ones without a local `.git` directory in the final summary as "no local repo" — don't skip them silently, but don't try to gather signals either.

## Step 3 — generate and upload, one project at a time

For each candidate slug, run the **Per-project flow** below sequentially. Do **not** dispatch parallel agents for a first run — keep it serial so the user can watch and abort.

### Per-project flow

**3a. Gather signals (read-only, ~2 seconds total):**

```bash
SLUG=<current slug>
REPO=~/emdash-projects/$SLUG
cd "$REPO"

echo "=== branch ==="
git status -sb 2>&1 | head -3
echo "=== recent commits ==="
git log --oneline -10 2>&1
echo "=== open PRs ==="
gh pr list --json number,title,headRefName,updatedAt --limit 5 2>&1 || echo "(no gh access)"
echo "=== uncommitted ==="
git status --short 2>&1 | head -20
```

**3b. Synthesize the guide.** Take the gathered signals and write 200-400 words of markdown. Structure:

```
# Next up on <slug>

**Current state:** one sentence on branch + WIP.

## Recommended next moves

1. **<concrete action>** — why it matters, what file/PR/branch to touch.
2. **<concrete action>** — ...
3. **<concrete action>** — ...

## Watch out for

- One or two real risks visible from the signals (stale branch, unmerged PR, etc.).
```

Rules for the guide:
- **Be specific.** "Address PR #14 review comments" beats "review open PRs."
- **Cite handles.** PR numbers, branch names, commit SHAs, file paths.
- **No filler.** Skip generic advice ("write tests", "improve docs") unless the signals point to it.
- **Honest "I don't see much":** if the repo has no recent activity and no open PRs, say that — recommend either archive or a single re-engagement move.

**3c. Upload.** Write the markdown to a temp file, then PUT:

```bash
GUIDE_FILE=$(mktemp)
cat > "$GUIDE_FILE" <<'EOF'
<the markdown you just synthesized>
EOF

TOKEN=$(cat ~/.claude/canopy/workbench-token)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'content': open(sys.argv[1]).read(), 'source': f'canopy:portfolio-guide@{sys.argv[2]}'}))" "$GUIDE_FILE" "$TS")

HTTP=$(curl -s -o /tmp/guide-resp.json -w "%{http_code}" \
  -X PUT \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "$PAYLOAD" \
  "$CANOPY_WEB/api/projects/$SLUG/guide/" \
  --max-time 15)

rm -f "$GUIDE_FILE"

if [ "$HTTP" = "200" ]; then
  echo "✓ $SLUG → $CANOPY_WEB/projects/$SLUG/guide"
elif [ "$HTTP" = "404" ]; then
  echo "⊘ $SLUG (not seeded on canopy-web — skipping)"
else
  echo "✗ $SLUG → HTTP $HTTP: $(cat /tmp/guide-resp.json)"
fi
```

## Step 4 — final summary

Print:

```
Portfolio guide run complete.
  uploaded: <N>
  no local repo: <M> (slugs)
  not seeded on canopy-web: <K> (slugs)
  failed: <L> (slugs + status codes)

View at: <CANOPY_WEB>/
```

Each `<slug>` link in the per-project output goes directly to the guide page; the user can click through.

## Rules

- **Token never echoed.** Never print `$TOKEN` or include it in any output.
- **PUT, not POST.** The guide endpoint uses PUT (upsert). POST will 405.
- **Stop on auth failure.** If the very first PUT returns 401, stop the loop and tell the user the token is rejected — don't burn through 12 more attempts.
- **Don't push or commit anything.** This skill only reads local repos and writes to canopy-web. No mutations to local state.

## When NOT to use

- For a single project's status, use `/canopy:project-status` (it's read-only and faster).
- For a strategic narrative across all activity, use `/canopy:brief`.
- For "what changed in the last week," use `/canopy:patterns`.
