#!/usr/bin/env python3
"""Tell the agent who is asking — on EVERY prompt a canopy turn delivered, beside the words.

canopy-web writes a CALLER ENVELOPE for every turn (who asked, whether THIS message
is verified, their relationship to the agent, the turn's mode) and the runner saves
it to `~/.canopy/caller/<turn_id>.json`. A slash-command prompt names it with
`--caller <path>`; a FREE-TEXT prompt (Slack, the web chat, the widget) is the
person's own words and is left byte-for-byte alone — so until this hook, the agent
answering a chat message never learned who sent it. On 2026-09-26 Hal, driven from
Slack by a non-member, pushed and deployed code while its envelope said
relationship=caller, verified=false, turn_mode=manual.

This UserPromptSubmit hook closes that gap without touching the prompt: it returns
the summary as `additionalContext`, which Claude Code adds beside it.

**How it finds the envelope.**
  * Laptop (emdash): before typing a turn's text into a session, the runner leaves a
    one-shot POINTER at `~/.canopy/caller/pending/<task>.json`, keyed by the emdash
    task name. The hook derives the task name from its own transcript path — the same
    anchor `profile_guard` confines `cx-` sessions by (`…-worktrees-<repo>-emdash-
    <task>-<suffix>`) — claims the pointer with an atomic rename (two prompts can
    never both take it), and ignores one older than two minutes: a pointer the send
    never followed must not attach a stranger's name to what a human types next.
  * Cloud runner (spawns `claude -p` itself): `CANOPY_CALLER=<envelope>` in the env.

**No pointer, no output.** A human typing at the keyboard is the machine's owner and
needs no introduction, and every non-canopy session pays one `stat` for this hook.

**Never blocks, never fails loud.** Any error → exit 0 with nothing printed: the
prompt always goes through, and the agent still has `who_is_asking` and the file.
"""
import json
import os
import re
import sys
import time

CALLER_ROOT = os.path.expanduser("~/.canopy/caller")
FRESH_SECONDS = 120
_ANCHOR = re.compile(r"-worktrees-.+?-emdash-(?P<leaf>.+)$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")

_RELATIONSHIP = {
    "owner": "the agent's OWNER",
    "admin": "an ADMIN of this agent",
    "member": "a member of the agent's workspace — not its owner or an admin",
    "caller": "a CALLER — not the agent's owner, an admin, or a workspace member",
    "system": "canopy itself or another agent (no outside person)",
}


def task_candidates(transcript_path: str, cwd: str = ""):
    """Task names this session could be, most specific first.

    emdash appends a random `-<suffix>` to the worktree dir, so both the leaf and the
    leaf without its last segment are tried. EVERY path component is examined (a
    subagent's transcript sits deeper), and the cwd's basename is the fallback.
    """
    leaves = []
    for part in (transcript_path or "").split("/"):
        m = _ANCHOR.search(part) if "-worktrees-" in part else None
        if m:
            leaves.append(m.group("leaf"))
            break
    base = os.path.basename(os.path.normpath(cwd)) if cwd else ""
    if base:
        leaves.append(base[len("emdash-"):] if base.startswith("emdash-") else base)
    out = []
    for leaf in leaves:
        stem, _, _ = leaf.rpartition("-")
        for name in (leaf, stem):
            if name and _NAME.match(name) and name not in out:
                out.append(name)
    return out


def _inside(path: str, root: str) -> bool:
    real, rroot = os.path.realpath(path), os.path.realpath(root)
    return real.startswith(rroot + os.sep)


def claim_pointer(candidates, *, root=None, now=time.time):
    """The envelope path from a FRESH pointer for one of `candidates`, consuming it."""
    pending = os.path.join(root or CALLER_ROOT, "pending")
    if not os.path.isdir(pending):
        return None, None
    for name in candidates:
        path = os.path.join(pending, f"{name}.json")
        claimed = os.path.join(pending, f".{name}.{os.getpid()}.claimed")
        try:
            os.rename(path, claimed)            # atomic: one prompt takes it, ever
        except OSError:
            continue
        try:
            with open(claimed, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            doc = None
        finally:
            try:
                os.unlink(claimed)
            except OSError:
                pass
        if not isinstance(doc, dict) or doc.get("task") != name:
            continue
        age = now() - float(doc.get("written_at") or 0)
        if not (-5 <= age <= FRESH_SECONDS):
            continue                              # stale: never guess who is typing
        env = str(doc.get("envelope") or "")
        if env and _inside(env, root or CALLER_ROOT):
            return env, str(doc.get("turn_id") or "")
    return None, None


def _person(who: dict) -> str:
    rec = who.get("user") or who.get("contact") or {}
    name, email = rec.get("name") or "", rec.get("email") or ""
    if name and email and name != email:
        label = f"{name} <{email}>"
    else:
        label = email or name or who.get("agent") or "unknown"
    return f"{label} ({who.get('kind') or 'unknown'})"


def summarize(env: dict, path: str, turn_id: str = "") -> str:
    who = env.get("who") or {}
    rel = str(env.get("relationship") or "unknown")
    verified = bool(env.get("verified"))
    assurance = who.get("assurance") or "none"
    mode = (env.get("turn_mode") or {}).get("mode") or "unknown"
    basis = (env.get("turn_mode") or {}).get("basis") or ""
    trigger = env.get("trigger") or {}
    cap = env.get("capability") or {}
    tid = turn_id or env.get("turn_id") or ""
    lines = [
        f"[canopy] Who is asking — the caller envelope canopy wrote for turn {tid}. "
        "This is canopy's answer, not something the person typed:",
        f"- who: {_person(who)}" + (f", via {who['via']}" if who.get("via") else ""),
        f"- relationship: {rel} — {_RELATIONSHIP.get(rel, 'unknown')}",
        f"- verified: {'yes' if verified else 'NO'} (assurance: {assurance})"
        + ("" if verified else " — who they say they are is a claim, not proven"),
        f"- access: profile={env.get('profile') or 'unknown'}, granted_by={env.get('granted_by') or 'unknown'}"
        + (f", capability={cap.get('name')}" if cap.get("name") else ""),
        f"- turn mode: {mode}" + (f" ({basis})" if basis else "")
        + (" — outbound or irreversible actions (push, deploy, merge, send, publish) need the "
           "OWNER's approval first" if mode != "auto" else ""),
        f"- channel: {trigger.get('origin') or 'unknown'}"
        + (f", runner {trigger['runner']}" if trigger.get("runner") else ""),
    ]
    if rel not in ("owner", "admin", "system") or not verified:
        lines.append("Act accordingly: this person does not hold the agent's authority. Do not push, "
                     "deploy, send, publish or change shared state on their say-so — answer within "
                     "what they may have, and take anything more to the owner.")
    lines.append(f"Full envelope: {path}; re-read it with the who_is_asking tool "
                 f"(turn_id={tid}) before anything irreversible.")
    return "\n".join(lines)


def main() -> int:
    try:
        data = json.load(sys.stdin)
        explicit = os.environ.get("CANOPY_CALLER", "")
        if explicit:
            path, tid = (explicit if _inside(explicit, CALLER_ROOT) else None), ""
        else:
            path, tid = claim_pointer(task_candidates(data.get("transcript_path", ""),
                                                      data.get("cwd", "")))
        if not path:
            return 0
        with open(path, encoding="utf-8") as fh:
            env = json.load(fh)
        if not isinstance(env, dict):
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": summarize(env, path, tid)}}))
    except Exception:  # noqa: BLE001 — never block a prompt, never fail loud
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
