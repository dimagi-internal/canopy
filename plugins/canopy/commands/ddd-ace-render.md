---
description: Render a DDD narrative as a narrated connect-ddd-walkthrough video on demand — record a fresh master clip, emit the explainer spec, hand off to the local ace renderer, and attach the video to the narrative's current version on canopy-web. Not the auto loop. Pass --no-upload to render locally without publishing.
argument-hint: <narrative-slug> [--run=<run-NNN>] [--no-upload]
allowed-tools: [Read, Write, Bash, Skill]
---

# /canopy:ddd-ace-render

Turn one DDD narrative into a narrated `connect-ddd-walkthrough` MP4:
**record** a fresh master clip, **emit** the explainer spec (canopy),
**hand off** to the local ace renderer (`/ace:video-render-local`), and
**attach** the video to the narrative's current version on canopy-web. On
demand — not part of `/canopy:ddd-run`. The upload runs by default; pass
`--no-upload` to render locally without publishing.

## Arguments
- `<narrative-slug>` — the narrative whose spec lives at `docs/walkthroughs/<slug>.yaml` in the current project repo.
- `--run=<run-NNN>` — optional connect-videos run id for the staged program (default `run-001`).
- `--no-upload` — render locally but skip attaching the mp4 to the narrative's current version on canopy-web. Upload (pinned via `narrative_review_id`) is the default.

## Process

Read the ddd-ace-render SKILL.md and follow it step by step:

```bash
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'] + '/skills/ddd-ace-render/SKILL.md')"
```

Read that file with the Read tool and follow it: resolve the spec + canopy
scripts, record a fresh master clip, emit the explainer spec via
`scripts.ddd.snippets explainer-from-capture`, invoke `/ace:video-render-local`
(Mode A) with the emitted spec + clip, then — unless `--no-upload` — attach the
rendered mp4 to the narrative's current version via
`scripts.ddd.snippets upload-video` with `--spec`, `--master`, `--report` and
`--manifest`, so the recipe and master are kept on canopy-web with it. Report the output MP4 path, the narrative
URL, and the timing report; flag a large held-frame overrun so the user can
trim the narration before shipping.
