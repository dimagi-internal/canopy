---
name: ddd-video-improve
description: >
  Autonomous video-improvement loop for a connect-ddd-walkthrough. Renders the
  video, gates cheap (ddd-timing-eval), judges it multimodally (ddd-video-judge),
  AUTO-APPLIES the safe engine/render fixes the judge routes, re-renders, and
  re-judges — keeping a change only if the score improves (keep-if-better). Risky
  fixes (re-record, narrative reorder, product/app changes) are SURFACED, never
  silently applied. Runs on demand, parallel to the product loop — it does NOT
  gate the fast screenshot loop. Use to iteratively improve a rendered narrative
  without approving each edit by hand.
---

# DDD video-improvement loop

The video counterpart to the product loop: render → judge → fix → re-render →
converge, **autonomous** (no per-edit approval) but **safe** (keep-if-better +
route-based auto-apply policy).

```
render (render.ts) ─► verdict-timing.json  ── gate: timing/overrun below floor? → fix render first
       │  (cheap, deterministic)
       ▼ pass
build packets (scripts/ddd/video_judge.py) ─► per-scene montages
       ▼
multimodal judge (canopy:ddd-video-judge) ─► verdict-video.json {scores, findings[route]}
       ▼
route findings ──┬─ AUTO-APPLY (re-render only, reversible, our code): RENDER + engine
                 └─ SURFACE (need a human / external action): PRODUCT, FOOTAGE, NARRATION
       ▼
re-render ─► re-judge ─► keep change iff overall improved, else revert
       ▼
stop at: N consecutive non-improving rounds, max-iter, or all-scenes ≥ target
```

## Auto-apply policy (what the loop changes by itself)

ONLY fixes that are (a) in canopy's own code, (b) verified by re-render+re-judge,
and (c) reversible:

- **RENDER / engine** — e.g. anchor warp marks on the action's EFFECT not its
  start (`snippets._REVEAL_KINDS`), excise leaked "Loading…" frames in de-dwell,
  trim a dead intro hold (dead-air cap). Apply → re-render → re-judge → keep iff
  overall went up (and no scene regressed). This is the autonomous core.

NEVER auto-applied — surfaced as findings for a human:

- **PRODUCT** — the live labs app (a clipped Award column, a confusing-in-motion
  control). Route to the product loop; changing the app is not a video edit.
- **FOOTAGE** — needs a re-record (expensive, flaky); propose the spec/action
  change and let a human trigger it.
- **NARRATION** — the narrative is human-approved (`narrative_locked`). Reordering
  what a scene *says* changes the story; propose the edit, don't silently rewrite.

## Procedure

0. **Convergence gate.** Refuse unless the narrative's DDD run converged —
   the video films the product as it is now, and a pre-convergence video
   measures a product the concept loop is still changing:
   ```bash
   _CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
   DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
   export DDD_DIR="${DDD_DIR:-$(git rev-parse --show-toplevel)/.canopy/ddd}"   # the PROJECT repo's runs, not the runtime's
   (cd "$DDD_REPO" && uv run python -m scripts.ddd.video_gate --slug "<narrative-slug>") || exit 1
   ```
   `--allow-unconverged` is the explicit override for a deliberate preview only.
1. **Baseline.** Render + `ddd-video-judge`; record `verdict-video.json` overall.
2. **Gate.** If `verdict-timing` coverage is low only because of NARRATION-order
   inversions, that's a surface item, not a blocker. If the render's held-frame
   overrun is large, surface a FOOTAGE/NARRATION item.
3. **Pick a candidate auto-fix** from the RENDER/engine findings (highest-value,
   most-confident first).
4. **Apply → re-render → re-judge.** Compare overall + per-scene. Keep iff
   improved with no scene regressing below its prior verdict; else `git checkout`
   the change.
5. **Loop** until no improving auto-fix remains (then report the surfaced
   PRODUCT/FOOTAGE/NARRATION queue for human action), or max-iter.

## Notes
- Keep-if-better needs a stable judge. Per-video judging has run-to-run variance;
  for a tight gate use per-scene fresh-context judging and/or average two passes.
- The loop is report-only toward the product/narrative; its autonomy is bounded to
  canopy's own render path on purpose.
