---
name: alignment
description: Compare two sibling systems for drift and surface what one built that the other should bring over or reconcile. Read-only — posts ranked, reasoned findings to the canopy-web /insights feed as [alignment] cards. Use when asked to "align two projects", "check alignment between X and Y", "what's diverged between ace and canopy", or "alignment <projectA> <projectB>".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`, mention the upgrade once and continue.

# Alignment

Compares **two** sibling systems passed as arguments — e.g. the `ace` and
`canopy` plugins, or the `ace-web` and `canopy-web` apps — across four axes,
reasons case-by-case about which side (if any) should be the reference, and
posts ranked findings to the canopy-web **/insights** feed as `[alignment]`
cards.

**Read-only.** It reads both repos and writes only insight cards to canopy-web.
It never edits, commits, or opens PRs against either compared repo.

## Inputs

Two project **slugs**, passed at invocation: `alignment <projectA> <projectB>`.

- If **fewer than two** slugs were provided, STOP and ask the user which two
  repos to align. Do not guess or default to a pair.
- Resolve each slug to a local path by checking these bases in order and taking
  the first that exists: `~/emdash/repositories/<slug>`, then
  `~/emdash-projects/<slug>`.

Set shell variables once, substituting the actual slugs the user gave (do NOT
use `$1`/`$2` — slash-command expansion strips them):

```bash
A=ace        # <-- replace with projectA slug
B=canopy     # <-- replace with projectB slug

resolve() {
  for base in "$HOME/emdash/repositories" "$HOME/emdash-projects"; do
    if [ -d "$base/$1/.git" ]; then echo "$base/$1"; return 0; fi
  done
  return 1
}
A_PATH=$(resolve "$A") || { echo "ERROR: cannot resolve repo for slug '$A'"; exit 1; }
B_PATH=$(resolve "$B") || { echo "ERROR: cannot resolve repo for slug '$B'"; exit 1; }
echo "A=$A -> $A_PATH"
echo "B=$B -> $B_PATH"
```

(`resolve` takes a real positional arg inside a standalone function body, which
is fine — the `$1`/`$2` ban only applies to slash-command argument expansion,
not to bash functions you define and call yourself.)

## Step 1 — sanity check

```bash
TOKEN_FILE=~/.claude/canopy/workbench-token
test -s "$TOKEN_FILE" || { echo "ERROR: $TOKEN_FILE missing or empty"; exit 1; }
CANOPY_WEB="${CANOPY_WEB_API_URL:-https://labs.connect.dimagi.com/canopy}"
curl -s -o /dev/null -w "%{http_code}\n" "$CANOPY_WEB/health/" --max-time 8
```

Expect `200`. Otherwise stop.

## Step 2 — clear stale alignment cards

Insights are an inbox. Clear the previous run's `canopy:alignment` cards so old
or no-longer-true findings don't pile up.

**Preferred — the canopy-web MCP `clear_insights` tool.** The canopy plugin
registers the canopy-web MCP server (Streamable HTTP, per-user PAT auth), so when
it's connected this session call the tool
`mcp__plugin_canopy_canopy-web__clear_insights` with:

```json
{ "source": "canopy:alignment" }
```

(args are `source` / `category` / `project` / `older_than_days`, all optional).
It returns `{ "cleared": N }` and runs as your authenticated user. This is the
canonical path — one typed contract, no hand-maintained URL/verb to drift (the
reason an earlier `DELETE` silently no-op'd).

**Fallback — REST POST** (only if the canopy-web MCP tool isn't available this
session):

```bash
TOKEN=$(cat ~/.claude/canopy/workbench-token)
CLEAR_HTTP=$(curl -s -o /tmp/alignment-clear.json -w "%{http_code}" -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data '{"source":"canopy:alignment"}' \
  "$CANOPY_WEB/api/insights/clear/" --max-time 10)
[ "$CLEAR_HTTP" = "200" ] && echo "cleared: $(head -c 120 /tmp/alignment-clear.json)" \
  || echo "WARN: clear returned HTTP $CLEAR_HTTP — skipping; new cards pile alongside old (user can dismiss)."
```

The clear endpoint takes a JSON **body** (`{source?, category?, project?,
older_than_days?}`); filters AND-combine and an empty body clears all, so always
send `{"source":"canopy:alignment"}` to scope deletion to this skill's cards.
Clearing is best-effort: on any non-200 the skill warns and continues.

## Step 3 — dispatch ONE comparison subagent

Use the **Agent tool** (general-purpose) to do the heavy repo reading and
reasoning in an isolated context. Pass it `$A`, `$B`, `$A_PATH`, `$B_PATH`. The
agent's prompt MUST instruct it to:

> Compare two sibling codebases for **alignment drift**. Read selectively — entry
> points, command/skill manifests, `lib/` and utility modules, CLAUDE.md, and
> recent git history (`git log --oneline -30`, `git log -1 --format=%cd <area>`)
> — not exhaustively.
>
> Walk all four **axes** and find places where the two systems are out of
> alignment:
> 1. **features** — a whole capability one side has and the other lacks.
> 2. **patterns** — the same problem solved differently (error handling, config
>    layout, naming, directory structure, version/release discipline).
> 3. **shared-code** — near-duplicate helper logic that has drifted; a candidate
>    for a shared lib or at least matching implementations.
> 4. **docs-ux** — differences in how each is documented, onboarded, or
>    presented (CLAUDE.md conventions, command help text, web UI patterns).
>
> For **each** divergence, decide **case-by-case** which side should be the
> reference and WHY — newer, more complete, better-tested, simpler, already the
> documented standard — or state explicitly that there is **no clear winner** and
> the action is to reconcile. Do NOT apply a blanket rule (e.g. "newest wins").
> Think critically about each one.
>
> Skip non-divergences. If the two systems are well-aligned on an axis, return
> nothing for it. Quality over volume.
>
> Return a YAML list of findings, one per divergence, each with:
> `axis`, `reference` (slug or "none — reconcile"), `lagging` (slug),
> `reasoning`, `evidence` (a handle on BOTH sides: file path / command name /
> commit), `recency` (last-touched date of the affected area, YYYY-MM-DD), and
> `card` (one sentence, see card rules below).

## Step 4 — rank

Order the returned findings by **recency** (most-recently-touched area first)
then by impact. Recent work floats to the top.

## Step 5 — post each card

For each finding, post to the **lagging** side's slug (the repo that should
adopt or that owns the reconcile). If `reference` is `none — reconcile`, post to
whichever side the user is more likely acting on (default: `$A`).

Each card's `content` is the finding's `card` sentence prefixed with
`[alignment]`. **Card rules** (these keep the feed sharp):

1. **One claim per card.** Multiple claims → multiple cards.
2. **Cite the handle on BOTH sides** — file path, command name, commit SHA.
3. **Action verb at the end** — "adopt", "reconcile", "extract", "standardize on".
4. **One sentence.** Hard cap.
5. **Empty is allowed.** Zero findings is a valid, good result.

```bash
# LINE is the full card text INCLUDING the [alignment] prefix.
# SLUG is the lagging side's slug.
PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'context_type':'insight','content':sys.argv[1],'source':'canopy:alignment'}))" "$LINE")

HTTP=$(curl -s -o /tmp/alignment-resp.json -w "%{http_code}" \
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
  echo "✗ [$SLUG] HTTP $HTTP — $(cat /tmp/alignment-resp.json | head -c 200)"
fi
```

**Stop on the first 401** — don't burn through every card re-trying; tell the
user the token is bad. A non-201/401 (e.g. 404) usually means the slug isn't a
curated project on canopy-web — report the HTTP code rather than dropping it
silently.

## Step 6 — summary

```
Alignment sweep complete: <A> ↔ <B>
  cards posted: <N>
  findings by axis: features=<n> patterns=<n> shared-code=<n> docs-ux=<n>
  failed: <list with HTTP codes>

View at: <CANOPY_WEB>/insights
```

## Rules

- **Token never echoed.** Never print `$TOKEN`.
- **Read-only on the compared repos.** No edits, commits, or PRs against `$A`/`$B`.
- **POST, not PUT.** Insight cards are append-only context entries.
- **Exactly two projects per run.** Not a whole-portfolio sweep — for that, use
  `/canopy:portfolio-review`.

## When NOT to use

- For one feed of categorized insights across ALL projects, use
  `/canopy:portfolio-review`.
- For a strategic narrative across recent activity, use `/canopy:brief`.
- For cross-session friction patterns, use `/canopy:patterns`.
