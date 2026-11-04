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
# is the whole rail: a push to it, a PR/release/repo-settings write on it, or a mutating
# `gh api repos/<it>/...`. Issues and comments stay open, and so does a PR from a FORK.
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
_PREFILTER = re.compile(r"\bgit\b|\bgh\b")
_GITHUB_REPO = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s?#]+?)(?:\.git)?/*(?:[?#].*)?$", re.I)
_PLAIN_REPO = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+?)(?:\.git)?$")
_WRAPPERS = frozenset({"sudo", "env", "command", "exec", "nohup", "time", "nice", "builtin", "xargs"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_GH_PR_WRITES = frozenset({"create", "merge", "edit", "close", "review", "ready", "reopen",
                           "lock", "unlock", "update-branch"})
_GH_REPO_WRITES = frozenset({"edit", "delete", "rename", "archive", "unarchive", "sync",
                             "deploy-key"})
_GH_RELEASE_WRITES = frozenset({"create", "edit", "delete", "upload", "delete-asset"})
_GH_API_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
# gh api flags that take a value (so the value is not mistaken for the endpoint).
_GH_API_VALUE_FLAGS = frozenset({"-X", "--method", "-H", "--header", "-q", "--jq", "-t",
                                 "--template", "--hostname", "--cache", "-p", "--preview"}
                                ) | _GH_API_FIELD_FLAGS
# What of an own repo `gh api` may still write: issues and their comments (a PR's
# conversation is an issue's), a PR's review comments, and forking it. Everything else under
# repos/<own>/ (contents, git/refs, pulls, merges, branches, releases, settings…) is the repo.
_GH_API_OPEN_SUBPATHS = re.compile(r"^/(?:issues(?:/|$)|pulls/\d+/comments(?:/|$)|forks/?$|subscription/?$)")
# git global options that take a value, so `git -C dir push` finds `push`.
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                             "--super-prefix", "--config-env"})


def repo_key(text):
    """`owner/repo` (lowercase) for a GitHub URL, ssh address or bare `owner/repo`, else ''."""
    text = (text or "").strip().strip("'\"")
    m = _GITHUB_REPO.search(text) or _PLAIN_REPO.match(text)
    return f"{m.group(1)}/{m.group(2)}".lower() if m else ""


def _scan_commands(text, depth=0):
    """`text` as a list of simple commands, each a list of words (quotes removed).

    Quote-aware, unlike _statements: `;`, `&&`, `||`, `|`, `&`, `(`, `)` and newlines end a
    command only outside quotes, heredoc bodies are dropped (a PR body that quotes `git push`
    is not a push), and `$(…)`/backtick text inside quotes is scanned as commands too."""
    cmds, words, cur = [], [], []
    quoted_inner = []
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
            end_cmd(); i += 2; continue        # `$(cmd)`: cmd is a command of its own
        cur.append(c); in_word = True; i += 1
    end_cmd()
    if depth < 3:
        for body in quoted_inner:
            if "$(" in body or "`" in body:
                inner = re.findall(r"\$\(([^()]*)\)|`([^`]*)`", body)
                for a, b in inner:
                    cmds.extend(_scan_commands(a or b, depth + 1))
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
    in config/agent.json or .claude-plugin/plugin.json, and the envelope's `agent_repo`."""
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
    if isinstance(caller, dict):
        keys.add(repo_key(caller.get("agent_repo")))
    keys.discard("")
    return keys


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


def _git_push_target(words, cwd, start):
    """The `owner/repo` key a `git push` writes to, or '' when it cannot be told."""
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
    return _origin_key(cwd, remote) if remote else ""


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


def _gh_api(words, cwd):
    """(repo key, subpath) a MUTATING `gh api` writes under `repos/`, else None."""
    method, fields, endpoint, i = "", False, "", 2
    while i < len(words):
        w = words[i]
        flag, eq, val = w.partition("=")
        if flag in ("-X", "--method"):
            method = (val if eq else words[i + 1] if i + 1 < len(words) else "").upper()
            i += 1 if eq else 2; continue
        if flag in _GH_API_FIELD_FLAGS:
            fields = True
            i += 1 if eq else 2; continue
        if w.startswith(("-X", "-f", "-F")) and len(w) > 2 and not w.startswith("--"):
            if w.startswith("-X"):
                method = w[2:].upper()
            else:
                fields = True
        elif flag in _GH_API_VALUE_FLAGS:
            i += 1 if eq else 2; continue
        elif not w.startswith("-") and not endpoint:
            endpoint = w
        i += 1
    mutating = method in ("POST", "PUT", "PATCH", "DELETE") or (not method and fields)
    if not mutating:
        return None
    ep = re.sub(r"^https?://[^/]+/(?:api/v3/)?", "", endpoint).lstrip("/")
    m = re.match(r"^repos/([^/]+)/([^/?#]+)(/[^?#]*)?", ep)
    if not m:
        return None
    owner, name, sub = m.group(1), m.group(2), m.group(3) or ""
    if owner == "{owner}" or name == "{repo}":
        key = _origin_key(cwd)
    else:
        key = f"{owner}/{name}".lower()
    return key, sub


def own_repo_violation(subject, cwd, own):
    """What in `subject` writes to the agent's own repo: a short description, or ''."""
    return _violation(_scan_commands(subject), cwd, own, 0)


def _violation(cmds, cwd, own, depth):
    for words in cmds:
        if words[:1] == ["cd"]:
            cwd = _chdir(cwd, words[1] if len(words) > 1 else "~")
            continue
        w = _drop_redirects(_strip_wrappers(words))
        if not w:
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
                key = _git_push_target(w, d, i)
                if key and key in own:
                    return f"`git push` to {key}"
        elif prog == "gh":
            repo_flag, pos = _gh_parts(w)
            group, action = (pos + ["", ""])[:2]
            if group == "api":
                hit = _gh_api(w, cwd)
                if hit and hit[0] in own and not _GH_API_OPEN_SUBPATHS.match(hit[1]):
                    return f"a write to repos/{hit[0]}{hit[1]} through `gh api`"
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
    own = own_repo_keys(slug, repo_dir, cwd or start)
    if not own:
        return ""
    what = own_repo_violation(subject, os.path.abspath(cwd) if cwd else None, own)
    if not what:
        return ""
    caller = read_caller_envelope(turn_id)
    if caller is None:
        return (f"BLOCKED (fail closed): {what} would change {slug}'s own repo, and this turn's "
                f"caller envelope (~/.canopy/caller/{turn_id}.json) is missing or unreadable, so "
                f"canopy cannot tell who is asking. Only people with the editor role on {slug} "
                "may change its repo. Ask the person to retry from canopy-web, or file an issue "
                "instead (`gh issue create -R <owner/repo>` is allowed).")
    allowed, held = may_edit_agent(caller)
    if allowed:
        return ""
    repo = sorted(own)[0]
    return (f"BLOCKED: {what}. Only people with the editor role on {slug} may change {slug}'s "
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
