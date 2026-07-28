---
description: Triage a GitHub repo's open issues AND open PRs against the current code — recommend implement / blocked / investigate / close per issue and merge / unblock / needs-work / close per PR, then act behind gates. Declares a scope contract up front and carries unchanged verdicts forward from the last run. Defaults to the current repo; pass owner/repo to point elsewhere.
argument-hint: "[owner/repo] [--limit N] [--scope triage-only|quick-wins|one-cluster|full]"
allowed-tools: [Bash, Read, Glob, Grep, Agent, Write, Edit, AskUserQuestion]
---

# Issue Triage (issues **and** open PRs)

Read the issue-triage SKILL.md from disk and follow it exactly:

```bash
python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'] + '/skills/issue-triage/SKILL.md')"
```

Read that file with the Read tool and follow it. The SKILL.md is the
authoritative procedure — **do NOT improvise from memory.**

## Arguments (pass through to the skill)

- `owner/repo` (optional) — the GitHub repo to triage. If omitted, the skill
  defaults to the current repo's `origin` (`gh repo view`).
- `--limit N` (optional) — cap the number of open issues and PRs triaged
  (default 30 each).
- `--scope <contract>` (optional) — pre-answer the Phase 0.5 scope question:
  `triage-only` | `quick-wins` (default) | `one-cluster` | `full`. When passed,
  announce the contract and skip the question; otherwise ask it.

Whatever the user typed after the command is the target/limit/scope. Substitute
it into the skill's Phase 0 `ARG`, Phase 0.5 contract, and Phase 1 `--limit`.

**Three things the skill does that are easy to skip and shouldn't be:** Phase 2b
carries forward verdicts whose evidence files haven't changed since the last
run's YAML sidecar (a repeat triage should cost a fraction of the first);
Phase 3.5 is a mandatory single clustering pass over all verdicts — per-issue
subagents are blind to duplicates by construction; and **Phase 3b triages open
PRs**, which is the cheapest shipped value in the run — a finished PR stranded
on a missing version bump or a docs-sync acknowledgement is worth more than
anything in the issue backlog, and a repo with no reviewer has nothing else
that notices it.
