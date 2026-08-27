# Canopy

Canopy is an autonomous self-improving system and Claude Code plugin. It watches
your Claude Code sessions across every project, identifies friction and capability
gaps, and ships improvements — to MCP servers, skills, hooks, workflows, CLAUDE.md
docs, or the orchestrator itself. It runs as a CLI (`canopy ...`) backed by a
Python orchestration pipeline, and as a Claude Code plugin that surfaces the same
machinery through skills, slash commands, and agents.

## What it does

Canopy closes a loop:

1. **Observe** — a Claude Code hook (`hooks/post_tool_use.py`) captures a session
   log and repo mappings as you work. The orchestrator parses transcripts and
   extracts observations (friction, gaps, recurring patterns).
2. **Propose** — observations are turned into improvement proposals, each with a
   verification plan. Proposals are checked against the existing skill catalog so
   canopy doesn't propose building something it already ships.
3. **Implement** — proposals are implemented via headless `claude -p` runs in the
   target repos, gated by a circuit breaker and rate limiter, and shipped as PRs.

It also generates strategic briefs, cross-session friction patterns, and a
browser UI for inspecting transcripts.

## Install / setup

> **New here, and your goal is to stand up an agent rather than to develop canopy
> itself?** Start with **[docs/onboarding-a-new-operator.md](docs/onboarding-a-new-operator.md)** —
> the end-to-end path from nothing to a running agent, written for a first-timer.
> The rest of this README is oriented to canopy's own development.

Canopy installs as a Claude Code plugin from its marketplace. The fastest path on
a new machine is the bundled setup skill, which is idempotent:

```
/canopy:setup
```

This provisions the state directory, the main checkout, the capture hook, the
canopy-web workbench token, and the `canopy` CLI. To update an existing install
to the latest version from GitHub:

```
/canopy:update
```

For local development of the orchestrator itself:

```
uv run canopy --help          # CLI entry point (orchestrator.cli:main)
uv run pytest                 # test suite
```

Canopy targets Python 3.11+ and depends on PyYAML, Click, and Pydantic.

## Key concepts

- **The observe → propose → implement loop** is the core. `canopy improve` runs a
  full cycle; `--observe-only` and `--dry-run` stop after the earlier stages.
- **Two kinds of state.** Per-project state lives under `<repo>/.canopy/` and is
  committed to that project's git repo — e.g. `<repo>/.canopy/pm/` for
  product-management state and `<repo>/.canopy/lenses/` for per-project lenses.
  The global "self-improvement brain" (observations, proposals, the session log)
  lives under `$HOME/.claude/canopy/` and is intentionally cross-project on a
  single machine.
- **The plugin surface.** `plugins/canopy/` ships skills, slash commands, and
  agents — including the DDD (demo-driven-development) authoring pipeline, the
  product-management supervisor, portfolio review/guide, the alignment
  cross-system drift sweep, walkthrough rendering and eval, and the website
  builder. Skill descriptions are surfaced in the Claude Code system prompt, so
  canopy enforces description and naming budgets (see `canopy skills budget`,
  `canopy skills dropped`, and `canopy structure-drift`).
- **Always-PR shipping.** Changes ship as pull requests, and any change under
  `plugins/canopy/` must be accompanied by a version bump (`canopy version bump`)
  — the bump is the only signal that tells installed sessions to pick up new work.
  The full discipline lives in `CLAUDE.md`.

## Using canopy

Day to day you run a handful of commands. Everything else is discoverable via
`canopy <group> --help`.

```
canopy improve [--dry-run|--observe-only]   # the core loop: analyze → propose → implement
canopy patterns                             # recurring friction across your sessions
canopy proposals list / show <id>           # inspect what canopy wants to build
canopy brief                                # strategic summary of recent activity
canopy sessions status                      # is the capture hook logging my work?
canopy doctor                               # plugin health (hook, token, version)
canopy serve                                # transcript browser UI on localhost:8484
```

Start with `canopy improve --dry-run` until you trust the proposals, then run the
full cycle — it's gated by a circuit breaker and rate limiter, so it won't run away.

Canopy also publishes insights, portfolio guidance, shareouts, and DDD run
packages to **canopy-web**: https://labs.connect.dimagi.com/canopy

The rest of the surface — `observations`, `registry`, `skills budget`/`dropped`,
`structure-drift`, `version`, `test-audit` — is grouped under
`canopy <group> --help`. Contributors will find the versioning and skills-budget
commands documented in [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Start with **[CONTRIBUTING.md](CONTRIBUTING.md)** — the human-readable guide to
making a change safely: the three-copies model, the version-bump rule (the #1
mistake), the always-PR + self-merge ship flow, and the skill-authoring foot-guns.

`CLAUDE.md` is the authoritative, exhaustive source those rules are distilled from
(it's also loaded into every agent session). When the two disagree, CLAUDE.md wins.

Run the test suite with `uv run pytest` from the project root.
