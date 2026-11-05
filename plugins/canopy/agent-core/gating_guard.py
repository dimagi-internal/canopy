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

  tool           exact tool-name match — EXCEPT `"Bash"`, which means "any shell tool" (see
                 SHELL_TOOLS). A rail written against a command line applies to every shell
                 the harness offers unless it opts out with `bash_only`.
  bash_only      restrict a `"tool": "Bash"` rule to the Bash tool alone. For rails about the
                 SHELL rather than the command — e.g. zsh EQUALS expansion, which PowerShell
                 does not have.
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
  unquoted       match the shell subject with its quoted strings ('…', "…") blanked out first, so
                 a rail about a COMMAND does not fire on a commit message or echo that only
                 mentions it (and a `;` inside quotes does not split a statement).
  agent_only     skip this `always` rail in a session that belongs to no agent. For rails that
                 translate an AGENT's own rails (e.g. PowerShell HTTP writes -> the agent's curl
                 rails): a human's session has no such rails, so the translation only blocks.

## Whose rails: the SESSION's agent (canopy#849, 2026-10-09)

Every agent plugin is installed at user scope, so a gating hook an agent plugin registers
fires in EVERY session on the machine — ACE's body-file rail blocked a `gh pr create` in an
Ada session on 2026-10-09. The fix is that canopy registers ONE PreToolUse hook (this file,
run with `--session`) and that hook works out whose session it is:

  1. `$CANOPY_AGENT_SLUG`, then `$CANOPY_AGENT` — a runner turn, or an agent repo's own
     .claude/settings.json `env`. The env wins over the directory because a turn may work in
     another repo's checkout and is still that agent's turn.
  2. else the agent repo at or above `$CLAUDE_PROJECT_DIR` (else the payload's `cwd`): the
     first directory carrying `config/gating.json` or `config/agent.json`.
  3. else nobody — a human's own session.

It then applies the fleet baseline plus THAT agent's `deny` list, read from that agent's
repo (or, when the slug is known but the repo isn't under the session — a turn working in a
sibling repo — from the agent's installed plugin in ~/.claude/plugins/cache). With nobody,
only the channel-independent `always` baseline rails apply, minus those marked `agent_only`:
channel rails name an agent's own send path, which a human's session doesn't have.

The per-agent LOADERS (`hooks/gating_guard.py` in each agent repo) still run this file
without `--session`, during the migration away from them. They now enforce only when the
session's agent IS their agent and exit 0 otherwise — so a sibling's rails never fire in
your session, whether or not that sibling has dropped its hook yet.

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
        with open(os.path.join(repo_dir, "config", "agent.json"), encoding="utf-8") as fh:
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


# `{turn_run_dir}` in a rail's PATTERN: this turn's own per-turn env directory, relative to
# /run/canopy/ (a runner that keeps secrets off disk writes /run/canopy/<uid>/<turn>/env and
# names it in CANOPY_AGENT_ENV_FILE), regex-escaped — so a rail can deny every other turn's
# directory and still let this turn read its own. No such file → a pattern that never
# matches, so the rail denies all of /run/canopy/.
RUN_CANOPY = "/run/canopy/"
NEVER = "(?!)"


def turn_run_dir(env=None):
    env = os.environ if env is None else env
    f = (env.get("CANOPY_AGENT_ENV_FILE") or "").strip()
    d = os.path.dirname(os.path.normpath(f)) if f else ""
    if not d.startswith(RUN_CANOPY) or ".." in d.split("/"):
        return NEVER
    return re.escape(d[len(RUN_CANOPY):])


def _substituted(rule, slug):
    out = {k: (v.replace("{slug}", slug) if isinstance(v, str) else v) for k, v in rule.items()}
    if isinstance(out.get("pattern"), str) and "{turn_run_dir}" in out["pattern"]:
        out["pattern"] = out["pattern"].replace("{turn_run_dir}", turn_run_dir())
    return out


def load_baseline():
    """The fleet baseline (agent-core/gating-baseline.json) as a dict, or None if unreadable.

    CANOPY_PLUGIN_DIR overrides the plugin dir (tests / unusual installs). Otherwise the file
    next to THIS engine wins: the session hook runs this file out of the plugin it ships in, so
    its sibling is the baseline that plugin version was built with. The installed-plugins
    registry is the last resort (an engine copy run from somewhere unusual)."""
    candidates = []
    plugin_dir = os.environ.get("CANOPY_PLUGIN_DIR")
    if plugin_dir:
        candidates.append(os.path.join(plugin_dir, "agent-core", "gating-baseline.json"))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gating-baseline.json"))
        try:
            reg = json.load(open(os.path.expanduser("~/.claude/plugins/installed_plugins.json"), encoding="utf-8"))
            candidates.append(os.path.join(reg["plugins"]["canopy@canopy"][0]["installPath"],
                                           "agent-core", "gating-baseline.json"))
        except Exception:
            pass
    for path in candidates:
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
    return None


def baseline_rails(cfg, slug, agent=True):
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

    `agent=False` is a session that belongs to no agent: `always` rails only, minus the ones
    marked `agent_only` (they translate an agent's own rails, which a human session lacks).
    `cfg["channels"] == "*"` mounts every channel — used when the session's agent is known
    but its config cannot be found, so the safe assumption is that it mounts everything."""
    channels = cfg.get("channels") or []
    base = load_baseline()
    if base is None:
        return None if channels else []
    if channels == "*":
        channels = list((base.get("channels") or {}).keys())
    rails = [_substituted(rule, slug) for rule in base.get("always", [])
             if agent or not rule.get("agent_only")]
    for ch in channels:
        for rule in base.get("channels", {}).get(ch, []):
            rails.append(_substituted(rule, slug))
    return rails


# Every tool that takes a command LINE. On Windows, Claude Code offers PowerShell beside Bash,
# and until 2026-09-22 every rail in the fleet silently applied to Bash alone, in two layers:
# the factory matcher never routed PowerShell to this hook, and even when it did, `subject_for`
# fell through to the JSON-of-input path, so the command arrived as `{"command": "gog gmail
# send …"}` and every rail anchored on `(?:^|[\n;&|(])` missed on the leading quote. And a
# third layer neither of those fixes would have reached: every rail says `"tool": "Bash"`,
# which `matches` compared EXACTLY. Measured on fizzy (Shayoni Mazumdar): `gog gmail send`, a
# Salesforce `curl -X POST` and `python sf-refresh.py` were blocked in Bash and allowed in
# PowerShell. So a shell rail means every shell — one family, not a PowerShell copy of each
# rail, which would drift the day someone added a rail and forgot its twin.
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})


def subject_for(tool_name, tool_input):
    """The string a rule's pattern is tested against, per tool.

    Anything that is not a known built-in falls back to the JSON of its whole input — which is
    how an MCP tool becomes railable at all. Before 2026-08-13 this returned "" for every MCP
    tool, so a rail could not inspect an MCP call's ARGUMENTS: a rule with a pattern could
    never match, and a rule without one matched unconditionally (blocking the tool outright).
    That left every Drive-creating MCP tool outside the filing rails while Bash was railed."""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name in SHELL_TOOLS:
        return tool_input.get("command", "") or ""
    if tool_name in ("Edit", "Write", "NotebookEdit", "Read", "NotebookRead"):
        return tool_input.get("file_path", "") or tool_input.get("notebook_path", "") or ""
    # A search tool is railed on WHERE it looks, never on what it searches FOR: a Grep for
    # the text "~/.hal/.env" or "/etc/canopy-runner" is code search, not a read of the file.
    if tool_name == "Grep":
        return tool_input.get("path", "") or ""
    if tool_name == "Glob":
        return " ".join(str(tool_input.get(k) or "") for k in ("path", "pattern")).strip()
    try:
        return json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        return ""


def summarize_action(tool_name, subject):
    """A crisp, human-readable summary of the GATED action — so the approval prompt says exactly
    WHAT you're approving at a glance, not a generic 'needs approval' over a wall of bash."""
    if tool_name not in SHELL_TOOLS:
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


def tool_applies(rule, tool_name):
    """Does the rule's `tool` cover this tool? `"Bash"` covers every shell unless `bash_only`."""
    want = rule.get("tool")
    if not want or want == tool_name:
        return True
    return want == "Bash" and tool_name in SHELL_TOOLS and not rule.get("bash_only")


_QUOTED = re.compile(r"'[^']*'|" + r'"(?:[^"\\]|\\.)*"', re.S)


def _unquoted(subject):
    """`subject` with every quoted string replaced by a bare token (`Q`)."""
    return _QUOTED.sub("Q", subject)


def matches(rule, tool_name, subject):
    """Does this rule fire on this call?"""
    if not tool_applies(rule, tool_name):
        return False
    if rule.get("unquoted") and tool_name in SHELL_TOOLS:
        subject = _unquoted(subject)
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
    """Evaluate one PreToolUse payload against the agent repo at `repo_dir`.
    Returns (exit_code, stdout, stderr)."""
    try:
        cfg = json.load(open(config_path(repo_dir), encoding="utf-8"))
    except Exception:
        return 0, "", ""          # no/broken config = no extra gating

    slug, name = agent_labels(repo_dir, cfg)
    return _evaluate(cfg, repo_dir, payload, slug, name)


# Read-only file tools reach this hook since the agent credential read rails (2026-10-19).
# A person's own session was never gated on them, and still is not: every rail that
# applies to them is an agent rail, so in a session that belongs to no agent they pass.
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "NotebookRead"})


def _evaluate(cfg, repo_dir, payload, slug, name, agent=True):
    tool_name = payload.get("tool_name", "")
    if not agent and tool_name in READ_TOOLS:
        return 0, "", ""
    subject = subject_for(tool_name, payload.get("tool_input"))
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "")

    rails = baseline_rails(cfg, slug, agent=agent)
    if rails is None and tool_name not in FAIL_OPEN_TOOLS:
        # channels are mounted but the fleet baseline is unreadable — fail CLOSED, with the fix.
        return 2, "", (
            "BLOCKED (fail closed): " + slug + "'s config/gating.json mounts channels but the "
            "canopy fleet gating baseline (agent-core/gating-baseline.json) is unreadable. "
            "Fix: run /canopy:update (or `uv tool install canopy` / check "
            "~/.claude/plugins/installed_plugins.json), then retry.\n")

    for rule in (rails or []) + cfg.get("deny", []):
        if rule.get("requires_path") and not (repo_dir and os.path.exists(
            os.path.join(repo_dir, rule["requires_path"])
        )):
            continue
        if matches(rule, tool_name, subject):
            msg = rule.get("message") or ("BLOCKED by " + slug + " gating policy (deny rule).")
            return 2, "", msg.rstrip() + "\n"

    # The own-repo rail needs facts a regex cannot hold (who is asking, which repo is the
    # agent's own), so it is code, not a rule. After the rules: a forged-envelope attempt is
    # refused by its own baseline rail first.
    if agent and tool_name in SHELL_TOOLS:
        refusal = own_repo_refusal(subject, payload, slug, repo_dir)
        if refusal:
            return 2, "", refusal.rstrip() + "\n"

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


# ── whose session is this? ────────────────────────────────────────────────────────────

# Strongest first. CANOPY_AGENT_SLUG is what a runner turn carries; CANOPY_AGENT is what an
# agent repo's .claude/settings.json `env` sets (and the cloud runner too).
AGENT_ENV_VARS = ("CANOPY_AGENT_SLUG", "CANOPY_AGENT")
# Repos that carry agent-ish markers but are not agents (mirrors canopy_web._NON_AGENT_SLUGS).
NON_AGENT_SLUGS = frozenset({"canopy"})
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.I)
# When no agent owns the session, `{slug}` in a baseline message reads as this.
NO_AGENT_LABEL = "canopy"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_agent_repo(path):
    return (os.path.isfile(os.path.join(path, "config", "gating.json"))
            or os.path.isfile(os.path.join(path, "config", "agent.json")))


def repo_slug(repo_dir):
    """An agent repo's slug: gating.json `slug`, agent.json `slug`, plugin.json `name`, then the
    directory name. The directory is last because a worktree's name is the TASK, not the agent
    (emdash names them `emdash-<task>`)."""
    for rel, key in ((("config", "gating.json"), "slug"),
                     (("config", "agent.json"), "slug"),
                     ((".claude-plugin", "plugin.json"), "name")):
        val = _read_json(os.path.join(repo_dir, *rel)).get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return os.path.basename(repo_dir.rstrip("/")).lower()


def find_agent_repo(start):
    """The nearest directory at or above `start` that is an agent repo, or None."""
    if not start:
        return None
    d = os.path.abspath(os.path.expanduser(start))
    while True:
        if is_agent_repo(d):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _version_key(v):
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-+]", v))


def installed_agent_repo(slug, home=None):
    """The installed plugin dir of agent `slug` that ships config/gating.json, or None.

    The registry's own record first (`<slug>@<marketplace>`), then the highest version under
    ~/.claude/plugins/cache/<marketplace>/<slug>/<version>/."""
    home = home or os.path.expanduser("~")
    plugins = os.path.join(home, ".claude", "plugins")
    reg = _read_json(os.path.join(plugins, "installed_plugins.json")).get("plugins") or {}
    for key, entries in reg.items():
        if key.split("@", 1)[0] != slug or not isinstance(entries, list):
            continue
        for entry in entries:
            path = (entry or {}).get("installPath") if isinstance(entry, dict) else None
            if path and os.path.isfile(config_path(path)):
                return path
    found = []
    cache = os.path.join(plugins, "cache")
    try:
        for mkt in os.listdir(cache):
            base = os.path.join(cache, mkt, slug)
            if not os.path.isdir(base):
                continue
            for ver in os.listdir(base):
                if os.path.isfile(config_path(os.path.join(base, ver))):
                    found.append((_version_key(ver), os.path.join(base, ver)))
    except OSError:
        pass
    return max(found)[1] if found else None


def env_agent_slug(env=None):
    env = os.environ if env is None else env
    for var in AGENT_ENV_VARS:
        slug = (env.get(var) or "").strip().lower()
        if slug and slug not in NON_AGENT_SLUGS and _SLUG_RE.match(slug):
            return slug
    return ""


def session_agent(payload, env=None):
    """(slug, repo_dir) of the agent whose session this is; ("", None) for nobody's.

    repo_dir is None when the slug is known (env) but its config cannot be found."""
    env = os.environ if env is None else env
    start = env.get("CLAUDE_PROJECT_DIR") or (payload or {}).get("cwd") or ""
    repo = find_agent_repo(start)
    slug = env_agent_slug(env)
    if slug:
        if repo and repo_slug(repo) == slug:
            return slug, repo
        return slug, installed_agent_repo(slug)
    if repo:
        rs = repo_slug(repo)
        if rs and rs not in NON_AGENT_SLUGS:
            return rs, repo
    return "", None


def run_session(payload, env=None):
    """The canopy session hook: apply the SESSION's agent's rails (see the module doc)."""
    slug, repo = session_agent(payload, env)
    if slug and repo:
        return run(repo, payload)
    if slug:
        # Known agent, config nowhere to be found. Assume it mounts every channel rather than
        # none: an unknown config must cost a blocked raw send, never let one through.
        return _evaluate({"slug": slug, "channels": "*"}, None, payload, slug, slug.title())
    return _evaluate({}, None, payload, NO_AGENT_LABEL, NO_AGENT_LABEL, agent=False)


def run_loader(repo_dir, payload, env=None):
    """A per-agent LOADER's call (no `--session`). Enforce only in that agent's own session.

    During the migration both this and the session hook fire. The session hook already applies
    the session's agent's rails, so a sibling agent's loader must stand down — that is the
    whole bug (canopy#849). In the agent's OWN session it still enforces, which keeps its
    fail-closed contract even if the session hook were somehow absent."""
    try:
        cfg = json.load(open(config_path(repo_dir), encoding="utf-8"))
    except Exception:
        return 0, "", ""
    own = repo_slug(repo_dir)
    session_slug, _ = session_agent(payload, env)
    if session_slug != own:
        return 0, "", ""
    return run(repo_dir, payload)


# ── the own-repo rail ─────────────────────────────────────────────────────────────────
#
# canopy-web decides who may change an agent (the `agent_edit` permission) for every change
# made THROUGH canopy-web. It cannot see what a session does with `git` and `gh` directly, and
# an agent can read and write GitHub freely (other repos, issues, PRs are a feature). The one
# thing a turn's asker without `agent_edit` may not do is change THE AGENT'S OWN REPO, so that
# is the whole rail: a push to it, a PR/release/repo-settings/workflow/secret/variable write on
# it, a mutating `gh api repos/<it>/...` or the same call through curl/wget, or a GraphQL mutation
# that names it or cannot be told not to be on it. Issues and comments stay open (not closing a PR
# through its issue), and so does a PR from a FORK.
#
# This is a guard rail, not a boundary. It reads the command's own words; a session that
# builds the command out of variables, a script file or another tool is not seen
# (docs: agent-core/own-repo.md § What it does not cover).
#
# It fires only for a session that HAS an agent and a canopy turn (a turn id from the runner's
# env, a local turn, or the laptop's by-task record): a person running Claude Code in the
# agent's repo by hand has no turn and is untouched. Given a turn, a missing, unreadable or
# forged envelope FAILS CLOSED.

OWN_REPO_PERMISSION = "agent_edit"
#: Envelope `relationship`s that may change the agent when an older canopy-web sent no
#: `permissions` list (the owner and admins hold agent_edit; `system` is the agent's own login).
_PRIVILEGED_RELATIONSHIPS = frozenset({"owner", "admin", "system"})
_UUIDISH = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_NAMEISH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_PREFILTER = re.compile(r"\bgit\b|\bgh\b|\bcurl\b|\bwget\b")
_GITHUB_REPO = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s?#]+?)(?:\.git)?/*(?:[?#].*)?$", re.I)
_PLAIN_REPO = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+?)(?:\.git)?$")
# The shell keywords a command can follow on its own line (`then gh pr merge 3`) count too.
_SHELL_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!"})
_WRAPPERS = frozenset({"sudo", "env", "command", "exec", "nohup", "time", "nice", "builtin", "xargs"}) | _SHELL_KEYWORDS
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# A word `$(cmd)` that stands alone in a command's arguments: what it prints becomes more arguments.
_OPAQUE_ARGS = "\0$(args)"
# Commands that can change a shell variable without looking like `NAME=value`.
_VARIABLE_WRITERS = frozenset({"read", "for", "select", "mapfile", "readarray", "printf", "declare",
                               "typeset", "local", "export", "unset", "getopts", "let"})
_EXPANSION = re.compile(r"[$`]")
# gh's own commands; another first word is an alias of the person's (or an extension).
_GH_COMMANDS = frozenset({"accessibility", "agent-task", "alias", "api", "attestation", "auth", "browse",
                          "cache", "co", "codespace", "completion", "config", "copilot", "extension",
                          "gist", "gpg-key", "help", "issue", "label", "licenses", "org", "pr", "preview",
                          "project", "release", "repo", "ruleset", "run", "search", "secret", "ssh-key",
                          "status", "variable", "workflow"})
_GH_PR_WRITES = frozenset({"create", "merge", "edit", "close", "review", "ready", "reopen",
                           "lock", "unlock", "update-branch"})
_GH_REPO_WRITES = frozenset({"edit", "delete", "rename", "archive", "unarchive", "sync",
                             "deploy-key"})
_GH_RELEASE_WRITES = frozenset({"create", "edit", "delete", "upload", "delete-asset"})
# `gh workflow|secret|variable` writes. A secret or variable set with --org/--user is not the repo's.
_GH_WORKFLOW_WRITES = frozenset({"run", "enable", "disable"})
_GH_SETTING_WRITES = frozenset({"set", "delete"})
_GH_NOT_THE_REPO_FLAGS = frozenset({"-o", "--org", "-u", "--user"})
_GH_API_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
# gh api flags that take a value (so the value is not mistaken for the endpoint).
_GH_API_VALUE_FLAGS = frozenset({"-X", "--method", "-H", "--header", "-q", "--jq", "-t",
                                 "--template", "--hostname", "--cache", "-p", "--preview"}
                                ) | _GH_API_FIELD_FLAGS
# What of an own repo `gh api` may still write: issues and their comments (a PR's
# conversation is an issue's), a PR's review comments, and forking it. Everything else under
# repos/<own>/ (contents, git/refs, pulls, merges, branches, releases, settings…) is the repo.
_GH_API_OPEN_SUBPATHS = re.compile(r"^/(?:issues(?:/|$)|pulls/\d+/comments(?:/|$)|forks/?$|subscription/?$)")
# The one issue write that is not open: PATCH with `state` closes or reopens it, and a PR is an issue.
_ISSUE_NUMBER = re.compile(r"^/issues/\d+/?$")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# GraphQL. A mutation names its target by opaque node id (`pullRequestId: "PR_..."`), which this
# hook cannot map to a repo without a network call, so it fails closed: while the rail is active a
# mutation is refused unless its root fields are the issue-and-comment writes below, or the one
# mutation whose input really carries its repo names another repo. A repo named anywhere else (a
# spare variable, say) does not count, or naming any other repo would unlock a merge of this one.
# A query the hook cannot read at all (a file, stdin, an unresolved variable) is refused the same
# way, since it may be a mutation. Reads stay free.
_GRAPHQL_TOKEN = re.compile(r'"""(.*?)"""|"((?:[^"\\]|\\.)*)"|#[^\n]*', re.S)
_GRAPHQL_OPEN_MUTATIONS = frozenset({
    "addComment", "updateIssueComment", "deleteIssueComment", "minimizeComment", "unminimizeComment",
    "createIssue", "updateIssue", "closeIssue", "reopenIssue", "addReaction", "removeReaction",
    "addPullRequestReviewComment", "addPullRequestReviewThreadReply",
})
_GRAPHQL_NAMES_ITS_REPO = "createCommitOnBranch"
_GRAPHQL_REPO_FIELD = re.compile(r'\brepositoryNameWithOwner\s*:\s*(?:"(\d+)"|\$(\w+))')
_GRAPHQL_BRANCH_BY_ID = re.compile(r"\bbranch\s*:\s*\{[^{}]*\bid\s*:")
# In every refusal that fails closed, so the message can say what to do instead.
_GRAPHQL_BLIND = "the hook cannot read"
_GRAPHQL_UNREAD = f"a GraphQL request whose query {_GRAPHQL_BLIND}"
_GRAPHQL_HINT = (" It is treated as your own repo because it cannot be told apart from it, whichever "
                 "repo you meant. For a PR on any repo use `gh pr merge|close|edit ... -R <owner/repo>` "
                 "or REST (`gh api repos/<owner>/<repo>/...`), which name the repo. A read goes through "
                 "if its query is on the command line or in a variable set in the same command. "
                 "`createCommitOnBranch` may name another repo with `repositoryNameWithOwner`. Taking a "
                 "PR out of a merge queue has no gh or REST form: use the PR's page.")
_GRAPHQL_URL_IN_A_VARIABLE = re.compile(r"^\S*[$`]\S*/graphql/?(?:[?#]\S*)?$", re.I)
_GRAPHQL_OWNER_NAME = re.compile(r'\bowner\s*:\s*"([^"]+)"\s*,?\s*name\s*:\s*"([^"]+)"')
_GRAPHQL_NAME_OWNER = re.compile(r'\bname\s*:\s*"([^"]+)"\s*,?\s*owner\s*:\s*"([^"]+)"')
# A whole value that IS a GitHub repo address (a URL below it counts; a sentence holding one does not).
_REPO_ADDRESS = re.compile(r"^(?:[a-z]+://)?(?:[^/@\s]+@)?(?:www\.)?github\.com[:/]+([^/\s]+)/([^/\s?#]+?)"
                           r"(?:\.git)?(?:[/?#]\S*)?$", re.I)
# curl and wget against the REST API (the host carries a token as user:token@ in some calls).
_GITHUB_API_URL = re.compile(r"^(?:https?://)?(?:[^/@\s]+@)?(?:api|uploads)\.github\.com(?:/api/v3)?"
                             r"(/[^?#\s]*)?(?:[?#]\S*)?$", re.I)
_CURL_DATA_FLAGS = frozenset({"-d", "--data", "--data-raw", "--data-binary", "--data-ascii",
                              "--data-urlencode", "--json", "-F", "--form", "--form-string"})
_CURL_UPLOAD_FLAGS = frozenset({"-T", "--upload-file"})
_CURL_SHORT_VALUE_FLAGS = frozenset("AbcCdDeEFHKmoPQrtTuUwxXyYz")
_WGET_DATA_FLAGS = frozenset({"--post-data", "--post-file", "--body-data", "--body-file"})
# git global options that take a value, so `git -C dir push` finds `push`.
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                             "--super-prefix", "--config-env"})


def repo_key(text):
    """`owner/repo` (lowercase) for a GitHub URL, ssh address or bare `owner/repo`, else ''."""
    text = (text or "").strip().strip("'\"")
    m = _GITHUB_REPO.search(text) or _PLAIN_REPO.match(text)
    return f"{m.group(1)}/{m.group(2)}".lower() if m else ""


def _substitution_end(text, i):
    """The index of the `)` closing a `$(` whose body starts at `i` (len(text) if none)."""
    depth, n = 1, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2; continue
        if c == "'":
            j = text.find(c, i + 1)
            i = (n if j < 0 else j) + 1; continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1; continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if not depth:
                return i
        i += 1
    return n


def _scan_commands(text, depth=0):
    """`text` as a list of simple commands, each a list of words (quotes removed).

    Quote-aware, unlike _statements: `;`, `&&`, `||`, `|`, `&`, `(`, `)` and newlines end a
    command only outside quotes, heredoc bodies are dropped (a PR body that quotes `git push`
    is not a push), and `$(…)`/backtick text inside quotes is scanned as commands too."""
    cmds, words, cur = [], [], []
    quoted_inner, quoted_substitutions = [], []
    i, n = 0, len(text)
    pending_heredocs = []
    in_word = False

    def end_word():
        nonlocal cur, in_word
        if in_word:
            words.append("".join(cur))
        cur, in_word = [], False

    def end_cmd():
        nonlocal words
        end_word()
        if words:
            cmds.append(words)
        words = []

    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            cur.append(text[i + 1]); in_word = True; i += 2; continue
        if c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            cur.append(text[i + 1:j]); in_word = True; i = j + 1; continue
        if c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1]); j += 2
                else:
                    if text.startswith("$(", j):   # from the raw text: it may hold quotes and parens
                        end = _substitution_end(text, j + 2)
                        if end < n:
                            quoted_substitutions.append(text[j + 2:end])
                    buf.append(text[j]); j += 1
            body = "".join(buf)
            cur.append(body); in_word = True; i = j + 1
            quoted_inner.append(body)
            continue
        if c == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            quoted_inner.append(text[i + 1:j]); in_word = True; i = j + 1; continue
        if c == "\n":
            end_cmd()
            i += 1
            while pending_heredocs:
                delim = pending_heredocs.pop(0)
                while i < n:
                    j = text.find("\n", i)
                    j = n if j < 0 else j
                    line = text[i:j]
                    i = j + 1
                    if line.strip() == delim:
                        break
            continue
        if c in " \t":
            end_word(); i += 1; continue
        if c == "<" and text.startswith("<<", i) and not text.startswith("<<<", i):
            m = re.match(r"<<-?\s*(['\"]?)([A-Za-z0-9_.-]+)\1", text[i:])
            if m:
                pending_heredocs.append(m.group(2))
                i += m.end(); continue
        if c in ";|(){}" or (c == "&" and not (cur and cur[-1].endswith((">", "<")))):
            if c in "{}" and in_word:
                cur.append(c); i += 1; continue
            if c in "{}":                      # `{ cmd; }` grouping: a word, never a command end
                i += 1; continue
            end_cmd(); i += 1; continue
        if c == "$" and text.startswith("$(", i):
            if in_word and depth < 3:          # `https://x:$(cmd)@host/…`: the word goes on
                j = _substitution_end(text, i + 2)
                cmds.extend(_scan_commands(text[i + 2:j], depth + 1))
                cur.append("$()"); i = j + 1; continue
            if words:                          # its output is more of this command's arguments
                words.append(_OPAQUE_ARGS)
            end_cmd(); i += 2; continue        # `$(cmd)`: cmd is a command of its own
        cur.append(c); in_word = True; i += 1
    end_cmd()
    if depth < 3:
        for inner in quoted_substitutions:
            cmds.extend(_scan_commands(inner, depth + 1))
        for body in quoted_inner:
            for inner in re.findall(r"`([^`]*)`", body):
                cmds.extend(_scan_commands(inner, depth + 1))
    return cmds


def _strip_wrappers(words):
    """Drop leading env assignments and wrappers (`sudo`, `env`, `command`, `timeout 5`…)."""
    w = list(words)
    while w:
        head = os.path.basename(w[0])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
            w.pop(0)
        elif head in _WRAPPERS:
            w.pop(0)
            while w and (w[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0])):
                w.pop(0)
        elif head in ("timeout", "nice") and len(w) > 1:
            w.pop(0)
            while w and (w[0].startswith("-") or re.match(r"^[\d.]+[smhd]?$", w[0])):
                w.pop(0)
        else:
            break
    return w


def _drop_redirects(words):
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
            continue
        if re.match(r"^\d*(?:>>?|<|&>>?)$", w):
            skip = True
            continue
        if re.match(r"^\d*(?:>>?|<|&>)\S", w):
            continue
        out.append(w)
    return out


def _git_run(args, cwd, timeout=4):
    import subprocess
    try:
        r = subprocess.run(["git", *args], cwd=cwd or None, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _origin_key(cwd, remote="origin"):
    return repo_key(_git_run(["remote", "get-url", remote], cwd)) if cwd and os.path.isdir(cwd) else ""


def _default_push_remote(cwd):
    if not cwd or not os.path.isdir(cwd):
        return ""
    up = _git_run(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}"], cwd)
    return up.split("/", 1)[0] if "/" in up else "origin"


def own_repo_keys(slug, repo_dir, start_cwd, caller=None):
    """`owner/repo` keys naming THIS agent's repo: its clone's `origin` (the repo dir the hook
    resolved, and the repo above the session's directory when it is this agent's), the repo URL
    in config/agent.json or .claude-plugin/plugin.json, and the envelope's `agent_repo` when the
    envelope is this agent's."""
    dirs = []
    for d in (repo_dir, find_agent_repo(start_cwd)):
        if d and os.path.isdir(d) and d not in dirs and repo_slug(d) == slug:
            dirs.append(d)
    keys = set()
    for d in dirs:
        keys.add(_origin_key(d))
        for rel, field in ((("config", "agent.json"), "repo_url"), (("config", "agent.json"), "repo"),
                           ((".claude-plugin", "plugin.json"), "repository")):
            val = _read_json(os.path.join(d, *rel)).get(field)
            keys.add(repo_key(val.get("url") if isinstance(val, dict) else val))
    if isinstance(caller, dict) and str(caller.get("agent") or "").lower() == slug:
        repo = caller.get("agent_repo")
        keys.add(repo_key(repo) if isinstance(repo, str) else "")
    keys.discard("")
    return keys


class _AnyRepo:
    """Stands in for the own-repo keys when none can be found: every repo might be it."""

    def __contains__(self, key):
        return True


def envelope_knows_of_no_repo(caller):
    """Whether this canopy-web answered and had no repo to name. Only a string says so: `null` is a
    turn with no agent, and an envelope without the field is an older canopy-web, which never said."""
    return isinstance(caller, dict) and isinstance(caller.get("agent_repo"), str)


def caller_root():
    return os.path.realpath(os.path.expanduser("~/.canopy/caller"))


def _turn_id_for(payload, env):
    """This session's canopy turn id, or "": the runner's `$CANOPY_TURN_ID`; a LOCAL turn's own
    state (`~/.canopy/local/<claude session>.json`); the laptop's by-task record (unless the
    owner has since taken over the keyboard: presence `keyboard`)."""
    tid = (env.get("CANOPY_TURN_ID") or "").strip()
    if _UUIDISH.match(tid):
        return tid
    home = os.path.expanduser("~/.canopy")
    sid = str((payload or {}).get("session_id") or env.get("CLAUDE_CODE_SESSION_ID") or "")
    if sid and _NAMEISH.match(sid):
        state = _read_json(os.path.join(home, "local", f"{sid}.json"))
        if state and not state.get("closed_at") and _UUIDISH.match(str(state.get("turn_id") or "")):
            return str(state["turn_id"])
        if _read_json(os.path.join(home, "caller", "presence", f"{sid}.json")).get("origin") == "keyboard":
            return ""
    transcript = (payload or {}).get("transcript_path") or ""
    cwd = (payload or {}).get("cwd") or env.get("CLAUDE_PROJECT_DIR") or ""
    leaves = []
    m = re.search(r"-worktrees-.+?-emdash-(?P<leaf>[^/]+)$",
                  next((p for p in transcript.split("/") if "-worktrees-" in p), ""))
    if m:
        leaves.append(m.group("leaf"))
    base = os.path.basename(os.path.normpath(cwd)) if cwd else ""
    if base:
        leaves.append(base[len("emdash-"):] if base.startswith("emdash-") else base)
    for leaf in leaves:
        for name in (leaf, leaf.rpartition("-")[0]):
            if name and _NAMEISH.match(name):
                rec = _read_json(os.path.join(home, "caller", "by-task", f"{name}.json"))
                if _UUIDISH.match(str(rec.get("turn_id") or "")):
                    return str(rec["turn_id"])
    return ""


def read_caller_envelope(turn_id):
    """canopy's envelope for `turn_id` — only `<caller root>/<turn_id>.json`, a file whose own
    `turn_id` is the one in its name — or None."""
    root = caller_root()
    path = os.path.realpath(os.path.join(root, f"{turn_id}.json"))
    if os.path.dirname(path) != root:
        return None
    doc = _read_json(path)
    if not doc or str(doc.get("turn_id") or turn_id) != turn_id:
        return None
    return doc


def may_edit_agent(env):
    """(allowed, held) for an envelope. `permissions` is canopy-web's own list of what the
    asker holds on THIS agent for THIS turn (a confined turn lists read and a door only);
    an older canopy-web sent none, so fall back to the relationship (owner/admin/system)."""
    perms = env.get("permissions")
    if isinstance(perms, list) and all(isinstance(p, str) for p in perms):
        return OWN_REPO_PERMISSION in perms, sorted(perms)
    who = env.get("who") if isinstance(env.get("who"), dict) else {}
    ok = (env.get("relationship") in _PRIVILEGED_RELATIONSHIPS or who.get("kind") == "system") \
        and env.get("profile") != "confined"
    return ok, []


def _chdir(cwd, target):
    """`cd target` from `cwd`; None when the target is a variable, glob or substitution."""
    if re.search(r"[$`*]", target):
        return None
    t = os.path.expanduser(target)
    if os.path.isabs(t):
        return os.path.normpath(t)
    return os.path.normpath(os.path.join(cwd, t)) if cwd else None


def _global_git_cwd(words, cwd):
    """(subcommand index, working dir) for `git [-C dir …] <sub> …`, dir None if unresolvable."""
    i, d = 1, cwd
    while i < len(words):
        w = words[i]
        if w == "-C" and i + 1 < len(words):
            d = _chdir(d, words[i + 1])
            i += 2
        elif w in _GIT_VALUE_OPTS and "=" not in w:
            i += 2
        elif w.startswith("-"):
            i += 1
        else:
            break
    return i, d


def _git_push_target(words, cwd, start, remotes=None):
    """The `owner/repo` key a `git push` writes to, or '' when it cannot be told. `remotes`:
    remotes an earlier `git remote add|set-url` in this same command gave a URL."""
    args, i, repo_opt = [], start + 1, ""
    while i < len(words):
        w = words[i]
        if w in ("-o", "--push-option", "--receive-pack", "--exec") and "=" not in w:
            i += 2; continue
        if w == "--repo" and i + 1 < len(words):
            repo_opt = words[i + 1]; i += 2; continue
        if w.startswith("--repo="):
            repo_opt = w.split("=", 1)[1]
        elif not w.startswith("-"):
            args.append(w)
        i += 1
    target = repo_opt or (args[0] if args else "")
    if target and ("://" in target or re.match(r"^[\w.-]+@[\w.-]+:", target) or repo_key(target)):
        return repo_key(target)
    remote = target or _default_push_remote(cwd)
    if remote in (remotes or {}):
        return repo_key(remotes[remote])
    if not remote or not cwd or not os.path.isdir(cwd):
        return ""
    return repo_key(_git_run(["remote", "get-url", "--push", remote], cwd))   # a pushurl counts


def _remote_url_set(words, start):
    """(remote, url) for `git remote add|set-url … <remote> <url>`, else None."""
    args = [a for a in words[start + 1:] if not a.startswith("-")]
    return (args[1], args[2]) if len(args) >= 3 and args[0] in ("add", "set-url") else None


def _gh_parts(words):
    """(repo flag value, positionals after `gh`) with global/-R flags removed."""
    repo, pos, i = "", [], 1
    while i < len(words):
        w = words[i]
        if w in ("-R", "--repo") and i + 1 < len(words):
            repo = words[i + 1]; i += 2; continue
        if w.startswith("--repo="):
            repo = w.split("=", 1)[1]
        elif w.startswith("-R") and len(w) > 2 and not w.startswith("--"):
            repo = w[2:]
        elif not w.startswith("-"):
            pos.append(w)
        i += 1
    return repo, pos


def _gh_api_args(words):
    """(method, fields as [(key, value)], has --input, endpoint, other positionals) of `gh api …`."""
    method, fields, has_input, endpoint, extras, i = "", [], False, "", [], 2

    def field(text):
        key, _, value = text.partition("=")
        fields.append((key, value))

    while i < len(words):
        w = words[i]
        flag, eq, val = w.partition("=")
        if flag in ("-X", "--method"):
            method = (val if eq else words[i + 1] if i + 1 < len(words) else "").upper()
            i += 1 if eq else 2; continue
        if flag == "--input":
            has_input = True
            i += 1 if eq else 2; continue
        if flag in _GH_API_FIELD_FLAGS:
            field(val if eq else words[i + 1] if i + 1 < len(words) else "")
            i += 1 if eq else 2; continue
        if w.startswith(("-X", "-f", "-F")) and len(w) > 2 and not w.startswith("--"):
            if w.startswith("-X"):
                method = w[2:].upper()
            else:
                field(w[2:])
        elif flag in _GH_API_VALUE_FLAGS:
            i += 1 if eq else 2; continue
        elif not w.startswith("-"):
            if endpoint:
                extras.append(w)
            else:
                endpoint = w
        i += 1
    return method, fields, has_input, endpoint, extras


def _gh_api(words, cwd, here=""):
    """(repo key, subpath, method, sets `state`) a MUTATING `gh api` writes under `repos/`, else
    None. `here` is the repo `{owner}/{repo}` means (else the origin of `cwd`)."""
    method, fields, has_input, endpoint, _ = _gh_api_args(words)
    mutating = method in _MUTATING_METHODS or (not method and (fields or has_input))
    if not mutating:
        return None
    ep = re.sub(r"^https?://[^/]+/(?:api/v3/)?", "", endpoint).lstrip("/")
    m = re.match(r"^repos/([^/]+)/([^/?#]+)(/[^?#]*)?", ep)
    if not m:
        return None
    owner, name, sub = m.group(1), m.group(2), m.group(3) or ""
    if owner == "{owner}" or name == "{repo}":
        key = here or _origin_key(cwd)
    else:
        key = f"{owner}/{name}".lower()
    return key, sub, method, any(k == "state" for k, _ in fields)


def _repo_write_is_open(sub, method, sets_state):
    """Whether a mutating REST call on repos/<own><sub> is one of the writes that stay open."""
    if method == "PATCH" and sets_state and _ISSUE_NUMBER.match(sub):
        return False
    return bool(_GH_API_OPEN_SUBPATHS.match(sub))


def _graphql_mutation(doc):
    """(root fields of the document's `mutation` operations, its string literals, whether it can be
    read). Strings and comments are set aside first, so `mutation` inside one is not an operation.

    GraphQL's own `$variables` live inside parentheses, so a `$` or backtick left outside them is the
    shell's, and what it expands to could be anything. Likewise a document that does not balance."""
    literals = []

    def set_aside(m):
        if m.group(0).startswith('"'):
            literals.append(m.group(1) if m.group(1) is not None else m.group(2))
        return " "

    skeleton = re.sub(r"\([^()]*\)", " ", _GRAPHQL_TOKEN.sub(set_aside, doc or ""))
    readable = bool(skeleton.strip(" \t\r\n,﻿")) and not re.search(r'[$`()"]', skeleton)
    depth, head, body, is_mutation = 0, [], [], False
    for c in skeleton:
        if c == "{":
            depth += 1
            if depth == 1:
                # GraphQL ignores commas and a BOM, and `mutation@d{` needs no space.
                is_mutation = bool(re.match(r"[\s,﻿]*mutation\b", "".join(head)))
                head = []
            elif depth == 2 and is_mutation:
                body.append(" ")
        elif c == "}":
            depth -= 1
            readable = readable and depth >= 0
        elif depth == 0:
            head.append(c)
        elif depth == 1 and is_mutation:
            body.append(c)
    # A fragment spread stands for fields this does not resolve, so it is never an open root.
    roots = [name or "..." for name in re.findall(r"\.\.\.|(?:\w+\s*:\s*)?(\w+)", "".join(body))]
    return roots, literals, readable and depth == 0


def _value_repo(value):
    """The `owner/repo` key when the WHOLE of `value` is a GitHub repo address, else ''."""
    value = (value or "").strip()
    m = _REPO_ADDRESS.match(value) or _PLAIN_REPO.match(value)
    return f"{m.group(1)}/{m.group(2)}".lower() if m else ""


def _commits_name_their_repos(doc, count, by_name):
    """Whether each of `count` createCommitOnBranch roots says its repo in the field that sets it:
    `repositoryNameWithOwner`, a literal or a variable with a plain value, and no branch node id."""
    literals = []

    def mask(m):
        if not m.group(0).startswith('"'):
            return " "
        literals.append(m.group(1) if m.group(1) is not None else m.group(2))
        return f'"{len(literals) - 1}"'

    masked = _GRAPHQL_TOKEN.sub(mask, doc or "")           # so text in a string is not a field
    targets = _GRAPHQL_REPO_FIELD.findall(masked)
    if len(targets) != count or _GRAPHQL_BRANCH_BY_ID.search(masked):
        return False
    values = [literals[int(i)] if i else by_name.get(name, "") for i, name in targets]
    return all(_PLAIN_REPO.match(str(v)) for v in values)


def _graphql_violation(doc, variables, own, here=""):
    """What a GraphQL document may do to the own repo: a short description, or ''. `variables` is
    [(name, value)]. See _GRAPHQL_OPEN_MUTATIONS's comment for what passes."""
    roots, literals, readable = _graphql_mutation(doc)
    if not readable:
        return _GRAPHQL_UNREAD
    if not roots or all(r in _GRAPHQL_OPEN_MUTATIONS for r in roots):
        return ""
    owner_name = here.split("/", 1) if "/" in here else ["", ""]

    def fill(value):
        return value.replace("{owner}", owner_name[0]).replace("{repo}", owner_name[1])

    named = {fill(str(v)) for _, v in variables}
    keys = {_value_repo(v) for v in set(literals) | named}
    keys.update(f"{o}/{n}".lower() for o, n in _GRAPHQL_OWNER_NAME.findall(doc))
    keys.update(f"{o}/{n}".lower() for n, o in _GRAPHQL_NAME_OWNER.findall(doc))
    by_name = {k: fill(str(v)) for k, v in variables}
    if by_name.get("owner") and (by_name.get("name") or by_name.get("repo")):
        keys.add(f"{by_name['owner']}/{by_name.get('name') or by_name.get('repo')}".lower())
    hit = sorted(k for k in keys if k and k in own)
    if hit:
        return f"a GraphQL mutation ({', '.join(roots)}) on {hit[0]}"
    closed = [r for r in roots if r not in _GRAPHQL_OPEN_MUTATIONS]
    if all(r == _GRAPHQL_NAMES_ITS_REPO for r in closed) and _commits_name_their_repos(doc, len(closed), by_name):
        return ""
    return f"a GraphQL mutation ({', '.join(roots)}) whose repo {_GRAPHQL_BLIND}"


def _resolved(value, assigned):
    """`value` if it is not exactly `$NAME`/`${NAME}`; else what `assigned` says NAME holds, if known."""
    m = re.fullmatch(r"\$(?:(\w+)|\{(\w+)\})", value or "")
    return (assigned or {}).get(m.group(1) or m.group(2)) or value if m else value


def _shell_assignments(cmds):
    """{name: literal value} of the shell variables this command line sets with `NAME=value`.

    A name set twice to different values, or touched by another command (`read`, `for`, `export`…),
    has no known value, wherever in the line that happens: a loop can run the later one first.
    `source` and `eval` could set anything, so then nothing is known."""
    found = {}

    def note(name, value):
        found[name] = value if found.get(name, value) == value else None

    for words in cmds:
        words = [w for w in words if w != _OPAQUE_ARGS]
        while words and words[0] in _SHELL_KEYWORDS:
            words = words[1:]
        if not words:
            continue
        if all(re.match(r"^[A-Za-z_]\w*\+?=", w) for w in words):
            for w in words:
                name, _, value = w.partition("=")
                note(name.rstrip("+"), None if name.endswith("+") else value)
            continue
        prog = os.path.basename(words[0])
        if prog in ("source", ".", "eval"):
            return {}
        if prog in _VARIABLE_WRITERS:
            for a in words[1:]:
                name = a.split("=", 1)[0]
                if re.fullmatch(r"[A-Za-z_]\w*", name):
                    found[name] = None
    return found


def _is_graphql_endpoint(endpoint, fields, has_input):
    """Whether a `gh api` endpoint is GraphQL, or is a variable that may be with a query on the line."""
    ep = re.sub(r"^https?://[^/]+/(?:api/)?", "", endpoint).split("?", 1)[0].strip("/").lower()
    m = _EXPANSION.search(ep)
    if not m:
        return ep == "graphql"
    return "graphql".startswith(ep[:m.start()]) and (
        has_input or any(k == "query" or _EXPANSION.search(k) for k, _ in fields))


def _gh_graphql_violation(fields, has_input, extras, opaque, assigned, own, here):
    """_graphql_violation for `gh api graphql`: what the call sends has to be on the command line."""
    queries = [v for k, v in fields if k == "query"]
    if (has_input or opaque or not queries or any(_EXPANSION.search(x) for x in extras)
            or any(_EXPANSION.search(k) for k, _ in fields) or any(q.startswith("@") for q in queries)):
        return _GRAPHQL_UNREAD
    variables = [f for f in fields if f[0] != "query"]
    for query in queries:                                  # gh sends the last; read them all
        hit = _graphql_violation(_resolved(query, assigned), variables, own, here)
        if hit:
            return hit
    return ""


def _http_graphql_violation(bodies, own, assigned):
    """_graphql_violation for a curl/wget POST: one JSON body, on the line, holding the query."""
    doc = _json_object(_resolved(bodies[0], assigned)) if len(bodies) == 1 else None
    if doc is None or not isinstance(doc.get("query"), str):
        return _GRAPHQL_UNREAD
    return _graphql_violation(doc["query"], _flatten(doc.get("variables") or {}), own)


def _gh_aliases(config_dir=""):
    """{name: expansion} from gh's config file (no gh subprocess, and nothing is sent anywhere)."""
    base = config_dir or os.environ.get("GH_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or "~/.config", "gh")
    try:
        with open(os.path.join(os.path.expanduser(base), "config.yml"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, ValueError):
        return {}
    aliases, inside, indent, block, block_lines = {}, False, None, None, []

    def finish_block():
        nonlocal block, block_lines
        if block:
            aliases[block] = "\n".join(s.strip() for s in block_lines)
        block, block_lines = None, []

    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        width = len(line) - len(line.lstrip())
        if width == 0:
            finish_block()
            inside, indent = line.strip() == "aliases:", None
            continue
        if not inside:
            continue
        indent = width if indent is None else indent
        if width > indent:
            if block:
                block_lines.append(line)
            continue
        finish_block()
        name, sep, value = line.strip().partition(":")
        if not sep:
            continue
        name, value = name.strip("'\""), value.strip()
        if value[:1] in ("|", ">"):
            block = name
        elif len(value) > 1 and value[0] == value[-1] == "'":
            aliases[name] = value[1:-1].replace("''", "'")
        elif len(value) > 1 and value[0] == value[-1] == '"':
            try:
                aliases[name] = json.loads(value)
            except ValueError:
                aliases[name] = value[1:-1]
        else:
            aliases[name] = value
    finish_block()
    return aliases


def _alias_commands(expansion, rest=(), shell=False):
    """An alias's expansion as shell text: `!cmd` and --shell run cmd, anything else is `gh <it>`."""
    import shlex
    expansion = expansion.strip()
    if shell or expansion.startswith("!"):
        return expansion.lstrip("!")
    return " ".join(["gh", expansion, *map(shlex.quote, rest)])


def _gh_alias_set_violation(w, pos, cwd, own, depth):
    """What `gh alias set|import` would later let `gh <alias>` do, or ''."""
    # `$1` is gh's own placeholder; it is read below, when the expansion is scanned.
    if pos[1] == "import" or len(pos) < 4 or pos[3] == "-" or re.search(r"`|\$(?!\d)", pos[3]):
        return f"`gh alias {pos[1]}` with an expansion that is not on the command line"
    if depth >= 3:
        return ""
    shell = any(a in ("-s", "--shell") for a in w)
    return _violation(_scan_commands(_alias_commands(pos[3], shell=shell)), cwd, own, depth + 1)


def _flatten(value, name=""):
    """A JSON value's leaves as [(name, value)]."""
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _flatten(v, k)]
    if isinstance(value, list):
        return [p for v in value for p in _flatten(v, name)]
    return [(name, value)]


def _json_object(text):
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _body_sets_state(bodies):
    """Whether a request body (JSON, or text that would be once a shell fills it) sets `state`."""
    for body in bodies:
        doc = _json_object(body)
        if ("state" in doc) if doc is not None else re.search(r'"state"\s*:', body):
            return True
    return False


def _curl_call(words):
    """(method, inline bodies, url candidates) of a `curl` command."""
    method, bodies, urls, get, head, upload, i = "", [], [], False, False, False, 1
    while i < len(words):
        w = words[i]
        if w.startswith("--"):
            flag, eq, val = w.partition("=")
            takes = flag in ("--request", "--url") or flag in _CURL_DATA_FLAGS or flag in _CURL_UPLOAD_FLAGS
            if takes and not eq:
                val = words[i + 1] if i + 1 < len(words) else ""
                i += 1
            if flag == "--request":
                method = val.upper()
            elif flag == "--url":
                urls.append(val)
            elif flag in _CURL_DATA_FLAGS:
                bodies.append(val)
            elif flag in _CURL_UPLOAD_FLAGS:
                upload = True
            elif flag == "--get":
                get = True
            elif flag == "--head":
                head = True
        elif w.startswith("-") and len(w) > 1:
            for j, c in enumerate(w[1:], 2):
                if c in _CURL_SHORT_VALUE_FLAGS:
                    val = w[j:]
                    if not val:
                        val = words[i + 1] if i + 1 < len(words) else ""
                        i += 1
                    if c == "X":
                        method = val.upper()
                    elif f"-{c}" in _CURL_DATA_FLAGS:
                        bodies.append(val)
                    elif f"-{c}" in _CURL_UPLOAD_FLAGS:
                        upload = True
                    break
                get = get or c == "G"
                head = head or c == "I"
        else:
            urls.append(w)
        i += 1
    if not method:
        method = "HEAD" if head else "GET" if get else "PUT" if upload else "POST" if bodies else "GET"
    return method, bodies, urls


def _wget_call(words):
    """(method, inline bodies, url candidates) of a `wget` command."""
    method, bodies, urls, posts, i = "", [], [], False, 1
    while i < len(words):
        w = words[i]
        flag, eq, val = w.partition("=")
        if flag == "--method" or flag in _WGET_DATA_FLAGS:
            if not eq:
                val = words[i + 1] if i + 1 < len(words) else ""
                i += 1
            if flag == "--method":
                method = val.upper()
            else:
                posts = True
                if flag.endswith("-data"):
                    bodies.append(val)
        elif not w.startswith("-"):
            urls.append(w)
        i += 1
    return method or ("POST" if posts else "GET"), bodies, urls


def _http_violation(words, prog, own, assigned=None):
    """What a curl/wget command writes to the own repo through the GitHub API, or ''."""
    method, bodies, urls = (_curl_call if prog == "curl" else _wget_call)(words)
    # A curl config file can set the method and body this cannot see, so a GraphQL URL is not a read.
    config = prog == "curl" and any(re.match(r"^(?:-[A-Za-z]*K|--config(?:=|$))", a) for a in words[1:])
    mutating = method in _MUTATING_METHODS
    if not mutating and not config:
        return ""
    for url in urls:
        m = _GITHUB_API_URL.match(url)
        path = (m.group(1) or "") if m else ""
        if path.rstrip("/") == "/graphql" or _GRAPHQL_URL_IN_A_VARIABLE.match(url):
            hit = _http_graphql_violation(bodies, own, assigned)
            if hit:
                return f"{hit} through `{prog}`"
            continue
        if not mutating:
            continue
        r = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if r and f"{r.group(1)}/{r.group(2)}".lower() in own:
            sub = r.group(3) or ""
            if not _repo_write_is_open(sub, method, _body_sets_state(bodies)):
                return f"a {method} to repos/{r.group(1)}/{r.group(2)}{sub} through `{prog}`"
    return ""


def own_repo_violation(subject, cwd, own):
    """What in `subject` writes to the agent's own repo: a short description, or ''."""
    return _violation(_scan_commands(subject), cwd, own, 0)


def _gh_env(words, name="GH_REPO"):
    """A variable assigned in front of a command (`gh` reads `GH_REPO` like -R), or ''."""
    for w in words:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) and os.path.basename(w) not in _WRAPPERS:
            break
        if w.startswith(f"{name}="):
            return w.split("=", 1)[1]
    return ""


def _violation(cmds, cwd, own, depth):
    remotes = {}
    assigned = _shell_assignments(cmds)
    for words in cmds:
        opaque = _OPAQUE_ARGS in words
        words = [x for x in words if x != _OPAQUE_ARGS]
        w = _drop_redirects(_strip_wrappers(words))
        if not w:
            continue
        if w[0] == "cd":
            cwd = _chdir(cwd, w[1] if len(w) > 1 else "~")
            continue
        prog = os.path.basename(w[0])
        if prog in _SHELLS and depth < 3:
            for k, a in enumerate(w[1:-1], 1):
                if a.startswith("-") and "c" in a.lstrip("-") and not a.startswith("--"):
                    hit = _violation(_scan_commands(w[k + 1]), cwd, own, depth + 1)
                    if hit:
                        return hit
                    break
            continue
        if prog == "eval" and depth < 3:
            hit = _violation(_scan_commands(" ".join(w[1:])), cwd, own, depth + 1)
            if hit:
                return hit
            continue
        if prog == "git":
            i, d = _global_git_cwd(w, cwd)
            if i < len(w) and w[i] == "push":
                key = _git_push_target(w, d, i, remotes)
                if key in own:                   # never "" unless every repo might be the own one
                    return f"`git push` to {key or 'a repo the hook cannot name'}"
            elif i + 1 < len(w) and w[i] == "remote" and _remote_url_set(w, i):
                name, url = _remote_url_set(w, i)
                remotes[name] = url
        elif prog in ("curl", "wget"):
            hit = _http_violation(w, prog, own, assigned)
            if hit:
                return hit
        elif prog == "gh":
            repo_flag, pos = _gh_parts(w)
            repo_flag = repo_flag or _gh_env(words)
            group, action = (pos + ["", ""])[:2]
            if group == "alias" and action in ("set", "import"):
                hit = _gh_alias_set_violation(w, pos, cwd, own, depth)
                if hit:
                    return hit
            elif group and group not in _GH_COMMANDS and not _EXPANSION.search(group) and depth < 3:
                expansion = _gh_aliases(_gh_env(words, "GH_CONFIG_DIR")).get(group)
                if expansion:                    # the person's alias: what it runs is what counts
                    rest = w[w.index(group) + 1:]
                    hit = _violation(_scan_commands(_alias_commands(expansion, rest)), cwd, own, depth + 1)
                    if hit:
                        return f"{hit} (through the gh alias `{group}`)"
            elif group == "api":
                here = repo_key(repo_flag) or _origin_key(cwd)
                method, fields, has_input, endpoint, extras = _gh_api_args(w)
                if _is_graphql_endpoint(endpoint, fields, has_input) or (opaque and not endpoint):
                    hit = _gh_graphql_violation(fields, has_input, extras, opaque, assigned, own, here)
                    if hit:
                        return f"{hit} through `gh api graphql`"
                    continue
                hit = _gh_api(w, cwd, here)
                if hit and hit[0] in own and not _repo_write_is_open(hit[1], hit[2], hit[3]):
                    what = "a state change (a PR is an issue) to" if _ISSUE_NUMBER.match(hit[1]) else "a write to"
                    return f"{what} repos/{hit[0]}{hit[1]} through `gh api`"
            elif group == "workflow" and action in _GH_WORKFLOW_WRITES or (
                    group in ("secret", "variable") and action in _GH_SETTING_WRITES):
                if group != "workflow" and any(a.split("=", 1)[0] in _GH_NOT_THE_REPO_FLAGS for a in w):
                    continue                     # an org's or a user's, not the repo's
                target = repo_key(repo_flag) or _origin_key(cwd)
                if target in own:
                    return f"`gh {group} {action}` on {target}"
            elif (group, action) and (
                    (group == "pr" and action in _GH_PR_WRITES)
                    or (group == "repo" and action in _GH_REPO_WRITES)
                    or (group == "release" and action in _GH_RELEASE_WRITES)):
                target = repo_key(repo_flag)
                if not target:
                    for p in pos[2:]:
                        if "github.com" in p:
                            target = repo_key(re.sub(r"/(?:pull|issues)/.*$", "", p))
                            break
                if not target and group == "repo" and len(pos) > 2:
                    target = repo_key(pos[2])
                if not target:
                    target = _origin_key(cwd)
                if target in own:
                    if group == "pr" and action == "create":
                        head = ""
                        for k, a in enumerate(w):
                            if a in ("-H", "--head") and k + 1 < len(w):
                                head = w[k + 1]
                            elif a.startswith("--head="):
                                head = a.split("=", 1)[1]
                        owner = target.split("/")[0]
                        if ":" in head and head.split(":", 1)[0].lower() != owner:
                            continue                 # a PR from a fork: a suggestion
                    return f"`gh {group} {action}` on {target}"
    return ""


def own_repo_refusal(subject, payload, slug, repo_dir, env=None):
    """The deny message when `subject` (a shell command) writes to this agent's own repo in a
    turn whose asker lacks `agent_edit`; "" otherwise. See the block comment above."""
    if not subject or not _PREFILTER.search(subject):
        return ""
    env = os.environ if env is None else env
    start = env.get("CLAUDE_PROJECT_DIR") or (payload or {}).get("cwd") or ""
    cwd = (payload or {}).get("cwd") or start or os.getcwd()
    # Cheap exits first: no turn → a person's own session, untouched.
    turn_id = _turn_id_for(payload, env)
    if not turn_id:
        return ""
    caller = read_caller_envelope(turn_id)
    own = own_repo_keys(slug, repo_dir, cwd or start, caller)
    if not own:
        # Nothing here or in the envelope says which repo is the agent's. An envelope that does
        # not carry the field is an older canopy-web, and one that is missing is routine (the
        # runner prunes them after a week while a laptop's by-task record stays), so neither may
        # lock anyone out: the rail is off as it always was. Only a canopy-web that answered "no
        # repo" makes this fail closed, and only for an asker the rail guards.
        if not envelope_knows_of_no_repo(caller) or may_edit_agent(caller)[0]:
            return ""
        what = own_repo_violation(subject, os.path.abspath(cwd) if cwd else None, _AnyRepo())
        if not what:
            return ""
        return (f"BLOCKED (fail closed): {what}. canopy cannot tell which repo is {slug}'s own: "
                f"canopy-web has none set for {slug} and this session is not in its clone, so this "
                f"could be it, and the person asking this turn does not hold `{OWN_REPO_PERMISSION}`. "
                "Reads, issues and comments, and a PR from a FORK still work. Ask "
                f"{slug}'s owner to set its repo on canopy-web, or to give this person the editor role.")
    what = own_repo_violation(subject, os.path.abspath(cwd) if cwd else None, own)
    if not what:
        return ""
    hint = _GRAPHQL_HINT if _GRAPHQL_BLIND in what else ""
    if caller is None:
        return (f"BLOCKED (fail closed): {what}. That counts as changing {slug}'s own repo, and this "
                f"turn's caller envelope (~/.canopy/caller/{turn_id}.json) is missing or unreadable, "
                f"so canopy cannot tell who is asking. Only people with the editor role on {slug} "
                "may change its repo. Ask the person to retry from canopy-web, or file an issue "
                f"instead (`gh issue create -R <owner/repo>` is allowed).{hint}")
    allowed, held = may_edit_agent(caller)
    if allowed:
        return ""
    repo = sorted(own)[0]
    return (f"BLOCKED: {what}.{hint} Only people with the editor role on {slug} may change {slug}'s "
            f"repo; the person asking this turn holds {', '.join(held) or 'no edit permission'} "
            f"(no `{OWN_REPO_PERMISSION}`). You can still open or comment on an issue "
            f"(`gh issue create -R {repo}`), or open a PR from a FORK "
            f"(`gh repo fork {repo}`, push to your fork, `gh pr create -R {repo} --head "
            "<your-fork-owner>:<branch>`). Or ask the owner of "
            f"{slug} to give them the editor role, then ask again.")


def main(repo_dir=None, argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)               # never block on a parse failure
    if not isinstance(payload, dict):
        sys.exit(0)
    if "--session" in argv:
        code, out, err = run_session(payload)
    else:
        repo_dir = repo_dir or os.environ.get("CANOPY_AGENT_REPO") or os.getcwd()
        code, out, err = run_loader(repo_dir, payload)
    if out:
        print(out)
    if err:
        sys.stderr.write(err)
    sys.exit(code)


if __name__ == "__main__":
    main()
