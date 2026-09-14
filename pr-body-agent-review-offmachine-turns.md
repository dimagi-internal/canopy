## The hole

`agent-review` has three separate guards against reporting a failed run as a clean bill of health — the synthesis-pass warning, the `--json-output` warning, and `BLIND SCAN`. All three miss the same case, and it happens to be the case that hides an entire agent.

**echo is the one agent claimed by the cloud runner** (`cloud-ec2-1`). Its transcripts are written on that box and never land in any local `~/.claude/projects`. So the corpus is legitimately `whole-corpus` — every readable source *was* read — while attributing zero turns. `BLIND SCAN` can't catch it either: that guard fires on `considered > 0` with none attributed, and here nothing was ever a candidate.

Measured 2026-09-14, `canopy agent-review echo --hours 75`:

```
Agent: echo  (/Users/jjackson/emdash/repositories/echo)
Turns reviewed (last 75h): 0
  corpus: whole-corpus — local:acedimagi, local:jjackson
  tool failures: 0  •  gating blocks: 0  •  auth friction: 0  •  human corrections: 0

No findings synthesized.
```

The harness held **two completed echo turns in that window**, both on `cloud-ec2-1`, one with a full turn transcript in its `result_note`. Read literally, that output says echo is clean. It actually says echo is unreadable — so echo has been structurally invisible to the fleet's own self-improvement lens, silently, for as long as it has been on the cloud runner.

## The fix

When `turns == 0`, cross-check the harness for turns in the same window. If there are any, name the count and the runner(s) and say plainly that this is not a clean bill of health:

```
⚠  NOT A CLEAN BILL OF HEALTH — the harness recorded 2 turn(s) for this agent in the
window (1 on an unnamed runner, 1 on cloud-ec2-1), but none of their transcripts are
readable on this machine. Review them where that runner writes its sessions; this run
found nothing because it could not read them.
```

Best-effort and never fatal — an unreachable canopy-web loses the hint, not the review (offline runs, `--no-llm`).

## Verified

- **Live, on the real case**: the output above is the actual `canopy agent-review echo --hours 75 --no-llm` run after the change; before it, the same command printed `Turns reviewed: 0` with no warning.
- 6 new tests in `tests/test_agent_review.py` pinning the boundaries: names the runner; stays quiet for turns *outside* the window (a genuinely idle agent must still read as idle); stays quiet with no harness turns at all; skips the check entirely when `turns > 0`; survives an unreachable canopy-web with `exit_code == 0`.
- `tests/test_agent_review.py` + `tests/test_plugin_runtime_resolution.py` — **183 passed**.
- `canopy version verify` green at v0.2.491.

Not verified: the full suite (scoped to the two suites above); and this makes echo's invisibility *legible*, it does not make echo's cloud transcripts *readable* — reviewing them still needs a reader on that runner.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
