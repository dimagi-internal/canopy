---
name: gslides-export
description: |
  Export a deck to Google Slides WITHOUT the claude.ai Google Drive connector — take the
  deck's .pptx (a Claude Slides artifact's own Download › PowerPoint, or any .pptx) and
  publish it as a native, editable Google Slides deck authored as the agent, filed under
  its Projects/<project> folder, shared, and read back to confirm every slide arrived.
  Use when asked to "export my deck to Google", "put this deck in Google Slides", "send
  the talk to Drive", or when a Slides artifact's own "send to Google Drive" export is
  unavailable (orgs can disable the connector it rides). Re-run it after each round of
  deck edits; it retires the previous export.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
```

If output shows `UPGRADE_AVAILABLE <old> <new>`, mention it once and continue.

# Export a deck to Google Slides

**Why this route.** A Claude Slides artifact can send itself to Google Drive, but that
button rides the claude.ai Google Drive connector, and a Workspace admin can switch
connectors off org-wide. The deck's **.pptx download** needs no connector, and it is the
Slides runtime's own editable export, so converting it is as faithful as Google's
PowerPoint import allows. `canopy gslides publish` does the rest as the agent's own
Google identity, via gog. Nothing goes through a claude.ai connector.

## 1. Get the .pptx — the one step a human does

You cannot press the deck's download button from a terminal, so ask once, plainly:

> In the deck, open **Share › Export** and download it as **PowerPoint (.pptx)**. If it
> offers an editable option and a pictures-only option, take the editable one. Tell me
> when it's downloaded.

Then find THAT file, not just some .pptx:

```bash
ls -t ~/Downloads/*.pptx 2>/dev/null | head -3
```

Take the newest file, and only if **(a)** it was modified in the last ~30 minutes and
**(b)** its name matches the deck's title (a Claude Slides deck names the file after the
`title` in its `project/deck.json`). Otherwise ask for the path. An older export of the same
deck looks right and is silently stale. A browser still writing the file leaves a
`.crdownload`/partial file; the engine refuses a truncated zip, so if it reports "is the
download finished?", wait and retry.

If the user already gave you a .pptx path, skip all of this.

## 2. Pick the destination and the previous export

- **Project folder:** the deck's existing project under your Drive root (search first;
  reuse it). Pass `--project "<Project>"`.
- **Previous export:** list that folder and look for a Google Slides file
  (`application/vnd.google-apps.presentation`) with the same name. Pass its id as
  `--supersede`. Drive cannot overwrite a native deck in place, so each export is a new
  file, and `--supersede` trashes the old one **only after** the new one verifies
  (recoverable from Drive's trash).

```bash
gog drive ls --parent <project-folder-id> -a <agent mailbox> --client "$(canopy email client --repo .)" --json \
  | jq -e '[.files[] | select(.mimeType=="application/vnd.google-apps.presentation") | {id,name,modifiedTime}]'
```

## 3. Publish

Run from the agent's repo (identity comes from its `config/agent.json`; else `--agent <slug>`):

```bash
uv run --project "$CANOPY_ROOT" canopy gslides publish --repo . \
  --pptx "$HOME/Downloads/<Deck title>.pptx" --project "<Project>" \
  --share domain [--supersede <previous deck id>] [--name "<title>"]
```

It converts, files, shares, then reads the deck back and compares slide counts. Exit 0
prints `{id, url, slides, pptx_slides, verified, superseded}`. **Exit 1 means do not hand
out the link yet**: either slides went missing in conversion (`degraded`) or the share did
not verify. Open it, fix, and re-run. When the new deck fails, the old export is kept.

`--share user --share-email <addr>` shares with one person instead of the domain.

## 4. Report

Give the requester the new `url` and say plainly that **the link changed** (every export is a
new file), plus `slides` = `pptx_slides`. Mention the known PowerPoint→Slides losses
**only if the deck uses them**:
- live embeds (`<x-embed>`) become static or vanish
- slide transitions and build-ins mostly drop
- a typeface Google Fonts doesn't serve is substituted

For a deck that is going on stage, ask them to flip through it once in Google Slides.
That's a reasonable request, not a hedge.

## What this skill is NOT

- Not a re-renderer. It ships the runtime's own .pptx; layout fidelity is Google's
  PowerPoint importer's. If a slide imports badly, the pictures-only .pptx export gives a
  pixel-faithful, non-editable deck through the same command.
- Not for a PDF. Drive cannot convert a PDF into Slides; the engine refuses one.
