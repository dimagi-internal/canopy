---
name: walkthrough-share
description: |
  Upload a walkthrough artifact (HTML slideshow or MP4 video) to a canopy-web
  instance so it can be shared via URL. Per-walkthrough visibility: private
  (dimagi-OAuth gate) or link (anyone with the token).
  Use when asked to "share this walkthrough", "upload to canopy-web",
  "make a share link for the walkthrough", or "walkthrough-share <path>".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`, mention it once and continue.

# Walkthrough Share

Uploads a finished walkthrough to canopy-web. HTML walkthroughs get their
relative screenshot/CSS references inlined as base64 data URIs before upload,
so the single file renders standalone inside the canopy-web viewer's iframe.
MP4 videos upload as-is.

The viewer page (`/walkthrough/<id>`) lets the owner toggle visibility, copy
the tokened share link, rotate it, or delete. Non-owners just see the player.

## Required state

- **PAT** at `~/.claude/canopy/workbench-token` (or `CANOPY_WEB_PAT` env
  var). Mint with `/canopy:canopy-web-pat-mint` — gh-style loopback flow,
  one click in the browser. The PAT identifies the human caller; uploaded
  walkthroughs are owned by whoever minted the token, no separate `--as`
  flag needed.
- **Canopy-web reachability**: defaults to the production deploy. Override
  with `CANOPY_WEB_API_URL` env var (e.g. for local dev against
  `http://localhost:8000`).

## Modes

First resolve the uploader from the canopy runtime (resolved via
`scripts/canopy-runtime.sh`). Run this in the same shell as the command
you pick below (Claude Code starts a fresh shell per block — re-run it if you
split blocks):

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
UPLOAD="$CANOPY_ROOT/scripts/walkthrough-share/upload.py"
```

```bash
# Upload (private — only visible to logged-in dimagi users)
uv run --project "$CANOPY_ROOT" python "$UPLOAD" \
  screenshots/walkthroughs/my-demo.html \
  --title "My Demo" \
  --project canopy-web

# Upload + mint a share link (anyone with the URL can view)
uv run --project "$CANOPY_ROOT" python "$UPLOAD" \
  screenshots/walkthroughs/my-demo.html \
  --title "My Demo" \
  --project canopy-web \
  --public

# Upload a video (kind auto-detected from .mp4 extension)
uv run --project "$CANOPY_ROOT" python "$UPLOAD" \
  screenshots/walkthroughs/my-demo.mp4 \
  --public

# Upload a video WITH companion links the /walkthrough/<id> viewer renders:
# - back to the narrative that generated it
# - the still-frame (deck) version of the same demo
# - the app pages the demo walked through (one per scene url in the spec)
uv run --project "$CANOPY_ROOT" python "$UPLOAD" \
  screenshots/walkthroughs/my-demo.mp4 \
  --public \
  --narrative-url "https://canopy-web.../review/42/?t=abc" \
  --companion-url "https://canopy-web.../walkthrough/<deck-uuid>?t=def" \
  --spec docs/walkthroughs/my-demo.yaml \
  --link "Connect microplanning::https://connect.dimagi.com/microplanning"
```

The script prints:

```
inlining HTML assets from <dir>…
uploading <N> MB to <api>…
attaching <N> companion link(s)   # only when links are passed
View: <api>/walkthrough/<uuid>
Share: <api>/walkthrough/<uuid>?t=<token>   # only with --public (the server-returned share_url)
```

Pass the `Share:` line back to the user verbatim.

## Companion links (the `/walkthrough/<id>` viewer panels)

The viewer page renders attached links in two panels under the player:

- **This walkthrough** — `narrative` + `companion` links. Use these to send a
  viewer back to the story that generated the demo and to the sibling artifact
  (the still-frame deck for a video, or the video for a deck).
- **Explore in the app** — `reference` links: the destinations the demo
  visited, clickable and live so the viewer can go try them.

The DDD loop (`/canopy:ddd-run`) attaches these automatically when it uploads
each iteration's clip. For a one-off upload, pass them yourself with the flags
below.

## Argument map

| Flag | Required | Notes |
|---|---|---|
| `<path>` (positional) | yes | `.html`, `.htm`, or `.mp4`. Other types rejected. |
| `--title <str>` | no | Defaults to filename stem. Max 200 chars on the server. |
| `--description <str>` | no | Optional long-form description. |
| `--project <slug>` | no | Links the walkthrough to a canopy-web project tile. Must match an existing slug (no server-side validation today; bad slug = no link, no error). |
| `--public` | no | Sets visibility=link and prints a `?t=<token>` URL. Default is private. |
| `--narrative-url <url>` | no | "Back to the narrative" link (kind=narrative). Label via `--narrative-label`. |
| `--companion-url <url>` | no | Sibling artifact (kind=companion). Label defaults by kind: "Still-frame walkthrough" for a video, "Watch the video" for a deck. Override with `--companion-label`. |
| `--link "Label::url"` | no | A reference link (kind=reference). Repeatable. |
| `--spec <path>` | no | Walkthrough spec YAML — derives one reference link per scene `url` (label = scene title, deduped). Imports pyyaml lazily. |
| `--api-url <url>` | no | Override canopy-web base URL (also via `CANOPY_WEB_API_URL`). |

Reference links from `--link` and `--spec` are merged and de-duplicated by URL.
Malformed input fails loud: a bad `--link` (no `::`) or a server-rejected link
(missing url, etc.) exits non-zero rather than silently dropping.

## First-time setup

If `~/.claude/canopy/workbench-token` doesn't exist (or the token is expired),
mint one:

```
/canopy:canopy-web-pat-mint
```

That command opens your browser to canopy-web's authorize page (signing you
in via Google if needed), captures the minted PAT on a one-shot localhost
listener, and writes it to `~/.claude/canopy/workbench-token` (chmod 600).
Same token is used by the post-tool-use hook and `/canopy:canopy-doctor`.

## Error handling

The script exits non-zero with a one-line `error: ...` message on stderr:

- Missing PAT → tells you to run `/canopy:canopy-web-pat-mint`.
- HTTP 401 → PAT is expired or revoked. Re-mint via the slash command.
- HTTP 500 with `Drive not configured` → canopy-web is missing
  `CANOPY_DRIVE_SA_KEY_JSON` / `CANOPY_DRIVE_ROOT_FOLDER_ID`. Deployment
  problem, not a client problem.
- HTTP 413 → file exceeds `WALKTHROUGH_MAX_UPLOAD_BYTES` (default 75 MB on
  the server). For HTML, shrink the screenshots before re-running.

## After uploading

If you uploaded with `--public`, paste the `Share:` URL into the channel /
DM you want to share it in. The URL is unguessable — but treat it like any
other unlisted-link share (don't paste in public unless you mean it).

To revoke a share link without deleting the walkthrough, visit the viewer
page at `/walkthrough/<uuid>` and click "Rotate link" — the old URL stops working
immediately.

## Used by

- `/canopy:walkthrough` — at the end of a successful run, optionally calls
  this skill to publish the deck.
