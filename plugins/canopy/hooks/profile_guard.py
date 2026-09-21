#!/usr/bin/env python3
"""Confine a CALLER's session to its capability — the enforcement half of the declared interface.

canopy-web decides, per turn, whether the person who asked is the agent's owner
or an admin (full profile) or a caller — anyone else — who reaches only the
capability the agent declares for them (`config/interface.yaml`; canopy-web
`apps/agents/interface.py`). The runner opens every caller turn in its OWN
emdash session, named `cx-…`, and writes that capability's profile to
`~/.canopy/profiles/<task>.json` BEFORE the session starts. This hook, registered
by the canopy plugin for every session on the box, is what makes the profile
binding: in a `cx-` session, a tool call that the profile does not list is
refused.

**Allowlist, fail closed.** Everything not listed is denied. A `cx-` session
whose profile is missing, unreadable or malformed denies EVERYTHING — a caller's
session must never fall back to the full profile because a file was absent.

**Found from the transcript path, not the cwd.** Claude Code keys a session's
transcript directory on the directory the session STARTED in, and never moves
it; `cwd` follows `cd`. So the task name is read from the transcript directory
(`…-worktrees-<repo>[-<hex>]-emdash-<task>-<suffix>`), anchored at the FIRST
`-emdash-` after `-worktrees-` so an email subject slugged into a name cannot
move the anchor. Any `-emdash-cx-` anywhere that does not anchor cleanly is
treated as restricted-and-unresolvable: deny. The inverse cannot happen — a
restricted session always has `-emdash-cx-` right at the anchor.

**Cheap for everyone else.** A session that is not `cx-` is decided from the
transcript path STRING alone: no file is opened, so the hook costs every
ordinary tool call on the box one JSON parse and one substring test.

Profile shape (written by the runner from canopy-web's envelope):
    {"version": 1, "capability": {"name", "tools", "bash", "read_paths", …},
     "thread_id": "…", "turn_id": "…"}

  tools       fnmatch patterns over the TOOL NAME (`Read`, `mcp__canopy-web__who_is_asking`)
  bash        patterns over the command's ARGUMENTS, token for token: `*` is one
              argument; a command with an operator, $() or backticks is refused
  read_paths  fnmatch patterns a path-taking tool's target (resolved) must match;
              `{cwd}` is the session's working directory. Empty → no path allowed.

`{thread_id}` in a pattern is the conversation's thread, so a caller's session
can read and answer THEIR thread and no other.
"""
import fnmatch
import json
import os
import re
import shlex
import sys

#: The runner reads this to decide whether it may report `profiles` to canopy-web.
#: Bump only when the profile contract changes.
PROFILE_ENFORCEMENT_VERSION = 1

PROFILE_ROOT = os.path.expanduser("~/.canopy/profiles")
_ANCHOR = re.compile(r"-worktrees-.+?-emdash-(?P<leaf>.+)$")
_RESTRICTED = "cx-"
#: Refused anywhere in a caller's command, quoted or not: the shell still expands
#: `$(…)` and backticks inside double quotes, and a caller's command is ONE
#: invocation of an allowed program. Bodies travel by --body-file, never inline.
_NEVER = re.compile(r"[`$\n\r\\]")
_OPERATOR = set(";&|<>()")
#: Tools that act on a path, and the input keys that carry it.
_PATH_KEYS = {
    "Read": ("file_path",), "Write": ("file_path",), "Edit": ("file_path",),
    "MultiEdit": ("file_path",), "NotebookRead": ("notebook_path",),
    "NotebookEdit": ("notebook_path",), "Glob": ("path",), "Grep": ("path",), "LS": ("path",),
}


def restricted_task(transcript_path: str):
    """(is_restricted, task_candidates) from the transcript path alone.

    EVERY path component is examined, not just the parent directory: a
    subagent's transcript lives deeper (`<project-dir>/<session>/subagents/…`),
    and reading only the parent there would call a caller's session
    unrestricted.
    """
    parts = [p for p in (transcript_path or "").split("/") if p]
    marked = [p for p in parts if "-emdash-cx-" in p]
    if not marked:
        return False, []
    m = _ANCHOR.search(marked[0])
    leaf = m.group("leaf") if m else ""
    if not leaf.startswith(_RESTRICTED):
        return True, []                     # looks restricted, cannot resolve: deny
    # emdash appends a random `-<suffix>` to the task name; older layouts did not.
    base, _, _ = leaf.rpartition("-")
    return True, [leaf] + ([base] if base.startswith(_RESTRICTED) else [])


def load_profile(candidates, root=None):
    root = root or PROFILE_ROOT
    for name in candidates:
        if not re.fullmatch(r"cx-[a-z0-9-]{1,200}", name):
            continue
        try:
            with open(os.path.join(root, f"{name}.json"), encoding="utf-8") as fh:
                prof = json.load(fh)
        except (OSError, ValueError):
            continue
        cap = prof.get("capability") if isinstance(prof, dict) else None
        if isinstance(cap, dict):
            return prof
    return None


def _subst(pattern: str, prof: dict, cwd: str) -> str:
    return (pattern.replace("{thread_id}", str(prof.get("thread_id") or "\0no-thread\0"))
                   .replace("{cwd}", cwd or "\0no-cwd\0"))


def argv(cmd: str):
    """The command's arguments, or None if it is anything but one plain invocation.

    Parsed with shlex in punctuation mode, so an UNQUOTED operator (`;`, `&&`, `|`,
    `>`, `(`) comes out as its own token and is refused, while the same character
    inside quotes — a subject like "Re: payments (Q3)" — is just text.
    """
    if _NEVER.search(cmd):
        return None
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:                        # unbalanced quotes
        return None
    if not toks or any(t and set(t) <= _OPERATOR for t in toks):
        return None
    return toks


def _argv_matches(toks, pattern: str, prof, cwd) -> bool:
    """Token for token: `*` stands for exactly ONE argument, never several — so
    `--body-file *` cannot absorb a trailing `--to attacker@example.com`. A token
    matched against a `{cwd}/…` pattern must also RESOLVE inside the worktree,
    which is what stops `{cwd}/../../.ssh/id_rsa`."""
    rcwd = os.path.realpath(cwd) if cwd else ""
    try:
        want = shlex.split(_subst(pattern, prof, rcwd))
    except ValueError:
        return False
    if len(want) != len(toks):
        return False
    for tok, pat in zip(toks, want):
        if not fnmatch.fnmatchcase(tok, pat):
            return False
        if rcwd and pat.startswith(rcwd + "/"):
            real = os.path.realpath(os.path.join(rcwd, tok))
            if not real.startswith(rcwd + "/"):
                return False
    return True


def _path_ok(path: str, patterns, prof, cwd) -> bool:
    """The resolved target must match a pattern. Resolving (realpath) is what
    defeats `..` and symlinks; `{cwd}` is resolved the same way so the two
    sides compare like for like (macOS alone maps /var → /private/var)."""
    if not path:
        return False
    rcwd = os.path.realpath(cwd) if cwd else ""
    real = os.path.realpath(os.path.join(rcwd or "/", os.path.expanduser(path)))
    # A directory is inside `{cwd}/**` when its contents are: test it with the
    # trailing slash too, or a bare Grep at the worktree root is refused.
    targets = (real, real + "/") if os.path.isdir(real) else (real,)
    return any(fnmatch.fnmatchcase(t, os.path.expanduser(_subst(p, prof, rcwd)))
               for p in patterns for t in targets)


def decide(tool: str, tool_input: dict, prof: dict, cwd: str):
    """None to allow, else the reason to refuse."""
    cap = prof["capability"]
    name = cap.get("name") or "?"
    tools = cap.get("tools") or []
    if tool == "Bash":
        cmd = str((tool_input or {}).get("command") or "").strip()
        toks = argv(cmd)
        if toks is None:
            return (f"this session answers a caller through '{name}'; a command here is one "
                    "program with its arguments — no chaining, pipes, redirection, $() or backticks")
        if any(_argv_matches(toks, p, prof, cwd) for p in cap.get("bash") or []):
            return None
        return f"this session answers a caller through '{name}', which does not allow: {cmd[:160]}"
    if not any(fnmatch.fnmatchcase(tool, p) for p in tools):
        return f"this session answers a caller through '{name}', which does not include the {tool} tool"
    if tool == "Glob":
        # The PATTERN can name a path too (`/etc/**`, `../**`), and would list
        # names outside the worktree whatever `path` says.
        pat = str((tool_input or {}).get("pattern") or "")
        if pat.startswith(("/", "~")) or ".." in pat.split("/"):
            return f"this session answers a caller through '{name}'; that pattern reaches outside what it may touch"
    keys = _PATH_KEYS.get(tool)
    if keys:
        paths = [str((tool_input or {}).get(k) or "") for k in keys]
        if tool in ("Glob", "Grep", "LS") and not any(paths):
            paths = [cwd]
        for p in paths:
            if not _path_ok(p, cap.get("read_paths") or [], prof, cwd):
                return f"this session answers a caller through '{name}'; {p or 'that path'} is outside what it may touch"
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0                            # not a hook payload we understand; not ours to judge
    restricted, candidates = restricted_task(data.get("transcript_path", ""))
    if not restricted:
        return 0
    prof = load_profile(candidates)
    if prof is None:
        print("canopy: this is a caller's session and its profile could not be found, so "
              "nothing is allowed. The runner writes ~/.canopy/profiles/<task>.json before "
              "the session starts.", file=sys.stderr)
        return 2
    why = decide(data.get("tool_name", ""), data.get("tool_input") or {}, prof,
                 data.get("cwd") or "")
    if why:
        print(f"canopy: {why}.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
