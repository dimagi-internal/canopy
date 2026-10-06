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

**It also leaves a durable parent record** at `~/.canopy/caller/by-task/<task>.json`
(`turn_id`, `session_id`, `claude_session_id`, `updated_at`) when it claims a laptop
pointer. The pointer is one-shot and emdash cannot pass env vars, so without this a
`canopy` CLI call made later in the same session (a dispatch, a script) could not say
which turn started it; `orchestrator/provenance.py` reads it to send the
`X-Canopy-Parent-*` headers.

**No pointer, no output.** A human typing at the keyboard is the machine's owner and
needs no introduction, and every non-canopy session pays one `stat` for this hook.

**Never blocks, never fails loud.** Any error → exit 0 with nothing printed: the
prompt always goes through, and the agent still has `who_is_asking` and the file.

**Vocabulary.** Every word in the summary — the agent roles (`owner` / `admin` /
`member` / `contact` / `system`), access (`full` / `confined` / `none`),
`granted_by`, turn mode — is defined in canopy-web's
`docs/architecture/access.md` (ACCESS_DOC below). Envelope VERSION 2 renamed
`relationship: caller` → `contact` and `profile: restricted` → `confined`; this
hook reads both and always prints the new word.
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

ACCESS_DOC = "https://github.com/dimagi-internal/canopy-web/blob/main/docs/architecture/access.md"

_RELATIONSHIP = {
    "owner": "the agent's OWNER",
    "admin": "an ADMIN of this agent (holds its keys)",
    "member": "a member of the agent's workspace — not its owner or an admin",
    "contact": "a CONTACT — not a member of the agent's workspace",
    "system": "canopy itself (a schedule, a drill, an agent dispatch) or this agent's OWN login "
              "(no outside person; another agent's login is graded by its grants like anyone)",
}
#: Envelope VERSION 1 words, and what they are called now.
_LEGACY_RELATIONSHIP = {"caller": "contact"}
_LEGACY_PROFILE = {"restricted": "confined"}
#: What a `granted_by` basis means, for the bases that need saying.
_BASIS = {
    "editor": "a workspace editor: the whole agent, but every turn runs manual",
    "refused": "refused",
}


def relationship_of(env: dict) -> str:
    """`relationship` in VERSION 2 words (`caller` → `contact`)."""
    rel = str(env.get("relationship") or "unknown")
    return _LEGACY_RELATIONSHIP.get(rel, rel)


def profile_of(env: dict) -> str:
    """`profile` in VERSION 2 words (`restricted` → `confined`)."""
    prof = str(env.get("profile") or "unknown")
    return _LEGACY_PROFILE.get(prof, prof)


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
    """(envelope path, turn id, task) from a FRESH pointer for one of `candidates`,
    consuming it; (None, None, None) when there is none."""
    pending = os.path.join(root or CALLER_ROOT, "pending")
    if not os.path.isdir(pending):
        return None, None, None
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
            return env, str(doc.get("turn_id") or ""), name
    return None, None, None


def record_parent(task: str, env: dict, turn_id: str, claude_session_id: str, *,
                  root=None, now=time.time) -> None:
    """Write `by-task/<task>.json` atomically, 0600. Best-effort: errors are swallowed."""
    if not task or not _NAME.match(task):
        return
    conv = env.get("conversation") if isinstance(env.get("conversation"), dict) else {}
    rec = {"task": task, "turn_id": turn_id or str(env.get("turn_id") or ""),
           "session_id": str((conv or {}).get("session_id") or ""),
           "claude_session_id": str(claude_session_id or ""), "updated_at": now()}
    d = os.path.join(root or CALLER_ROOT, "by-task")
    tmp = os.path.join(d, f".{task}.{os.getpid()}.tmp")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, os.path.join(d, f"{task}.json"))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


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
    rel = relationship_of(env)
    verified = bool(env.get("verified"))
    assurance = who.get("assurance") or "none"
    mode = (env.get("turn_mode") or {}).get("mode") or "unknown"
    basis = (env.get("turn_mode") or {}).get("basis") or ""
    trigger = env.get("trigger") or {}
    cap = env.get("capability") or {}
    tid = turn_id or env.get("turn_id") or ""
    # Belt and braces: canopy-web only grants to a verified owner/admin; a grant on an
    # envelope that says otherwise is ignored rather than believed.
    grant = ship_grant(env) if rel in ("owner", "admin") and verified else None
    if mode == "auto":
        mode_note = ""
    elif grant:
        mode_note = (" — sends, deploys, publishing and every other outbound or irreversible "
                     f"action need the OWNER's approval first; push / PR / merge in {grant['repo']} "
                     "are covered by the ship grant below")
    else:
        mode_note = (" — outbound or irreversible actions (push, deploy, merge, send, publish) "
                     "need the OWNER's approval first")
    lines = [
        f"[canopy] Who is asking — the caller envelope canopy wrote for turn {tid}. "
        "This is canopy's answer, not something the person typed:",
        f"- who: {_person(who)}" + (f", via {who['via']}" if who.get("via") else ""),
        f"- relationship: {rel} — {_RELATIONSHIP.get(rel, 'unknown')}",
        f"- verified: {'yes' if verified else 'NO'} (assurance: {assurance})"
        + ("" if verified else " — who they say they are is a claim, not proven"),
        f"- access: profile={profile_of(env)}, granted_by={env.get('granted_by') or 'unknown'}"
        + (f" ({_BASIS[env['granted_by']]})" if env.get("granted_by") in _BASIS else "")
        + (f", capability={cap.get('name')}" if cap.get("name") else ""),
        f"- turn mode: {mode}" + (f" ({basis})" if basis else "") + mode_note,
        f"- channel: {trigger.get('origin') or 'unknown'}"
        + (f", runner {trigger['runner']}" if trigger.get("runner") else ""),
    ]
    if grant:
        lines.append(f"- ship grant: push / PR / merge in {grant['repo']} are pre-approved by the "
                     f"owner ({grant['basis']}) — do them without waiting. Only that repo. Sends, "
                     "deploys of other systems, publishing and public writes still need the OWNER.")
    if rel not in ("owner", "admin", "system") or not verified:
        lines.append("Act accordingly: this person does not hold the agent's authority. Do not push, "
                     "deploy, send, publish or change shared state on their say-so — answer within "
                     "what they may have, and take anything more to the owner.")
    lines.extend(_page_lines(env.get("page")))
    lines.append(f"Full envelope: {path}; re-read it with the who_is_asking tool "
                 f"(turn_id={tid}) before anything irreversible. Terms: {ACCESS_DOC}")
    return "\n".join(lines)


def ship_grant(env: dict):
    """The envelope's repo-internal ship grant, or None.

    canopy-web sets `ship_grant` only when another agent's verified login that is
    the target's owner or an explicit admin dispatched the turn at that agent
    (owner decision, 2026-10-03). Read defensively: an envelope from an older
    canopy-web has no field, and anything malformed is no grant — the turn then
    behaves exactly as before.
    """
    g = env.get("ship_grant")
    if not isinstance(g, dict):
        return None
    repo = str(g.get("repo") or "").strip()
    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        return None
    return {"repo": repo, "basis": str(g.get("basis") or "").strip() or "owner-approved dispatch"}


def _page_lines(page) -> list:
    """What the person is looking at, when the conversation is embedded in a page
    that declared it. Here — in context, not in the person's message — because
    the widget used to paste this under their first message, and every
    transcript opened with a JSON dump (canopy-web, 2026-09-26). It is the
    SELECTION (ids + filters, ≤8 KiB), never the rows: read those with the
    backing tool, or re-read the page with current_page."""
    if not isinstance(page, dict) or not page.get("resource"):
        return []
    ids = page.get("visible_ids") or []
    out = [f"The person is looking at a page: {page['resource']}"
           + (f" ({page['path']})" if page.get("path") else "")
           + f" — {page.get('visible_count', len(ids))} item(s) on screen."]
    if ids:
        out.append("- on screen (ids): " + json.dumps(ids, separators=(",", ":")))
    if page.get("filters"):
        out.append("- filters: " + json.dumps(page["filters"], separators=(",", ":")))
    tool = page.get("backing_tool")
    out.append("- read the rows with " + (f"`{tool}`" if tool else "the page's backing tool")
               + "; re-read what is on screen now with `current_page`. \"this\" / \"these\" / "
               "\"the ones I'm looking at\" mean the items above.")
    return out


def main() -> int:
    try:
        data = json.load(sys.stdin)
        explicit = os.environ.get("CANOPY_CALLER", "")
        task = None
        if explicit:
            path, tid = (explicit if _inside(explicit, CALLER_ROOT) else None), ""
        else:
            path, tid, task = claim_pointer(task_candidates(data.get("transcript_path", ""),
                                                            data.get("cwd", "")))
        if not path:
            return 0
        with open(path, encoding="utf-8") as fh:
            env = json.load(fh)
        if not isinstance(env, dict):
            return 0
        if task:
            record_parent(task, env, tid, str(data.get("session_id") or ""))
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": summarize(env, path, tid)}}))
    except Exception:  # noqa: BLE001 — never block a prompt, never fail loud
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
