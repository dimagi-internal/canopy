"""Agent factory — stamp out a new Claude Code agent from the canopy operating model.

Backs `canopy create-agent <slug>` and the `canopy:create-agent` skill. Generates a
self-contained agent repo grounded in the primitives proven by echo (see
docs/agent-operating-model.md): a persona, the `turn` orchestrator, reads-free /
writes-gated guarding via a config-driven PreToolUse hook (the generalization of echo's
`block_raw_gog_send.py`), and a canopy-web-ready layout.

v1 is deliberately self-contained: it COPIES the editable surface into the new repo
(persona, turn checklist, skills, the gating hook) and ships a JSON gating config the
hook reads. Gating defaults to DENY RAILS ONLY with an empty `approve` list — approval
lives procedurally in the turn checklist, never in a blocking hook modal (the §1a
"rails, not gates" revision; see docs/architecture/shared-gog-gdrive.md §4). The email
channel is a thin `bin/<slug>-email` shim over the shared `canopy email` engine
(orchestrator.agent_email), with a deny rail blocking raw `gog gmail send/reply`.

Gating config is JSON, not YAML, so the generated hook is stdlib-only (same rule as
canopy's own hooks: a PreToolUse hook runs under system python3 which may lack PyYAML).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


class AgentFactoryError(Exception):
    """Raised for bad input or an unsafe target directory."""


_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
# Reserved Claude Code built-in command names — never let an agent slug collide
# (mirrors the rule enforced by tests/test_builtin_command_collisions.py).
_RESERVED = {
    "help", "clear", "doctor", "config", "compact", "model", "fast", "login",
    "logout", "agents", "mcp", "permissions", "init", "review", "run", "loop",
}


@dataclass
class AgentSpec:
    slug: str                      # lowercase id, e.g. "echo"
    display_name: str              # human name, e.g. "Echo"
    mandate: str                   # one-line mission
    mailbox: str = ""              # primary channel address, e.g. echo@dimagi-ai.com
    stakeholders: str = ""         # who the agent serves
    author_name: str = "Jonathan Jackson"
    author_email: str = "jjackson@dimagi.com"
    channels: list[str] = field(default_factory=lambda: ["email"])

    def tokens(self) -> dict[str, str]:
        return {
            "AGENT_SLUG": self.slug,
            "AGENT_NAME": self.display_name,
            "MANDATE": self.mandate,
            "MAILBOX": self.mailbox or f"{self.slug}@example.com",
            "STAKEHOLDERS": self.stakeholders or "the team",
            "AUTHOR_NAME": self.author_name,
            "AUTHOR_EMAIL": self.author_email,
        }


def normalize_slug(raw: str) -> str:
    slug = raw.strip().lower().replace(" ", "-").replace("_", "-")
    if not _SLUG_RE.match(slug):
        raise AgentFactoryError(
            f"invalid agent slug {raw!r}: use 2-31 chars, lowercase letters/digits/hyphen, "
            "starting with a letter"
        )
    if slug in _RESERVED:
        raise AgentFactoryError(
            f"agent slug {slug!r} collides with a Claude Code built-in command name; pick another"
        )
    return slug


def _render(template: str, tokens: dict[str, str]) -> str:
    out = template
    for key, val in tokens.items():
        out = out.replace("{{" + key + "}}", val)
    return out


def create_agent(spec: AgentSpec, target_dir: Path, *, force: bool = False) -> list[Path]:
    """Generate the agent repo at target_dir. Returns the list of files written.

    Refuses to write into a non-empty directory unless force=True (and even then never
    deletes — it only adds/overwrites the templated files).
    """
    target = Path(target_dir).expanduser().resolve()
    if target.exists() and any(target.iterdir()) and not force:
        raise AgentFactoryError(
            f"{target} is not empty; pass force=True to scaffold into it anyway"
        )
    tokens = spec.tokens()
    if "@example.com" in tokens["MAILBOX"]:
        print(
            f"WARNING: no --mailbox given — scaffolding with placeholder "
            f"{tokens['MAILBOX']!r}. Email will fail late and confusingly at gog "
            f"send/login; set the real mailbox in config/agent.json before the first turn.",
            file=sys.stderr,
        )
    written: list[Path] = []
    for rel_path, template in _TEMPLATES.items():
        rel = _render(rel_path, tokens)        # path itself may carry {{AGENT_SLUG}}
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_render(template, tokens))
        if dest.name.endswith(".py") or dest.parent.name == "bin":
            dest.chmod(0o755)
        written.append(dest)
    return written


# --------------------------------------------------------------------------------------
# Template set. Each value is a file written into the new agent repo (path keys may carry
# {{AGENT_SLUG}}). Keep these lean but real — they are the starting point every future
# agent inherits, so correctness matters more than richness. Kit extraction is follow-up.
# --------------------------------------------------------------------------------------

_GATING_GUARD = r'''#!/usr/bin/env python3
"""Generic reads-free / writes-gated PreToolUse guard for {{AGENT_NAME}}.

This is the generalization of echo's block_raw_gog_send.py. It reads
config/gating.json and enforces, at the tool-call boundary:

  - "deny" rules  -> exit 2 (hard block; the agent CANNOT bypass it), with a message
                     telling it the right way to do the action.
  - "approve" rules -> escalate to a human via a PreToolUse permissionDecision of "ask".
  - everything else -> allow (reads run free).

STDLIB ONLY by design: a PreToolUse hook runs under whatever python3 is on PATH, which
may not have PyYAML. That is why the gating config is JSON, not YAML.

A rule is `{"tool": "<ToolName>", "pattern": "<regex>", "message": "..."}`. `tool` is
matched exactly against the tool name; `pattern` (optional) is matched against the Bash
command string, or the file_path for Edit/Write. Omit `pattern` to match every call of
that tool.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.path.dirname(HERE), "config", "gating.json")


def _baseline_rails(cfg):
    """Fleet-baseline deny rails for this agent's mounted channels, from the INSTALLED canopy
    plugin (agent-core/gating-baseline.json) — so a rail fix ships once and reaches every agent
    via /canopy:update. Returns [] for legacy configs (no `channels` key: local rails only), or
    None when channels are mounted but the baseline is unresolvable (caller fails CLOSED).
    CANOPY_PLUGIN_DIR overrides the plugin dir (tests / unusual installs)."""
    channels = cfg.get("channels")
    if not channels:
        return []
    slug = cfg.get("slug") or os.path.basename(os.path.dirname(HERE))
    try:
        plugin_dir = os.environ.get("CANOPY_PLUGIN_DIR")
        if not plugin_dir:
            reg = json.load(open(os.path.expanduser("~/.claude/plugins/installed_plugins.json")))
            plugin_dir = reg["plugins"]["canopy@canopy"][0]["installPath"]
        base = json.load(open(os.path.join(plugin_dir, "agent-core", "gating-baseline.json")))
    except Exception:
        return None
    rails = []
    for ch in channels:
        for rule in base.get("channels", {}).get(ch, []):
            rails.append({k: (v.replace("{slug}", slug) if isinstance(v, str) else v)
                          for k, v in rule.items()})
    return rails


def _subject(tool_name, tool_input):
    """The string a rule's pattern is tested against, per tool."""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        return tool_input.get("command", "") or ""
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return tool_input.get("file_path", "") or tool_input.get("notebook_path", "") or ""
    return ""


def _summarize_action(tool_name, subject):
    """A crisp, human-readable summary of the GATED action — so the approval prompt says exactly
    WHAT you're approving at a glance, not a generic 'needs approval' over a wall of bash."""
    if tool_name != "Bash":
        return tool_name + " -> " + subject[:80]
    s = subject
    m = re.search(r"\bgit\s+push\b([^\n;&|]*)", s)
    if m:
        args = [a for a in m.group(1).split() if not a.startswith("-")]
        return "git PUSH -> " + (" ".join(args[:2]) if args else "default remote/branch")
    m = re.search(r"\bgh\s+pr\s+create\b([^\n;&|]*)", s)
    if m:
        t = re.search(r"--title[= ]+[\"']?([^\"'\n]{0,60})", m.group(1))
        r = re.search(r"-R[= ]+(\S+)", m.group(1))
        return "OPEN a PR" + (' "' + t.group(1).strip() + '"' if t else "") + (" in " + r.group(1) if r else "")
    m = re.search(r"\bgh\s+pr\s+merge\b([^\n;&|]*)", s)
    if m:
        n = re.search(r"\b(\d+)\b", m.group(1))
        r = re.search(r"-R[= ]+(\S+)", m.group(1))
        return "MERGE a PR" + (" #" + n.group(1) if n else "") + (" in " + r.group(1) if r else "")
    m = re.search(r"\bgh\s+repo\s+(create|delete)\b([^\n;&|]*)", s)
    if m:
        nm = re.search(r"([\w.-]+/[\w.-]+|[\w.-]+)", m.group(2))
        return m.group(1).upper() + " GitHub repo" + (" " + nm.group(1) if nm else "")
    return s.strip().replace("\n", " ")[:100]


def _approval_reason(rule, tool_name, subject, cwd):
    """A scannable approval prompt: WHAT (parsed action) + WHERE (repo) + the exact command +
    WHY (policy note). One glance should be enough to decide."""
    action = _summarize_action(tool_name, subject)
    repo = os.path.basename(cwd.rstrip("/")) if cwd else ""
    cmd = subject.strip().replace("\n", " ")
    if len(cmd) > 220:
        cmd = cmd[:220] + " ..."
    note = rule.get("message") or "outbound/write action - needs your approval."
    head = "APPROVE {{AGENT_NAME}} -> " + action + ("   (repo: " + repo + ")" if repo else "")
    return head + "\n  why: " + note + "\n  full command: " + cmd


def _matches(rule, tool_name, subject):
    if rule.get("tool") and rule["tool"] != tool_name:
        return False
    pat = rule.get("pattern")
    if not pat:
        return True
    try:
        return re.search(pat, subject) is not None
    except re.error:
        return False


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)            # never block on a parse failure
    try:
        cfg = json.load(open(CONFIG))
    except Exception:
        sys.exit(0)            # no/*broken* config = no extra gating

    tool_name = data.get("tool_name", "")
    subject = _subject(tool_name, data.get("tool_input"))
    cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "")

    baseline = _baseline_rails(cfg)
    if baseline is None:
        # channels are mounted but the fleet baseline is unreadable — fail CLOSED, with the fix.
        sys.stderr.write(
            "BLOCKED (fail closed): {{AGENT_SLUG}}'s config/gating.json mounts channels but the "
            "canopy fleet gating baseline (agent-core/gating-baseline.json) is unreadable. "
            "Fix: run /canopy:update (or `uv tool install canopy` / check "
            "~/.claude/plugins/installed_plugins.json), then retry.\n")
        sys.exit(2)

    for rule in baseline + cfg.get("deny", []):
        if _matches(rule, tool_name, subject):
            msg = rule.get("message") or "BLOCKED by {{AGENT_SLUG}} gating policy (deny rule)."
            sys.stderr.write(msg.rstrip() + "\n")
            sys.exit(2)

    for rule in cfg.get("approve", []):
        if _matches(rule, tool_name, subject):
            reason = _approval_reason(rule, tool_name, subject, cwd)
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }))
            sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
'''

_GATING_JSON = '''{
  "_doc": "Rails, not gates (docs/agent-operating-model.md §1a revision, Jon 2026-07-01; canopy docs/architecture/shared-gog-gdrive.md §4): hooks carry DENY rails only — make the wrong path impossible and name the right one, so {{AGENT_NAME}} self-corrects and keeps going at zero autonomy cost. `channels` mounts the FLEET-BASELINE deny rails shipped in the installed canopy plugin (agent-core/gating-baseline.json) — hooks/gating_guard.py merges them in front of this file's own `deny` list at call time, so a baseline rail fix reaches every agent via /canopy:update. ADD-ONLY: this file can add rails; it can never remove a baseline one. `approve` stays EMPTY by default: a PreToolUse 'ask' is a blocking modal that stalls autonomous work; approval semantics live in the procedural layer (skills/turn Step 2's 'present for approval'). Patterns are regex tested against the Bash command, or the file_path for Edit/Write.",
  "slug": "{{AGENT_SLUG}}",
  "channels": ["email"],
  "deny": [],
  "approve": []
}
'''

_SETTINGS_JSON = '''{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \\"$CLAUDE_PROJECT_DIR/hooks/gating_guard.py\\""
          }
        ]
      }
    ]
  }
}
'''

_PLUGIN_JSON = '''{
  "name": "{{AGENT_SLUG}}",
  "version": "0.1.0",
  "description": "{{AGENT_NAME}} — {{MANDATE}} Built on the canopy agent operating model: a turn-driven persona with reads-free / writes-gated guarding and human-approved outbound actions.",
  "author": {
    "name": "{{AUTHOR_NAME}}",
    "email": "{{AUTHOR_EMAIL}}"
  },
  "license": "MIT",
  "keywords": ["agent", "canopy", "{{AGENT_SLUG}}"]
}
'''

_CLAUDE_MD = '''# CLAUDE.md — {{AGENT_NAME}}

{{AGENT_NAME}} is an autonomous agent built on the **canopy agent operating model**
(see canopy `docs/agent-operating-model.md`). Read this first when working here.

## What {{AGENT_NAME}} is
- **Mandate:** {{MANDATE}}
- **Persona + routing key:** identity and voice live in `persona.md`; the primary channel is
  `{{MAILBOX}}`. One thread / one counterpart / one memory scope per turn — never reason about
  two counterparts together (the rule that prevents cross-contamination).
- **Harness = Claude Code today.** A human triggers a turn ("do a turn"); more autonomy is the
  long-term goal, climbed one rung at a time, not skipped to.
- **Stakeholders:** {{STAKEHOLDERS}}.

## Hard guardrail — reads free, writes gated
Search/read run freely. **Every outbound action (sending on a channel, public writes) requires
explicit human approval.** {{AGENT_NAME}} drafts; the human disposes.

## Invariants are hooks, not memory — and rails, not approval gates
Hard behavioral rules do NOT belong in prose alone — prose relies on the model choosing to
comply, which fails under load. Encode each as **enforcement**: a `deny` rule in
`config/gating.json` that `hooks/gating_guard.py` turns into a hard block naming the right
path (e.g. raw `gog gmail send` → `bin/{{AGENT_SLUG}}-email`). Keep the `approve` list EMPTY:
a PreToolUse "ask" is a blocking modal that stalls autonomous work — approval semantics live
in the turn checklist (Step 2's "present for approval"), not in hooks. See the operating
model's §1a revision (rails, not gates). Add a new outbound action? Add a deny rail pointing
at its one sanctioned path — do not just write a sentence here.

## Doing a turn — load the procedure, don't improvise
"Do a turn" / "check your inbox" = the `skills/turn` procedure. **Re-read `skills/turn/SKILL.md`
at the start of every turn and follow it in order.** Running a turn from memory is exactly how
steps get dropped under load.

## Canopy is installed alongside this agent
{{AGENT_NAME}} runs with the **canopy plugin + CLI** available. Shared infra lives in canopy, not
here: the gating *engine*, channel adapters, the canopy-web client (`canopy agent-publish`), and
the fleet self-improvement loop. This repo holds only what is {{AGENT_NAME}}-specific — persona,
domain skills, gating *rules* (`config/gating.json`), allowlist, secrets. When something is pure
infra you'd want every agent to get, it belongs in canopy; raise it there, don't fork it here.
{{AGENT_NAME}}'s web workspace is `/agents/{{AGENT_SLUG}}` on canopy-web.

## Conventions
- Capability logic belongs in CLIs / MCP tools; skills (`SKILL.md`) orchestrate. That keeps the
  agent portable to the Claude Agent SDK later (MCP is the portability boundary).
- Config/secrets live in a WORKTREE-CLEAN global home `~/.{{AGENT_SLUG}}/.env`, provisioned by
  `canopy provision` from `config/secrets.yaml` (1Password refs + non-secret literals). NOT a
  per-worktree repo `.env` (that gets recreated every turn). Scripts read it via `bin/_env.py`.
- Outputs that are deliverables live where the team can see them (a shared drive / the canopy-web
  workspace), not as loose local files.

## Shipping — no human code review
Ship code freely: branch → commit → PR → merge it yourself. The PR is a CI/record checkpoint,
not a review gate. This relaxes ONLY code review — it does NOT relax the runtime guardrail:
outbound actions still require human approval at run time.
'''

_PERSONA_MD = '''# {{AGENT_NAME}} — persona

## Identity
{{AGENT_NAME}} is an autonomous agent whose mandate is: **{{MANDATE}}**

Primary channel: `{{MAILBOX}}`. Serves: {{STAKEHOLDERS}}.

## Working style — magical planner and doer
{{AGENT_NAME}} is the best, most magical planner and doer — not a junior who asks for clarity
before acting. Think critically, decide, act, then report: (1) what you DID, (2) your opinion +
recommendation, (3) what else we could do. If a brief grants authority ("pick one and execute"),
exercise it and report — don't ask which.

## Voice
- Direct, warm, concrete. Lead with the outcome, not the process.
- Enumerate multiple asks and show how each was handled — one line each. Never blur several
  requests into one paragraph.

## Guardrails (enforced, not just stated)
- **Reads free, writes gated.** Outbound actions wait for explicit human approval.
- **One counterpart per turn.** Never reason about two counterparts' threads together.
- Hard rules live in `config/gating.json` + `hooks/gating_guard.py`, not in this prose.

## Memory scope (fill in when a memory backend is wired)
Per-counterpart facts (who they are, history, commitments) and per-campaign/topic state are the
only things worth persisting. Behaviors become skills, not memories.
'''

_TURN_SKILL = '''---
name: turn
description: >
  {{AGENT_NAME}}'s full turn-of-work orchestrator. Use when a human says "do a turn", "check your
  inbox", or otherwise triggers {{AGENT_NAME}} to work. The canonical procedure is fleet-wide and
  lives in the installed canopy plugin (agent-core/turn.md); this stub binds it to {{AGENT_NAME}}'s
  identity. THE entry point for a turn.
---

# Turn — {{AGENT_NAME}} (stub over the fleet-canonical core)

The turn procedure is fleet-canonical so every agent runs the same, current process, and
improvements ship once (a canopy PR) instead of N backports.

1. **Resolve the installed canopy plugin and check freshness:**
   ```bash
   CANOPY=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   bash "$CANOPY/scripts/canopy-update-check.sh"
   ```
   `UPGRADE_AVAILABLE <old> <new>` → tell the human and run `/canopy:update` BEFORE following a
   stale core.
2. **Read `$CANOPY/agent-core/turn.md`** (Read tool, absolute path) and **follow it exactly**,
   bound to the Identity below. Where it says `<slug>`, use this Identity.

## Identity
- Name: **{{AGENT_NAME}}** · slug: `{{AGENT_SLUG}}` · mailbox: `{{MAILBOX}}`
- Email shim: `bin/{{AGENT_SLUG}}-email` · board: `/agents/{{AGENT_SLUG}}`

## {{AGENT_NAME}}-local notes (the ONLY hand-edited section — fleet-process changes go to canopy)
- (none yet)
'''

_AGENT_TURN_REVIEW_SKILL = '''---
name: agent-turn-review
user-invocable: false   # internal: Claude invokes it; it is not a human-launchable /command
description: >
  {{AGENT_NAME}}'s pre-send review — invokes the fleet-wide `canopy:agent-turn-review` discipline,
  then adds {{AGENT_NAME}}'s specifics. Run before EVERY outbound reply / deliverable / PR.
---

# Agent turn review ({{AGENT_NAME}})

Run before every outbound action (the thing that gets dropped under load). The general discipline
is fleet-wide and DRY — **invoke `canopy:agent-turn-review`** and apply it in full:
- **A. Fidelity** — re-read the request, extract each ask, do EXACTLY that (read cited sources),
  rate it tough.
- **B. Grounded commitments** — every "I'll do X" needs a concrete, executable mechanism; a vague
  "sync with / coordinate with / loop in <person>" is vapor — convert it to a runnable check or a
  draft-then-ask message, or cut it.
- **C. Presentation** — lead with what you DID; enumerate multiple asks; link artifacts as shared
  docs; verify recipients (reply-all hides `Cc:` in raw views).

## {{AGENT_NAME}}-specifics
- **Send path:** outbound comms go ONLY via {{AGENT_NAME}}'s sanctioned path (draft-then-ask). §B's
  "concrete action" for coordination is a check you actually run or a message you actually send here.
- **Gated in:** `turn` (before shipping/replying, and the close checklist).
'''

_SECRETS_YAML = '''# {{AGENT_NAME}}'s secrets + config, declarative. `canopy provision` materializes these from the
# 1Password AI-Agents vault into the WORKTREE-CLEAN global home ~/.{{AGENT_SLUG}}/.env — NOT a
# per-worktree repo .env. emdash runs each turn in a fresh worktree; a repo .env would be absent
# there and get recreated every turn, so it lives ONCE in the global home and every worktree reads
# it (via bin/_env.py). No values here — only 1Password refs (`op:`) and non-secret literals
# (`value:`). Provision with `canopy provision` (or `--check` to dry-run) from this repo.
#
# The gog OAuth client is the SHARED fleet app (`canopy`), not per-agent: a gog "client" is the APP
# identity (client_id + client_secret), reused by every agent's mailbox. The per-agent, never-shared
# identity is the mailbox ({{MAILBOX}}). `canopy provision` places credentials-canopy.json; then a
# one-time `gog login {{MAILBOX}} --client canopy ...` consents this mailbox into it.
secrets:
  - name: canopy gog OAuth client JSON
    op: "op://AI-Agents/Canopy - gog OAuth client/notesPlain"
    target: "~/Library/Application Support/gogcli/credentials-canopy.json"
env:
  target: "~/.{{AGENT_SLUG}}/.env"
  mode: "0600"
  vars:
    - key: {{AGENT_SLUG}}_PRIMARY_CHANNEL
      value: "{{MAILBOX}}"                                   # not a secret — the agent's channel
    # - key: {{AGENT_SLUG}}_SOME_SECRET
    #   op: "op://AI-Agents/{{AGENT_NAME}} item/field"       # a real secret — resolved from 1Password
    # - key: CANOPY_WEB_PAT
    #   op: "op://AI-Agents/{{AGENT_NAME}} canopy-web PAT/credential"
    #   optional: true
'''

_ENV_LOADER = '''"""Canonical .env resolver for {{AGENT_NAME}}'s scripts — reads the WORKTREE-CLEAN global home.

{{AGENT_NAME}} runs each turn in a fresh emdash worktree; a per-repo `.env` would be absent there and
get recreated every turn. So secrets live ONCE at ~/.{{AGENT_SLUG}}/.env (provisioned by
`canopy provision` from config/secrets.yaml; override the home with ${{AGENT_SLUG}}_ENV, upper-cased).
A repo-local `.env` still works as a per-KEY dev override (it wins).

    from _env import get, load
    val = get("{{AGENT_SLUG}}_PRIMARY_CHANNEL", "")   # os.environ wins, then global .env, then default

Never `source` the .env from bash — values with `>`/`!`/`=` break shell parsing; use this module.
"""
import os

_SLUG = "{{AGENT_SLUG}}"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLOBAL_ENV = os.environ.get(_SLUG.upper() + "_ENV") or os.path.expanduser("~/." + _SLUG + "/.env")
REPO_ENV = os.path.join(REPO, ".env")
ENV_PATH = GLOBAL_ENV


def load(path=None):
    """Parse .env(s) into a dict — global home merged with an optional repo-local override (repo
    wins per key). Splits on the FIRST '=', value verbatim (so `>`/`!`/`=`/spaces survive)."""
    out = {}
    for p in ([path] if path else [GLOBAL_ENV, REPO_ENV]):
        if not p or not os.path.exists(p):
            continue
        for raw in open(p):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            out[k] = v
    return out


def get(key, default=None):
    """Resolve one var: os.environ first (caller override), then the .env home, else default."""
    if os.environ.get(key):
        return os.environ[key].strip()
    return load().get(key, default)
'''

_EMAIL_SHIM = '''#!/usr/bin/env python3
"""{{AGENT_NAME}}'s ONLY email send path — a thin shim over the shared canopy email engine.

The HTML wrapper (Gmail display-wraps plain text at ~72 cols; HTML reflows), reply
threading, and identity resolution all live in canopy (`canopy email send`), so fixes
propagate fleet-wide. This shim only pins --repo so identity comes from THIS repo's
config/agent.json (`email` + `gog_client`). The deny rail in config/gating.json blocks
raw `gog gmail send/reply`, so email leaves {{AGENT_NAME}} only through here.

SEND-SIDE CONTRACT: every successful send prints JSON with message_id + thread_id —
record thread_id into {{AGENT_NAME}}'s state layer so inbound triage can route the reply.

Usage:
  bin/{{AGENT_SLUG}}-email --to "a@x,b@y" [--cc "c@z"] --subject "Re: ..." \\
      --body-file body.txt [--reply-to-message-id <id>] [--reply-all] [--dry-run]

Auth check: `canopy email preflight --repo <this repo>` (exact `gog login` fix on failure).
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
    # This is a WRAPPER, not a subcommand. Do NOT forward --help to
    # `canopy email send` — its click usage line reads `Usage: canopy email send`,
    # which misleads you into calling `bin/{{AGENT_SLUG}}-email send ...` (an
    # "unexpected extra argument" error). Print THIS script's own usage instead.
    print((__doc__ or "").strip())
    print()
    print("Wraps `canopy email send` — pass the OPTIONS above directly, with NO "
          "`send` subcommand. Full flag list: canopy email send --help")
    sys.exit(0)
try:
    os.execvp("canopy", ["canopy", "email", "send", "--repo", REPO, *sys.argv[1:]])
except FileNotFoundError:
    sys.exit(
        "canopy CLI not on PATH — the shared email engine lives there.\\n"
        "Install it (`uv tool install canopy`) or run from a canopy checkout: "
        "`uv run canopy email send --repo <this repo> ...`"
    )
'''

_ALLOWLIST = '''# Counterparts {{AGENT_NAME}} may ACT on (send/reply/write). One per line.
# Unknown senders are triaged read-only and surfaced to the human, never acted on.
# A line may be a full address (name@example.com) or a whole domain (@example.com).
'''

_README = '''# {{AGENT_NAME}}

{{AGENT_NAME}} — {{MANDATE}}

A Claude Code agent built on the **canopy agent operating model** (see canopy
`docs/agent-operating-model.md`). Primary channel: `{{MAILBOX}}`.

## How it works
- **Persona** in `persona.md`; the operating contract in `CLAUDE.md`.
- **A turn** is the unit of work: `skills/turn/SKILL.md` is the re-read-every-time checklist.
- **Reads free, writes gated — rails, not gates:** `hooks/gating_guard.py` reads
  `config/gating.json` and hard-blocks wrong paths (`deny` rails that name the right path) at
  the tool-call boundary. Approval is procedural — the turn checklist's explicit
  draft-then-ask step — never a blocking hook modal (`approve` stays empty by default).
- **Email channel:** `bin/{{AGENT_SLUG}}-email` — a thin shim over the shared `canopy email`
  engine; identity from `config/agent.json`. Raw `gog gmail send` is deny-railed.

## Run a turn
In Claude Code, from this repo: "do a turn". {{AGENT_NAME}} re-reads `skills/turn/SKILL.md` and
follows it: preflight → process inbound (one counterpart at a time) → skill self-check → close.

## Make it yours
1. Fill in `persona.md` (voice, mandate detail, memory scope).
2. Add domain skills under `skills/<name>/SKILL.md`.
3. Add each outbound action as a `deny` rail in `config/gating.json` pointing at its one
   sanctioned path — not as prose, and not as an `approve` rule (approval is the turn
   checklist's job; hook modals stall autonomous work).
4. Declare secrets in `config/secrets.yaml` (1Password refs) and run `canopy provision` — they land
   in the worktree-clean global home `~/.{{AGENT_SLUG}}/.env`, read by `bin/_env.py`.
5. The agent's own thing is its MAILBOX ({{MAILBOX}}); the gog OAuth client is the SHARED fleet
   app (`canopy`), already declared in config/secrets.yaml — `canopy provision` places it. Then
   consent this mailbox into it once: `gog login {{MAILBOX}} --client canopy --services
   gmail,drive,docs,sheets,forms,appscript`. Verify with `canopy email preflight --repo .`; send via
   `bin/{{AGENT_SLUG}}-email`.
'''

_GITIGNORE = '''.env
__pycache__/
*.pyc
.DS_Store
'''

_AGENT_JSON = '''{
  "_doc": "{{AGENT_NAME}}'s identity for its canopy-web workspace (/agents/{{AGENT_SLUG}}) and for the shared email engine (`canopy email` resolves mailbox + gog client from here). Read by `canopy agent-publish`. slug + description come from .claude-plugin/plugin.json; these override/extend. gog_client is the SHARED fleet OAuth client (`canopy`), reused by every agent's mailbox; the per-agent, never-shared identity is `email` (identity bleed = acting as another agent's mailbox, governed by --account, not the client).",
  "name": "{{AGENT_NAME}}",
  "email": "{{MAILBOX}}",
  "gog_client": "canopy",
  "persona": "{{MANDATE}}",
  "avatar_url": ""
}
'''

_TASK_TRACKER_SKILL = '''---
name: task-tracker
user-invocable: false   # internal: Claude invokes it; it is not a human-launchable /command
description: >
  {{AGENT_NAME}}'s project/task state — one board task per iterative thread/project, backed by
  canopy-web (kanban at /agents/{{AGENT_SLUG}}). The canonical procedure is fleet-wide and lives
  in the installed canopy plugin (agent-core/task-tracker.md); this stub binds it to
  {{AGENT_NAME}}. Use when taking on multi-turn work, when a new request arrives, and at every
  turn's board-drain and close.
---

# Task Tracker — {{AGENT_NAME}} (stub over the fleet-canonical core)

1. **Resolve the installed canopy plugin and check freshness:**
   ```bash
   CANOPY=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   bash "$CANOPY/scripts/canopy-update-check.sh"
   ```
   `UPGRADE_AVAILABLE` → tell the human and run `/canopy:update` BEFORE following a stale core.
2. **Read `$CANOPY/agent-core/task-tracker.md`** and **follow it exactly**, bound to the
   Identity below. Where it says `<slug>`/`<mailbox>`, use this Identity.

## Identity
- Name: **{{AGENT_NAME}}** · slug: `{{AGENT_SLUG}}` · mailbox: `{{MAILBOX}}`
- Board: `/agents/{{AGENT_SLUG}}` · Drive folder id env: `{{AGENT_SLUG}}_DRIVE_FOLDER_ID`

## {{AGENT_NAME}}-local notes (the ONLY hand-edited section — fleet-process changes go to canopy)
- (none yet)
'''

_MANAGER_SYNC_SKILL = '''---
name: manager-sync
description: >
  {{AGENT_NAME}}'s periodic advisor sync — reviews everything done since the last sync, HARSHLY
  self-grades the work AND the reusable skills built, and puts a short list of already-decided next
  steps to the advisor to confirm or redirect. The canonical procedure is fleet-wide and lives in
  the installed canopy plugin (agent-core/manager-sync.md); this stub binds it to {{AGENT_NAME}}.
  Trigger: "manager sync" / "sync with my advisor".
---

# Manager sync — {{AGENT_NAME}} (stub over the fleet-canonical core)

1. **Resolve the installed canopy plugin and check freshness:**
   ```bash
   CANOPY=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   bash "$CANOPY/scripts/canopy-update-check.sh"
   ```
   `UPGRADE_AVAILABLE` → tell the human and run `/canopy:update` BEFORE following a stale core.
2. **Read `$CANOPY/agent-core/manager-sync.md`** and **follow it exactly**, bound to the Identity
   below. Window state + the posted sync live on canopy-web (`canopy agent syncs`/`sync`), never a
   repo file.

## Identity
- Name: **{{AGENT_NAME}}** · slug: `{{AGENT_SLUG}}` · mailbox: `{{MAILBOX}}`
- Advisor: (name the human whose steer this sync is for) · Board/workspace: `/agents/{{AGENT_SLUG}}`

## {{AGENT_NAME}}-local notes (the ONLY hand-edited section — fleet-process changes go to canopy)
- Publish the sync doc with your gdoc tool; draft the report to your advisor with your email tool
  (gated send). Link skills as GitHub source. (none beyond the core yet)
'''

_BUMP_SCRIPT = r'''#!/usr/bin/env python3
"""Bump this agent plugin's PATCH version in lock-step across every file that
carries it. Run from the repo root (or anywhere — paths resolve off this file).

The three fields that MUST agree, and drifted in the past when hand-bumped:
  * .claude-plugin/plugin.json          -> version
  * .claude-plugin/marketplace.json     -> metadata.version
  * .claude-plugin/marketplace.json     -> plugins[*].version

Used by the auto-version-bump GitHub Action (bump-on-every-merge, GitHub #357)
and runnable by hand. Prints "OLD -> NEW". stdlib only.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKET = ROOT / ".claude-plugin" / "marketplace.json"


def bump_patch(version: str) -> str:
    parts = version.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"unparseable version: {version!r}")
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


def _load(path: Path) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _save(path: Path, data: dict) -> None:
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def main() -> None:
    plugin = _load(PLUGIN)
    old = plugin.get("version")
    if not old:
        raise SystemExit("plugin.json has no version")
    new = bump_patch(old)
    plugin["version"] = new
    _save(PLUGIN, plugin)

    if MARKET.exists():
        market = _load(MARKET)
        market.setdefault("metadata", {})["version"] = new
        for entry in market.get("plugins", []):
            entry["version"] = new
        _save(MARKET, market)

    print(f"{old} -> {new}")


if __name__ == "__main__":
    main()
'''

_AUTO_BUMP_WORKFLOW = r'''name: Auto version bump

# Bump the PATCH version on EVERY merge to main, so canopy's fleet
# session-start auto-updater always sees a fresh version and `claude plugin
# list` stays honest. Bumping often is intentional — "landed on main" must mean
# "the running agent (and its crons) will pick it up next session" (GitHub #357).
#
# The updater self-heals on the git SHA regardless, so this is the label-honesty
# half, not the load-bearing half — but it closes the "skill changed, version
# forgotten" gap that made version-keyed updates a silent no-op.

on:
  push:
    branches: [main]

permissions:
  contents: write

concurrency:
  group: auto-version-bump
  cancel-in-progress: false

jobs:
  bump:
    # Skip the workflow's own bump commit. Belt-and-suspenders: pushes made with
    # the default GITHUB_TOKEN don't retrigger workflows anyway.
    if: ${{ !contains(github.event.head_commit.message, '[skip bump]') }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: main
          fetch-depth: 0
      - name: Bump patch version
        run: python3 scripts/bump-plugin-version.py
      - name: Commit and push
        run: |
          if git diff --quiet; then
            echo "No version change to commit."
            exit 0
          fi
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add .claude-plugin/plugin.json .claude-plugin/marketplace.json
          git commit -m "chore: auto-bump plugin version [skip bump]"
          git push origin HEAD:main
'''

_TEMPLATES: dict[str, str] = {
    ".claude-plugin/plugin.json": _PLUGIN_JSON,
    "scripts/bump-plugin-version.py": _BUMP_SCRIPT,
    ".github/workflows/auto-version-bump.yml": _AUTO_BUMP_WORKFLOW,
    "CLAUDE.md": _CLAUDE_MD,
    "persona.md": _PERSONA_MD,
    "README.md": _README,
    ".gitignore": _GITIGNORE,
    "config/secrets.yaml": _SECRETS_YAML,
    "bin/_env.py": _ENV_LOADER,
    "bin/{{AGENT_SLUG}}-email": _EMAIL_SHIM,
    "config/gating.json": _GATING_JSON,
    "config/allowlist.txt": _ALLOWLIST,
    "config/agent.json": _AGENT_JSON,
    ".claude/settings.json": _SETTINGS_JSON,
    "hooks/gating_guard.py": _GATING_GUARD,
    "skills/turn/SKILL.md": _TURN_SKILL,
    "skills/agent-turn-review/SKILL.md": _AGENT_TURN_REVIEW_SKILL,
    "skills/task-tracker/SKILL.md": _TASK_TRACKER_SKILL,
    "skills/manager-sync/SKILL.md": _MANAGER_SYNC_SKILL,
}
