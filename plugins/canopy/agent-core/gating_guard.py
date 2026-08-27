#!/usr/bin/env python3
"""The fleet's reads-free / writes-gated PreToolUse guard — ONE implementation, all agents.

Each agent's `hooks/gating_guard.py` is a thin loader that execs this file out of the
INSTALLED canopy plugin. Nothing here is agent-specific: the agent's slug and display name
are read from its own repo config at call time, so this file is byte-identical for everyone
and a fix reaches the fleet through `/canopy:update` like the rails already do.

## Why this moved here (2026-08-13)

The rails were centralized (`agent-core/gating-baseline.json`, shipped with the plugin) but
the ENGINE that read them was COPIED into every agent repo at scaffold time and never
updated. Config was shared; code was forked. All three predictable symptoms had arrived:

  1. A one-line engine fix needed N pull requests, one per agent.
  2. The copies drifted. Measured 2026-08-13: of four agents, three were behind — missing
     `tool_pattern` and the MCP `_subject` fallback, i.e. the Drive-filing rails silently did
     nothing for them.
  3. **Improvements flowed the wrong way.** Ada independently built `per_statement` (below)
     against a real false positive, and it stayed in Ada: canopy never got it, so eva, hal and
     echo could not use it — and on 2026-08-13 eva was bitten by a sibling of the very bug Ada
     had already solved. Value invented at a leaf could not reach the fleet.

What legitimately stays per-agent is CONFIG and CONTENT — `config/gating.json` (that agent's
own rails + its `channels` mounts), identity, persona, domain skills. Not the engine.

## The contract

Reads `<repo>/config/gating.json` and enforces, at the tool-call boundary:

  - `deny`    -> exit 2 (hard block the agent cannot bypass), with a message naming the RIGHT
                 path. Rails, not gates: a block that doesn't say what to do instead just
                 stalls the turn.
  - `approve` -> escalate to a human via a PreToolUse `permissionDecision` of "ask".
  - anything else -> allow. Reads run free.

A rule is `{"tool": ..., "tool_pattern": ..., "pattern": ..., "per_statement": ..., "message": ...}`:

  tool           exact tool-name match.
  tool_pattern   regex over the tool NAME. Needed because MCP tool names carry their plugin
                 mount — the same gdrive creator is `mcp__plugin_chrome-sales_gdrive__…` for
                 one agent and `mcp__plugin_ace_ace-gdrive__…` for another.
  pattern        regex over the SUBJECT (see `_subject`). Omit to match every call of the tool.
  per_statement  test each shell statement separately instead of the whole command (see below).
  requires_path  repo-relative path that must EXIST for the rule to apply. Lets a fleet-baseline
                 rail be conditional on what the agent actually ships, so one rail can serve
                 every agent without blocking the ones the rail makes no sense for — e.g. the
                 review-wrapper rail applies only where `skills/agent-turn-review/SKILL.md` is
                 present. The alternative is a per-agent copy of the rail, which is the drift
                 this file's whole history is about.

STDLIB ONLY by design: a PreToolUse hook runs under whatever python3 is on PATH, which may not
have PyYAML. That is why the gating config is JSON, not YAML.
"""
import json
import os
import re
import sys

# Statement separators in a Bash command line. Splitting here is approximate (it does not
# understand quoting or heredocs), which is exactly why it is only ever used to make a rail
# MORE precise, never less: a violation contained in one statement still matches that
# statement, and anything the split gets wrong stays inside a single chunk and still matches.
_STATEMENT_SPLIT = re.compile(r"[\n;]|&&|\|\||[|&]")


def _statements(subject):
    """The subject split into individual shell statements, plus the whole string."""
    parts = [p.strip() for p in _STATEMENT_SPLIT.split(subject)]
    return [p for p in parts if p]


def config_path(repo_dir):
    return os.path.join(repo_dir, "config", "gating.json")


def agent_labels(repo_dir, cfg):
    """(slug, display name) for this agent's messages — read, never templated in.

    Keeping these out of the file is what lets one implementation serve every agent."""
    slug = cfg.get("slug") or os.path.basename(repo_dir.rstrip("/")) or "the agent"
    name = slug.title()
    try:
        with open(os.path.join(repo_dir, "config", "agent.json")) as fh:
            name = json.load(fh).get("name") or name
    except Exception:
        pass
    return slug, name


# Tools exempt from the fail-CLOSED branch below. The exemption is narrow and the test is
# whether the tool can ITSELF perform an outbound or destructive act.
#
# `Skill` cannot. Invoking a skill sends nothing, writes nothing and spends nothing — it selects a
# procedure, and every act that procedure then takes arrives back at THIS hook as its own Bash /
# Edit / Write / MCP call and is railed there. So a lost baseline cannot cost safety on a `Skill`
# call, while blocking one costs the recovery path itself: the fail-closed message says to run
# `/canopy:update`, and with `Skill` in an agent's PreToolUse matcher and no exemption here, that
# is a `Skill` call — blocked by the very message telling you to make it. The guard would wedge
# the agent shut and name its own remedy in the same breath.
#
# This was the documented reason the review-wrapper collision went UNRAILED (hal's
# skills/agent-turn-review § "Why this isn't a gating rail", 2026-08): widening the matcher to
# reach a `Skill` call meant accepting exactly that deadlock, and "a guard that can wedge the
# agent is worse than the gap it closes" is the fleet's own standard. The objection was correct
# about the code as it stood; it is fixed here rather than routed around, which is what lets the
# `always` rail below exist at all.
#
# Anything not listed stays fail-closed, so a new tool defaults to the safe side.
FAIL_OPEN_TOOLS = frozenset({"Skill"})


def _substituted(rule, slug):
    return {k: (v.replace("{slug}", slug) if isinstance(v, str) else v) for k, v in rule.items()}


def baseline_rails(cfg, slug):
    """Fleet-baseline deny rails from the INSTALLED canopy plugin (agent-core/gating-baseline.json)
    — so a rail fix ships once and reaches every agent via /canopy:update.

    Two sources, and the difference is what happens when the baseline is unreadable:

      `channels`  rails for the channels this agent MOUNTS (email, gws). Safety-bearing: losing
                  one means a raw send or a My-Drive-root file lands unblocked. Unresolvable
                  baseline + mounted channels -> None, and the caller fails CLOSED.
      `always`    rails that apply to EVERY agent regardless of what it mounts, because what
                  they govern is not a channel (added 2026-08-27 for the review-wrapper rail).
                  A per-channel home would have meant every agent editing its own gating.json to
                  opt in — i.e. the rail is absent for exactly the agent that forgot, which is
                  the failure mode being fixed. These are nudges, not safety, so an agent with
                  nothing mounted still runs when the baseline cannot be read.

    CANOPY_PLUGIN_DIR overrides the plugin dir (tests / unusual installs)."""
    channels = cfg.get("channels") or []
    try:
        plugin_dir = os.environ.get("CANOPY_PLUGIN_DIR")
        if not plugin_dir:
            reg = json.load(open(os.path.expanduser("~/.claude/plugins/installed_plugins.json")))
            plugin_dir = reg["plugins"]["canopy@canopy"][0]["installPath"]
        base = json.load(open(os.path.join(plugin_dir, "agent-core", "gating-baseline.json")))
    except Exception:
        return None if channels else []
    rails = [_substituted(rule, slug) for rule in base.get("always", [])]
    for ch in channels:
        for rule in base.get("channels", {}).get(ch, []):
            rails.append(_substituted(rule, slug))
    return rails


def subject_for(tool_name, tool_input):
    """The string a rule's pattern is tested against, per tool.

    Anything that is not a known built-in falls back to the JSON of its whole input — which is
    how an MCP tool becomes railable at all. Before 2026-08-13 this returned "" for every MCP
    tool, so a rail could not inspect an MCP call's ARGUMENTS: a rule with a pattern could
    never match, and a rule without one matched unconditionally (blocking the tool outright).
    That left every Drive-creating MCP tool outside the filing rails while Bash was railed."""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        return tool_input.get("command", "") or ""
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return tool_input.get("file_path", "") or tool_input.get("notebook_path", "") or ""
    try:
        return json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        return ""


def summarize_action(tool_name, subject):
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


def approval_reason(rule, tool_name, subject, cwd, name):
    """A scannable approval prompt: WHAT (parsed action) + WHERE (repo) + the exact command +
    WHY (policy note). One glance should be enough to decide."""
    action = summarize_action(tool_name, subject)
    repo = os.path.basename(cwd.rstrip("/")) if cwd else ""
    cmd = subject.strip().replace("\n", " ")
    if len(cmd) > 220:
        cmd = cmd[:220] + " ..."
    note = rule.get("message") or "outbound/write action - needs your approval."
    head = "APPROVE " + name + " -> " + action + ("   (repo: " + repo + ")" if repo else "")
    return head + "\n  why: " + note + "\n  full command: " + cmd


def matches(rule, tool_name, subject):
    """Does this rule fire on this call?"""
    if rule.get("tool") and rule["tool"] != tool_name:
        return False
    tpat = rule.get("tool_pattern")
    if tpat:
        try:
            if re.search(tpat, tool_name) is None:
                return False
        except re.error:
            return False
    pat = rule.get("pattern")
    if not pat:
        return True
    try:
        # `per_statement` rails test each shell statement on its own. A multi-lookahead rail
        # (e.g. "a curl AND this URL AND a write verb") otherwise conjoins fragments from
        # UNRELATED statements in the same call: a free GET of /items in one statement plus an
        # unrelated -X POST in the next satisfied every lookahead and blocked a legitimate read
        # (ada, 2026-07-24: this fired on a compound command, and again on a python heredoc that
        # merely quoted the pattern's own example strings). Default stays whole-string, so
        # existing rails are unchanged.
        #
        # Promoted from ada's private copy to the fleet on 2026-08-13. It had been stranded
        # there for three weeks — the concrete case for why this engine is shared now.
        if rule.get("per_statement"):
            return any(re.search(pat, s) is not None for s in _statements(subject))
        return re.search(pat, subject) is not None
    except re.error:
        return False


def run(repo_dir, payload):
    """Evaluate one PreToolUse payload. Returns (exit_code, stdout, stderr)."""
    try:
        cfg = json.load(open(config_path(repo_dir)))
    except Exception:
        return 0, "", ""          # no/broken config = no extra gating

    slug, name = agent_labels(repo_dir, cfg)
    tool_name = payload.get("tool_name", "")
    subject = subject_for(tool_name, payload.get("tool_input"))
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "")

    rails = baseline_rails(cfg, slug)
    if rails is None and tool_name not in FAIL_OPEN_TOOLS:
        # channels are mounted but the fleet baseline is unreadable — fail CLOSED, with the fix.
        return 2, "", (
            "BLOCKED (fail closed): " + slug + "'s config/gating.json mounts channels but the "
            "canopy fleet gating baseline (agent-core/gating-baseline.json) is unreadable. "
            "Fix: run /canopy:update (or `uv tool install canopy` / check "
            "~/.claude/plugins/installed_plugins.json), then retry.\n")

    for rule in (rails or []) + cfg.get("deny", []):
        if rule.get("requires_path") and not os.path.exists(
            os.path.join(repo_dir, rule["requires_path"])
        ):
            continue
        if matches(rule, tool_name, subject):
            msg = rule.get("message") or ("BLOCKED by " + slug + " gating policy (deny rule).")
            return 2, "", msg.rstrip() + "\n"

    for rule in cfg.get("approve", []):
        if matches(rule, tool_name, subject):
            return 0, json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": approval_reason(rule, tool_name, subject, cwd, name),
                }
            }), ""

    return 0, "", ""


def main(repo_dir=None):
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)               # never block on a parse failure
    repo_dir = repo_dir or os.environ.get("CANOPY_AGENT_REPO") or os.getcwd()
    code, out, err = run(repo_dir, payload)
    if out:
        print(out)
    if err:
        sys.stderr.write(err)
    sys.exit(code)


if __name__ == "__main__":
    main()
