"""Shared guarded email engine for the agent fleet — backs `canopy email`.

The generalization of echo's `bin/echo_email.py` + `bin/echo_mark_read.py` (adopted by
ACE as `bin/ace-email` / `bin/ace-mark-read`). Implements §3 of
docs/architecture/shared-gog-gdrive.md: the send wrapper, mark-read, and preflight are
ENGINE (fix-once-propagate, lives here); each agent supplies only its MOUNTS — mailbox +
gog client name in its repo's `config/agent.json` (`email`, `gog_client`).

Three subcommands:

- `send` — HTML multipart send via gog. Why HTML: Gmail display-wraps plain text at a
  fixed ~72 columns, which reads as ugly hard line breaks; an HTML body reflows to the
  reader's width. So we build flowing <p> paragraphs + <ul> bullets + linkified URLs,
  with a plain-text alternative, and send both. Body-file contract: single-line
  paragraphs separated by blank lines; bullet lines ("- ", "* ", "1. ") one per line —
  normalize() also collapses accidental hard-wrapped paragraphs.
  Emits a JSON result with `message_id` + `thread_id`; the SEND-SIDE CONTRACT is that
  every caller records `thread_id` into the agent's state layer (ACE: run comms-log;
  echo: contact-memory) so inbound triage can route the reply.
- `mark-read` — remove the UNREAD label via `gog gmail thread modify` (API reads don't
  clear the flag). Auth rides gog's own token bucket — never the macOS Keychain, which
  blocks forever on a GUI prompt in non-interactive shells (dimagi-internal/ace#827).
- `preflight` — gog auth liveness for the agent's client, with the exact `gog login …`
  remediation (and the API-not-enabled self-heal echo's preflight learned the hard way).

Two identities, and only ONE of them is per-agent. The gog *client* (`credentials-<client>.json`)
is the APP identity — client_id + client_secret, "which app asks Google for access" — and it is a
SHARED fleet app (`canopy`), reused by every agent's mailbox. The per-agent, never-shared identity
is the *mailbox* (`--account`): the session/thread identity bleed the fleet was built to avoid is
acting as another agent's MAILBOX, which is governed by --account, not the client.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import click

from orchestrator.agent_web import AgentWebError, resolve_identity
from orchestrator.repo_paths import resolve_repo_path

# The installed `canopy` CLI is a uv tool — it does NOT pick up merges to main until
# someone reruns `uv tool install --reinstall`. That gap once shipped a stale-engine
# email (2026-07-10: the no-forced-font fix was merged and in the plugin cache, but the
# lagging installed engine still sent Arial). Before any send, refuse when the RUNNING
# engine is OLDER than the marketplace clone — merged email fixes exist that this
# process doesn't have. A dev checkout running AHEAD of the clone is fine.
MARKETPLACE_CLONE = Path.home() / ".claude/plugins/marketplaces/canopy"


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts)


def engine_staleness_error(clone_dir: Path | None = None) -> str | None:
    """Refusal message when the running engine lags the marketplace clone, else None.

    None also when the check can't resolve (no clone, no dist metadata) or when
    CANOPY_EMAIL_SKIP_ENGINE_CHECK=1 — the guard is best-effort and must never
    brick sending on machines without a plugin install.
    """
    if os.environ.get("CANOPY_EMAIL_SKIP_ENGINE_CHECK") == "1":
        return None
    clone = clone_dir or MARKETPLACE_CLONE
    try:
        clone_text = (clone / "pyproject.toml").read_text()
    except OSError:
        return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', clone_text, re.M)
    if not m:
        return None
    clone_v = m.group(1)
    try:
        from importlib.metadata import version

        running_v = version("canopy")
    except Exception:
        return None
    if _version_tuple(running_v) >= _version_tuple(clone_v):
        return None
    return (
        f"installed canopy engine v{running_v} lags the marketplace clone v{clone_v} — "
        f"merged email fixes may not be in this process. "
        f"Fix: (cd {clone} && git pull) && uv tool install --reinstall {clone} "
        f"(bypass: CANOPY_EMAIL_SKIP_ENGINE_CHECK=1)"
    )


def _default_gog_config_dir() -> str:
    """Mirror gog's own resolution so canopy finds the dir gog writes to:
    $GOG_HOME override, else macOS ~/Library/Application Support/gogcli,
    else XDG ~/.config/gogcli. Hardcoding the macOS path broke headless Linux."""
    home = os.environ.get("GOG_HOME")
    if home:
        return os.path.expanduser(home)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/gogcli")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(xdg, "gogcli")


GOG_CONFIG_DIR = _default_gog_config_dir()
# Every Google surface a turn commonly touches — one login covers them all, so an
# agent doesn't re-consent per service. gmail is the only one THIS engine needs;
# `appscript` is included because some agents drive Google Drive via Apps Script and
# the scope must be granted at login (the doctor's check_auth_services verifies it).
LOGIN_SERVICES = "gmail,drive,docs,sheets,forms,appscript"

LIST_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+")
URL_RE = re.compile(r"(https?://[^\s<>()]+)")
# Sentence punctuation that hugs a URL belongs to the prose, not the link.
# "…/edit." linkified whole once sent two broken doc links (Google 404s the
# doc id + trailing dot) — echo → Fiorenzo, 2026-07-13.
TRAILING_PUNCT_RE = re.compile(r"[.,;:!?'\"]+$")
# Markdown-style inline link: [display text](https://url) — lets agents write a
# clean anchor label instead of pasting a raw URL into outbound mail.
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
# Inline emphasis. Agents write markdown bodies (every SKILL.md teaches markdown), and
# before this the engine converted ONLY links — so `**bold**` and `code` shipped to the
# recipient as literal asterisks and backticks. Code runs FIRST so asterisks inside a
# code span stay literal. Both are single-line by construction: a `*` or a backtick left
# unpaired in prose stays untouched rather than swallowing the rest of the paragraph.
MD_CODE_RE = re.compile(r"`([^`\n]+)`")
MD_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
# Spans the bold pass must not enter: a code span (backticks mean "literal", so
# `**x**` inside one stays literal) and a bare URL (an href survives verbatim — a
# stray asterisk pair in a query string would otherwise be rewritten into a tag).
MD_PROTECTED_RE = re.compile(r"`[^`\n]+`|https?://[^\s<>()]+")


class AgentEmailError(Exception):
    """Raised for identity/config problems or a failed send."""


@dataclass
class EmailIdentity:
    slug: str        # agent slug, e.g. "hal"
    account: str     # mailbox, e.g. hal@dimagi-ai.com
    client: str      # gog client name (credentials-<client>.json), usually == slug
    repo: Path | None = None  # agent repo root — lets preflight read config/secrets.yaml


def resolve_email_identity(repo_dir: Path) -> EmailIdentity:
    """Resolve the agent's email identity from its repo (plugin.json + config/agent.json).

    Mailbox comes from agent.json `email`; the gog client from agent.json `gog_client`,
    defaulting to the slug (the fleet convention: client name == agent slug).
    """
    try:
        ident = resolve_identity(Path(repo_dir))
    except AgentWebError as e:
        raise AgentEmailError(str(e)) from e
    account = (ident.get("email") or "").strip()
    if not account:
        raise AgentEmailError(
            f"no mailbox for agent {ident['slug']!r} — add \"email\" to "
            f"{Path(repo_dir) / 'config' / 'agent.json'}"
        )
    client = (ident.get("gog_client") or "").strip() or ident["slug"]
    return EmailIdentity(slug=ident["slug"], account=account, client=client,
                         repo=Path(repo_dir))


def find_agent_repo(slug: str) -> Path:
    """Locate an agent repo by slug across the machine's emdash root conventions."""
    path = resolve_repo_path(slug)
    if path is None:
        raise AgentEmailError(
            f"no local repo found for agent {slug!r} — pass --repo <dir> explicitly"
        )
    return path


# --------------------------------------------------------------------------------------
# Body shaping (ported verbatim from echo's proven wrapper)
# --------------------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Collapse hard-wrapped prose to one line per paragraph; keep bullets/blank lines."""
    out: list[str] = []
    para: list[str] = []

    def flush():
        if para:
            out.append(" ".join(s.strip() for s in para))
            para.clear()

    for ln in text.split("\n"):
        s = ln.rstrip()
        if not s.strip():
            flush()
            out.append("")
        elif LIST_RE.match(s):
            flush()
            out.append(s.strip())
        else:
            para.append(s)
    flush()
    collapsed: list[str] = []
    for line in out:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip() + "\n"


def _autolink(escaped: str) -> str:
    def repl(m: re.Match) -> str:
        url = m.group(1)
        tail = ""
        punct = TRAILING_PUNCT_RE.search(url)
        if punct:
            url, tail = url[: punct.start()], punct.group(0)
        return f'<a href="{url}">{url}</a>{tail}'

    return URL_RE.sub(repl, escaped)


def _inline_md(escaped: str) -> str:
    """`code` -> <code>, **bold** -> <strong>, on already-HTML-escaped text.

    One left-to-right pass. Code spans and bare URLs are consumed whole so the bold
    pass can never reach inside them; bold applies only to the prose between them. The
    URL text is emitted unchanged for `_autolink` to pick up afterwards."""
    def bold(seg: str) -> str:
        return MD_BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", seg)

    out: list[str] = []
    last = 0
    for m in MD_PROTECTED_RE.finditer(escaped):
        out.append(bold(escaped[last:m.start()]))
        tok = m.group(0)
        out.append(f"<code>{tok[1:-1]}</code>" if tok.startswith("`") else tok)
        last = m.end()
    out.append(bold(escaped[last:]))
    return "".join(out)


def _linkify(escaped: str) -> str:
    """Turn links clickable. Markdown `[text](url)` becomes an anchor with clean
    display text; bare URLs elsewhere are still auto-linked (shown as the URL).
    Runs on already-HTML-escaped text — the `[...](...)` literals survive escaping.
    Bare URLs inside a markdown link's target are not re-linked (we only autolink
    the segments between markdown matches).

    Inline emphasis is applied to the PROSE and to a link's display text, never to a
    URL — an href must survive verbatim, and a stray asterisk in a query string would
    otherwise be rewritten into a tag."""
    out: list[str] = []
    last = 0
    for m in MD_LINK_RE.finditer(escaped):
        out.append(_autolink(_inline_md(escaped[last:m.start()])))
        text, url = m.group(1), m.group(2)
        out.append(f'<a href="{url}">{_inline_md(text)}</a>')
        last = m.end()
    out.append(_autolink(_inline_md(escaped[last:])))
    return "".join(out)


def _list_kind(line: str) -> str | None:
    """'ol' for a numbered item (`1.`), 'ul' for a bullet (`- * +`), None if not a list line."""
    m = LIST_RE.match(line)
    if not m:
        return None
    return "ol" if m.group(1).rstrip().endswith(".") else "ul"


def to_html(plain: str) -> str:
    """Markdown-ish plain text -> minimal HTML. Numbered lines become <ol> (numbers preserved),
    bullets become <ul>; a run of same-kind items coalesces into ONE list even across blank lines
    (canopy #291 — numbered lists were losing their numbers and runs were fragmenting into many
    single-item lists)."""
    lines = plain.strip().split("\n")
    parts: list[str] = []
    para: list[str] = []

    def flush_para():
        if para:
            text = " ".join(l.strip() for l in para)
            parts.append(f"<p>{_linkify(html.escape(text, quote=False))}</p>")
            para.clear()

    i, n = 0, len(lines)
    while i < n:
        kind = _list_kind(lines[i])
        if kind:
            flush_para()
            items: list[str] = []
            while i < n:
                if _list_kind(lines[i]) == kind:
                    items.append(LIST_RE.sub("", lines[i]).strip())
                    i += 1
                elif not lines[i].strip():
                    # blank line: only stays in the list if a same-kind item follows
                    j = i
                    while j < n and not lines[j].strip():
                        j += 1
                    if j < n and _list_kind(lines[j]) == kind:
                        i = j
                    else:
                        break
                else:
                    break
            lis = "".join(
                f"<li>{_linkify(html.escape(it, quote=False))}</li>" for it in items
            )
            parts.append(f"<{kind}>{lis}</{kind}>")
        elif not lines[i].strip():
            flush_para()
            i += 1
        else:
            para.append(lines[i])
            i += 1
    flush_para()
    # No font-family / size / color override: let the mail client render in its own
    # default (e.g. Gmail's default sans) so the message reads as a native reply,
    # not a styled-looking blast (Jonathan's pet peeve, 2026-07-08).
    return "<html><body>" + "".join(parts) + "</body></html>"


# --------------------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------------------

def build_send_command(
    identity: EmailIdentity,
    *,
    to: str,
    subject: str,
    plain_path: str,
    html_body: str,
    cc: str | None = None,
    reply_to_message_id: str | None = None,
) -> list[str]:
    cmd = ["gog", "gmail", "send", "--account", identity.account, "--client", identity.client,
           "--to", to, "--subject", subject,
           "--body-file", plain_path, "--body-html", html_body, "--json"]
    if cc:
        cmd += ["--cc", cc]
    if reply_to_message_id:
        cmd += ["--reply-to-message-id", reply_to_message_id]
    return cmd


def parse_send_result(stdout: str) -> dict:
    """Normalize gog's --json send output to {message_id, thread_id, raw}.

    Liberal on key names (id/messageId vs message_id, threadId vs thread_id) so a gog
    version bump doesn't silently drop the thread_id the routing contract depends on.
    """
    try:
        raw = json.loads(stdout)
    except (ValueError, TypeError):
        return {"message_id": "", "thread_id": "", "raw": (stdout or "").strip()}
    obj = raw if isinstance(raw, dict) else {}
    message_id = obj.get("message_id") or obj.get("messageId") or obj.get("id") or ""
    thread_id = obj.get("thread_id") or obj.get("threadId") or ""
    return {"message_id": str(message_id), "thread_id": str(thread_id), "raw": raw}


SEND_TIMEOUT = 120  # seconds — a hung gog must not hang the whole turn


def send(
    identity: EmailIdentity,
    *,
    to: str,
    subject: str,
    body_text: str,
    cc: str | None = None,
    reply_to_message_id: str | None = None,
    dry_run: bool = False,
    runner=subprocess.run,
) -> dict:
    """Send an HTML multipart email as the agent. Returns the normalized JSON result.

    dry_run renders the plain + HTML bodies without invoking gog; its result carries the
    same message_id/thread_id keys (empty) as a real send so scripted callers never branch.
    """
    plain = normalize(body_text)
    html_body = to_html(plain)
    if dry_run:
        # cc must appear here even when empty — the dry-run is HOW an agent verifies
        # recipients before approval, and omitting it hides cc'd people (same failure
        # class as the raw text mail view dropping the Cc: line).
        return {"dry_run": True, "message_id": "", "thread_id": "",
                "account": identity.account, "client": identity.client,
                "to": to, "cc": cc or "", "subject": subject,
                "plain": plain, "html": html_body}
    # Pre-send review rail (review_receipt.py explains why it lives here and not in each
    # agent's PreToolUse hook). Keyed to THIS body: a review of an earlier revision does
    # not carry over. dry_run above is exempt on purpose — it is how agents iterate.
    from orchestrator import review_receipt
    review_receipt.require(identity.slug, body_text)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(plain)
        plain_path = tf.name
    try:
        cmd = build_send_command(
            identity, to=to, subject=subject, plain_path=plain_path,
            html_body=html_body, cc=cc, reply_to_message_id=reply_to_message_id,
        )
        try:
            r = runner(cmd, capture_output=True, text=True, timeout=SEND_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise AgentEmailError(
                f"gog gmail send timed out after {SEND_TIMEOUT}s as {identity.account} — "
                "check network; the message may NOT have been sent."
            )
        if r.returncode != 0:
            raise AgentEmailError(
                f"gog gmail send failed (exit {r.returncode}) as {identity.account}: "
                f"{(r.stderr or r.stdout or '').strip()[:400]}"
            )
        return parse_send_result(r.stdout)
    finally:
        os.unlink(plain_path)


# --------------------------------------------------------------------------------------
# reply-all derivation (ported from echo's bin/echo_email.py — guards a bug that happened)
# --------------------------------------------------------------------------------------

def _headers_of(msg: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def derive_reply_all(
    identity: EmailIdentity,
    *,
    thread_id: str | None = None,
    message_id: str | None = None,
    runner=subprocess.run,
) -> tuple[str, str, str]:
    """Return (to, cc, reply_to_message_id) for a reply-all.

    Two modes (exactly one of thread_id / message_id):
    - **thread_id (preferred)** — reads the thread and replies to its LATEST non-self
      message: To = that sender, Cc = everyone else on its To+Cc,
      reply_to_message_id = its id. `gog gmail read` is a THREAD reader and 404s on a
      bare message id — which is every multi-message thread's latest id. That bug bit
      echo live; thread mode is the shape that avoids it.
    - **message_id** — replies to that specific message when its id happens to be
      readable (single-message threads / thread-head ids). Kept for callers that only
      hold a message id; falls back to the latest message when the id isn't in the
      returned thread.

    Cc is de-duped and excludes the agent's own address and the sender. Uses `--json`
    because the default text view omits Cc — silently dropping cc'd people is the bug
    this guards against (it happened; operating-model §1b rule 3).
    """
    from email.utils import getaddresses

    if bool(thread_id) == bool(message_id):
        raise AgentEmailError("reply-all: pass exactly one of thread_id / message_id")
    read_id = thread_id or message_id
    r = runner(
        ["gog", "gmail", "read", read_id, "--account", identity.account,
         "--client", identity.client, "--json"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        hint = " (pass a THREAD id — gog reads threads, not bare message ids)" if message_id else ""
        raise AgentEmailError(
            f"reply-all: could not read {read_id}: "
            f"{(r.stderr or '').strip()[:200]}{hint}"
        )
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise AgentEmailError(f"reply-all: unparseable gog read output for {read_id}")
    msgs = data.get("thread", {}).get("messages", [])
    if not msgs:
        raise AgentEmailError(f"reply-all: no messages in {read_id}")
    self_lc = identity.account.lower()
    if thread_id:
        # the message being replied to = latest one not sent by the agent itself
        msg = next((m for m in reversed(msgs)
                    if self_lc not in _headers_of(m).get("from", "").lower()), msgs[-1])
    else:
        msg = next((m for m in msgs if m.get("id") == message_id), None) or msgs[-1]
    h = _headers_of(msg)
    sender = getaddresses([h.get("from", "")])
    sender_email = sender[0][1].lower() if sender else ""
    if not sender_email:
        raise AgentEmailError(f"reply-all: message in {read_id} has no From header")
    others = getaddresses([h.get("to", ""), h.get("cc", "")])
    cc, seen = [], {sender_email, self_lc}
    for _name, email in others:
        e = email.lower()
        if not e or e in seen:
            continue
        seen.add(e)
        cc.append(email)
    return sender_email, ", ".join(cc), msg.get("id") or (message_id or "")


def dropped_participants(
    identity: EmailIdentity,
    *,
    message_id: str | None = None,
    thread_id: str | None = None,
    to: str | None,
    cc: str | None,
    runner=subprocess.run,
) -> list[str]:
    """Thread participants a manual-recipient reply would DROP (best-effort).

    A reply sent with explicit --to into an existing thread silently narrows the
    audience — the exact failure that hit hal on 2026-07-10 (an answer on a
    4-person thread went to one person; the item's owner never saw it). This
    computes what reply-all WOULD target (latest non-self message's sender +
    To/Cc, via derive_reply_all) minus the agent itself and the chosen To/Cc.

    Best-effort by design: any read/parse failure returns [] rather than
    blocking a send the agent may legitimately need to make — the caller turns
    a NON-EMPTY result into a refusal, so only a confirmed drop blocks.
    """
    try:
        tid = thread_id
        if not tid and message_id:
            r = runner(
                ["gog", "gmail", "get", message_id, "--account", identity.account,
                 "--client", identity.client, "--json"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return []
            tid = json.loads(r.stdout).get("message", {}).get("threadId")
        if not tid:
            return []
        d_to, d_cc, _ = derive_reply_all(identity, thread_id=tid, runner=runner)
    except Exception:
        return []
    split = lambda s: {a.strip().lower() for a in (s or "").split(",") if a.strip()}
    participants = (split(d_to) | split(d_cc)) - {identity.account.lower()}
    return sorted(participants - split(to) - split(cc))


# --------------------------------------------------------------------------------------
# mark-read
# --------------------------------------------------------------------------------------

def mark_read(
    identity: EmailIdentity,
    thread_ids: list[str],
    *,
    runner=subprocess.run,
) -> list[dict]:
    """Remove the UNREAD label from each thread as the agent. Per-thread results, keeps going.

    Shells out to `gog gmail thread modify` — gog's own token bucket handles auth, same
    as every other gog call in a turn. The previous implementation minted an access
    token itself via the macOS Keychain `security` call, which blocks FOREVER on a GUI
    prompt in non-interactive agent shells (dimagi-internal/ace#827) — never reintroduce
    a Keychain read here.
    """
    results = []
    for th in thread_ids:
        try:
            r = runner(
                ["gog", "gmail", "thread", "modify", th, "--remove", "UNREAD",
                 "--account", identity.account, "--client", identity.client],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            results.append({"thread_id": th, "ok": False, "error": "timed out after 30s"})
            continue
        except FileNotFoundError:
            raise AgentEmailError("gog CLI not found on PATH (brew install steipete/tap/gogcli)")
        if r.returncode == 0:
            results.append({"thread_id": th, "ok": True, "error": ""})
        else:
            err = (r.stderr or r.stdout or "").strip().replace("\n", " ")
            results.append({"thread_id": th, "ok": False, "error": err[:200]})
    return results


# --------------------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------------------

def _oauth_remedy(identity: EmailIdentity, stderr: str) -> list[str] | None:
    """Targeted fix for *API-not-enabled* failures (NOT a bad token — re-login won't help).

    The self-heal for the "accessNotConfigured" dead-end that stalled an echo turn on a
    fresh machine: gog is authed fine, but a Google API isn't enabled in the agent's
    OAuth project. Returns fix lines including the enable URL Google embeds, else None.
    """
    s = stderr or ""
    if not re.search(r"accessNotConfigured|SERVICE_DISABLED|has not been used in project|"
                     r"API has not been used", s, re.I):
        return None
    api = "A required Google API"
    am = re.search(r"([A-Z][\w ]*? API) has not been used", s)
    if am:
        api = am.group(1)
    url_m = re.search(r"https://console\.(?:developers|cloud)\.google\.com/\S+", s)
    lines = [
        f"FIX: {api} is not enabled for {identity.slug}'s OAuth project.",
        "     gog IS authed — this is NOT a token problem, so re-login won't fix it.",
        (f"     Enable it: {url_m.group(0).rstrip('.),')}" if url_m
         else "     Enable the API in the Google Cloud console for the agent's OAuth project."),
        "     Then wait ~1 min for it to propagate and re-run this preflight.",
    ]
    return lines


def _provision_remedy(identity: EmailIdentity, creds: str) -> list[str] | None:
    """Route the missing-client fix through the DECLARATIVE (legacy) path when the agent's repo
    declares this gog client in `config/secrets.yaml` via `canopy provision`. `.env.tpl`-primary
    agents (the current standard — see agent-runtime.md) don't declare file-type secrets this way
    at all: a non-env credential FILE like this one is resolved directly with
    `op read "op://<vault>/<item>/<field>" > <file>`, so this helper falls through to the
    generic `preflight` fallback message for them, same as any agent with no declared secrets.

    The manual "copy the JSON into place" instruction is the fallback of last resort — an
    agent that declares its client for provisioning (hal is the reference) should never be
    told to hand-shuffle keys. This helper distinguishes the three real failure modes the
    old message collapsed into one:
      * declared, but the 1Password item doesn't resolve  -> vault it, THEN provision
      * declared and resolvable, just not materialized here -> run `canopy provision`
      * not declared at all                                -> None (caller's manual fallback)
    Returns remediation lines, or None to fall back.
    """
    repo = getattr(identity, "repo", None)
    if repo is None:
        return None
    try:
        from orchestrator import provision as _provision
    except ImportError:
        return None
    try:
        secrets = _provision.load_manifest(Path(repo))
    except Exception:
        return None
    want = os.path.basename(creds)  # credentials-<client>.json
    match = next(
        (s for s in secrets
         if os.path.basename(_provision.resolve_target(s.target, Path(repo))) == want),
        None,
    )
    if match is None:
        return None
    prov_cmd = f"canopy provision --repo {repo}"
    try:
        _provision._op_read(match.op_ref)
    except Exception as e:
        # The item isn't in 1Password (missing or misnamed) — the true blocker.
        return [
            f"FIX: the `{identity.client}` gog OAuth client isn't in 1Password yet: {match.op_ref}",
            f"     This is the SHARED fleet app (client_id + client_secret), minted ONCE for all "
            "agents — not a per-agent client.",
            f"     Vault the client JSON at that ref, then materialize it: {prov_cmd}",
            f"     ({str(e).splitlines()[0][:140] if str(e) else 'op read failed'})",
        ]
    # The item resolves — it just hasn't been written to this machine.
    login_cmd = (f"gog login {identity.account} --client {identity.client} "
                 f"--services {LOGIN_SERVICES}")
    return [
        f"FIX: the `{identity.client}` gog client is in 1Password but not on this machine.",
        f"     Materialize it: {prov_cmd}",
        f"     Then consent {identity.slug}'s mailbox into it once (interactive): {login_cmd}",
    ]


def granted_services(
    identity: EmailIdentity,
    *,
    runner=subprocess.run,
) -> set[str] | None:
    """Google services actually GRANTED for this identity's (account, client).

    Reads `gog auth list --json` — gog's own record of what each stored account was
    authorized for — and returns the service-name set (e.g. {"gmail","drive","appscript"}).
    Returns None if gog is unavailable or the account isn't found, so the caller can
    decide whether "can't tell" is a hard failure (the doctor treats it as skip, not fail)."""
    try:
        r = runner(["gog", "auth", "list", "--json"],
                   capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        accounts = json.loads(r.stdout or "{}").get("accounts", [])
    except (ValueError, TypeError):
        return None
    for a in accounts:
        if (str(a.get("email", "")).lower() == identity.account.lower()
                and a.get("client") == identity.client):
            return set(a.get("services", []))
    return None


READ_TIMEOUT = 60  # seconds — a hung gog read/fetch must not hang the whole turn


def _attachments_of(msg: dict) -> list[dict]:
    """Walk a message payload's parts (recursively) for real attachments — a part with a
    filename AND a body.attachmentId. Returns [{attachment_id, filename, mime_type, size}].
    Surfacing the attachment_id here is the point: it's what `fetch_attachment` needs, and
    hand-walking the raw payload parts to find it is exactly what agents kept fumbling."""
    found: list[dict] = []

    def walk(part: dict) -> None:
        body = part.get("body", {}) or {}
        filename = part.get("filename") or ""
        if filename and body.get("attachmentId"):
            found.append({
                "attachment_id": body["attachmentId"],
                "filename": filename,
                "mime_type": part.get("mimeType", ""),
                "size": body.get("size"),
            })
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(msg.get("payload", {}) or {})
    return found


def _decode_plain(payload: dict) -> str:
    """Recursively concatenate the base64url-decoded text/plain parts of a message payload.
    gog's --json hands back base64 MIME parts (not decoded text) — this is the decode every
    read wrapper reimplemented (echo's bin/echo_read.py._plain)."""
    import base64
    out = ""
    body = payload.get("body", {}) or {}
    if payload.get("mimeType", "").startswith("text/plain") and body.get("data"):
        out += base64.urlsafe_b64decode(body["data"] + "===").decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        out += _decode_plain(part)
    return out


def _trim_quoted_tail(text: str) -> str:
    """Drop the quoted reply chain — stop at the first `>`, `On … wrote:`, or `-----Original`
    line (echo_read.py._trim_quotes). Keeps the message's own prose, sheds the history."""
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(">") or (s.startswith("On ") and s.endswith("wrote:")) \
                or s.startswith("-----Original"):
            break
        lines.append(ln)
    return "\n".join(lines).strip()


def _reply_all_of(raw_messages: list[dict], self_account: str) -> dict:
    """Compute reply-all recipients for a thread: To = the latest non-self sender, Cc =
    everyone else on that message's To+Cc minus self and the To, deduped; reply_to_message_id
    = that message's id. Same algorithm as derive_reply_all's thread mode, computed from the
    already-parsed messages so `read` needs only ONE gog read."""
    from email.utils import getaddresses
    if not raw_messages:
        return {"to": "", "cc": "", "reply_to_message_id": ""}
    me = self_account.lower()
    latest = next((m for m in reversed(raw_messages)
                   if me not in _headers_of(m).get("from", "").lower()), raw_messages[-1])
    h = _headers_of(latest)
    sender = [e for _, e in getaddresses([h.get("from", "")]) if e]
    to = sender[0] if sender else ""
    cc, seen = [], {me, to.lower()}
    for _, e in getaddresses([h.get("to", ""), h.get("cc", "")]):
        el = e.lower()
        if e and el not in seen:
            seen.add(el)
            cc.append(e)
    return {"to": to, "cc": ", ".join(cc), "reply_to_message_id": latest.get("id", "")}


def read_thread(
    identity: EmailIdentity,
    thread_id: str,
    *,
    runner=subprocess.run,
) -> dict:
    """Read a Gmail THREAD as the agent → a normalized dict the fleet can rely on:
      {thread_id,
       messages: [{message_id, from, to, cc, subject, date, snippet, body_text,
                   attachments: [{attachment_id, filename, mime_type, size}]}],
       reply_all: {to, cc, reply_to_message_id}}

    `body_text` is the decoded plain-text (base64 MIME parts decoded, quoted reply tail
    trimmed); `reply_all` is the ready recipient set (latest non-self sender = To, the rest
    = Cc, minus self). Together those are the whole reason agents hand-rolled read wrappers
    (echo's bin/echo_read.py) — folded into the engine so they don't. `gog gmail read` is a
    THREAD reader and 404s on a bare message id (the bug that bit echo live — see
    derive_reply_all). Uses --json because the default text view omits Cc and attachment ids.
    This is the fleet's sanctioned inbound READ path."""
    r = runner(
        ["gog", "gmail", "read", thread_id, "--account", identity.account,
         "--client", identity.client, "--json"],
        capture_output=True, text=True, timeout=READ_TIMEOUT,
    )
    if r.returncode != 0:
        raise AgentEmailError(
            f"read: could not read thread {thread_id} as {identity.account}: "
            f"{(r.stderr or '').strip()[:200]} "
            "(pass a THREAD id — gog reads threads, not bare message ids)"
        )
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise AgentEmailError(f"read: unparseable gog read output for {thread_id}")
    raw = data.get("thread", {}).get("messages", [])
    messages = []
    for m in raw:
        h = _headers_of(m)
        messages.append({
            "message_id": m.get("id"),
            "from": h.get("from", ""),
            "to": h.get("to", ""),
            "cc": h.get("cc", ""),
            "subject": h.get("subject", ""),
            "date": h.get("date", ""),
            "snippet": m.get("snippet", ""),
            "body_text": _trim_quoted_tail(_decode_plain(m.get("payload", {}) or {})),
            "attachments": _attachments_of(m),
        })
    return {
        "thread_id": thread_id,
        "messages": messages,
        "reply_all": _reply_all_of(raw, identity.account),
    }


def fetch_attachment(
    identity: EmailIdentity,
    message_id: str,
    attachment_id: str,
    *,
    out_dir: str | None = None,
    runner=subprocess.run,
) -> dict:
    """Download ONE attachment as the agent → {message_id, attachment_id, path, bytes,
    cached, saved_to}.

    Two gotchas this wraps so no agent re-fumbles them (both seen live in an ACE turn):
      - `gog gmail attachment` has NO `-o`/`--out` flag. It writes the bytes into gog's
        cache dir and emits JSON `{bytes, cached, path}` — read `.path`; there is no
        base64 `data` field to decode.
      - The subcommand is `attachment` (singular); the attachment_id comes from walking
        the thread payload (use `read_thread`, whose `attachments[]` hands it to you).
    With out_dir, the cached file is copied there and `saved_to` is that path. To parse an
    xlsx/docx, use Python stdlib (zipfile + xml) — the runtime env is externally-managed,
    so a `pip install openpyxl` fails; don't reach for it."""
    r = runner(
        ["gog", "gmail", "attachment", message_id, attachment_id,
         "--account", identity.account, "--client", identity.client, "--json"],
        capture_output=True, text=True, timeout=READ_TIMEOUT,
    )
    if r.returncode != 0:
        raise AgentEmailError(
            f"fetch-attachment: gog failed for {message_id}/{attachment_id} "
            f"as {identity.account}: {(r.stderr or '').strip()[:200]}"
        )
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise AgentEmailError("fetch-attachment: unparseable gog attachment output")
    path = data.get("path")
    if not path:
        raise AgentEmailError(
            f"fetch-attachment: gog returned no path (keys: {sorted(data)})"
        )
    saved_to = None
    if out_dir:
        import shutil
        os.makedirs(out_dir, exist_ok=True)
        saved_to = os.path.join(out_dir, os.path.basename(path))
        shutil.copyfile(path, saved_to)
    return {
        "message_id": message_id,
        "attachment_id": attachment_id,
        "path": path,
        "bytes": data.get("bytes"),
        "cached": data.get("cached"),
        "saved_to": saved_to,
    }


def preflight(
    identity: EmailIdentity,
    *,
    gog_dir: str | None = None,
    runner=subprocess.run,
) -> tuple[bool, list[str]]:
    """Is the agent's gog Gmail auth alive? (ok, report-lines) — read-only, never logs in."""
    gog_home = gog_dir or GOG_CONFIG_DIR
    login_cmd = (f"gog login {identity.account} --client {identity.client} "
                 f"--services {LOGIN_SERVICES}")
    creds = os.path.join(gog_home, f"credentials-{identity.client}.json")
    if not os.path.exists(creds):
        remedy = _provision_remedy(identity, creds)
        if remedy:
            return False, remedy
        return False, [
            f"FIX: gog `{identity.client}` client credentials missing: {creds}",
            f"     This is the SHARED fleet OAuth client (client_id + client_secret), not per-agent.",
            f"     Standard fix: `op read \"op://<vault>/<item>/<field>\" > {creds}` (a non-env "
            "credential FILE — see agent-runtime.md).",
            f"     Legacy: declare it in config/secrets.yaml so `canopy provision` places it.",
            f"     Then: {login_cmd}",
        ]
    cfg_path = os.path.join(gog_home, "config.json")
    mapped = False
    if os.path.exists(cfg_path):
        try:
            mapped = (json.load(open(cfg_path)).get("account_clients", {})
                      .get(identity.account) == identity.client)
        except (ValueError, OSError):
            mapped = False
    if not mapped:
        return False, [
            f"FIX: {cfg_path} does not map {identity.account} -> {identity.client}.",
            f"     Add it under account_clients (or re-run: {login_cmd})",
        ]
    # Live token check: a read-only search confirms the refresh token is good.
    try:
        r = runner(
            ["gog", "gmail", "search", "--account", identity.account,
             "--client", identity.client, "in:inbox", "--max", "1"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return False, ["FIX: gog CLI not installed (brew install gog / see GOG docs)."]
    except subprocess.TimeoutExpired:
        return False, ["FIX: gog gmail search timed out — check network / re-login."]
    if r.returncode != 0:
        remedy = _oauth_remedy(identity, r.stderr)
        if remedy:
            return False, remedy
        first_err = (r.stderr or "").strip().splitlines()[:1]
        return False, [
            f"FIX: gog `{identity.client}` creds present but not logged in / token bad.",
            f"     Run: {login_cmd}",
            f"     ({first_err[0] if first_err else 'no token'})",
        ]
    return True, [f"OK: gog Gmail ready (account {identity.account}, client {identity.client})."]


# --------------------------------------------------------------------------------------
# CLI — `canopy email …`
# --------------------------------------------------------------------------------------

def _identity_from_opts(repo: str | None, agent: str | None,
                        account: str | None, client: str | None) -> EmailIdentity:
    if account:  # fully explicit identity — no repo needed, but warn on identity bleed
        explicit = EmailIdentity(slug=agent or account.split("@")[0],
                                 account=account, client=client or agent or account.split("@")[0])
        try:
            repo_dir = Path(repo) if repo else (find_agent_repo(agent) if agent else Path.cwd())
            explicit.repo = repo_dir
            resolved = resolve_email_identity(repo_dir)
        except AgentEmailError:
            resolved = None
        if resolved and resolved.account.lower() != explicit.account.lower():
            sys.stderr.write(
                f"WARNING: sending as {explicit.account} from {resolved.slug!r}'s repo "
                f"(its identity is {resolved.account}). One mailbox per agent, never shared, "
                "is the fleet's one hard rule — make sure this cross-identity send is "
                "deliberate.\n"
            )
        return explicit
    repo_dir = Path(repo) if repo else (find_agent_repo(agent) if agent else Path.cwd())
    ident = resolve_email_identity(repo_dir)
    if client:
        ident.client = client
    return ident


_identity_options = [
    click.option("--repo", type=click.Path(exists=True, file_okay=False),
                 help="Agent repo root (default: cwd). Identity from its config/agent.json."),
    click.option("--agent", help="Agent slug — locate its local repo instead of --repo."),
    click.option("--account", help="Explicit mailbox override (skips repo resolution)."),
    click.option("--client", help="Explicit gog client override."),
]


def _with_identity_options(fn):
    for opt in reversed(_identity_options):
        fn = opt(fn)
    return fn


@click.group("email")
def email_group():
    """Guarded agent email — shared engine, per-agent identity (shared-gog-gdrive.md §3)."""


@email_group.command("send")
@_with_identity_options
@click.option("--to", help="Comma-separated recipients (required unless --reply-all).")
@click.option("--cc")
@click.option("--subject", required=True)
@click.option("--body-file", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Plain-text body: single-line paragraphs, blank-line separated; '- ' bullets.")
@click.option("--reply-to-message-id", help="Thread the send as a reply to this message id.")
@click.option("--thread-id",
              help="Thread to reply into (preferred for --reply-all): recipients + the "
                   "threading message-id derive from the thread's LATEST non-self message. "
                   "gog reads THREADS — a bare message id 404s on multi-message threads.")
@click.option("--reply-all", is_flag=True,
              help="Derive To (original sender) + Cc (everyone else on To+Cc) from JSON "
                   "headers — raw reads hide Cc and drop cc'd people. Pass --thread-id "
                   "(preferred) or --reply-to-message-id; explicit --cc is merged in.")
@click.option("--narrow", is_flag=True,
              help="Deliberately reply to FEWER people than are on the thread. Without "
                   "this flag, a reply (--reply-to-message-id) whose To/Cc drops known "
                   "thread participants is refused — reply-all is the default on "
                   "existing threads.")
@click.option("--dry-run", is_flag=True, help="Render plain + HTML bodies without sending.")
def email_send(repo, agent, account, client, to, cc, subject, body_file,
               reply_to_message_id, thread_id, reply_all, narrow, dry_run):
    """Send an HTML multipart email as the agent (the fleet's ONLY send path).

    Emits JSON with message_id + thread_id — record thread_id into the agent's state
    layer (comms-log / contact-memory) so inbound triage can route the reply.
    """
    stale = engine_staleness_error()
    if stale:
        raise click.ClickException(f"REFUSING to send — {stale}")
    if reply_all and not (thread_id or reply_to_message_id):
        raise click.ClickException("--reply-all requires --thread-id (preferred) or --reply-to-message-id")
    if thread_id and not reply_all:
        raise click.ClickException("--thread-id is only meaningful with --reply-all")
    if not reply_all and not to:
        raise click.ClickException("--to is required (or pass --reply-all)")
    try:
        ident = _identity_from_opts(repo, agent, account, client)
        if reply_to_message_id and not reply_all and not narrow:
            dropped = dropped_participants(
                ident, message_id=reply_to_message_id, to=to, cc=cc,
            )
            if dropped:
                raise click.ClickException(
                    "REFUSING narrow reply — this thread has participants missing from "
                    f"To/Cc: {', '.join(dropped)}. Reply-all is the default on existing "
                    "threads (--reply-all --thread-id <id>); pass --narrow to "
                    "deliberately drop them."
                )
        if reply_all:
            derived_to, derived_cc, derived_msg_id = derive_reply_all(
                ident, thread_id=thread_id,
                message_id=None if thread_id else reply_to_message_id,
            )
            to = derived_to
            cc = ", ".join(x for x in (derived_cc, cc) if x) or None
            reply_to_message_id = reply_to_message_id or derived_msg_id
            if thread_id:
                reply_to_message_id = derived_msg_id
        result = send(
            ident, to=to, subject=subject, body_text=Path(body_file).read_text(),
            cc=cc, reply_to_message_id=reply_to_message_id, dry_run=dry_run,
        )
    except AgentEmailError as e:
        raise click.ClickException(str(e))
    click.echo(json.dumps(result, indent=2))


@email_group.command("review-receipt")
@_with_identity_options
@click.option("--body-file", type=click.File("r"), required=True,
              help="The EXACT body file you are about to send.")
@click.option("--caught", multiple=True,
              help="What the review actually found and you fixed. Repeatable. Omit only "
                   "if the review genuinely found nothing AFTER you read the draft back.")
@click.option("--verdict", default="clean",
              type=click.Choice(["clean", "fixed"]),
              help="clean = nothing found; fixed = found something and corrected it.")
@click.option("--commitment", "commitments", multiple=True,
              help='Rule ONE commitment-class phrase: "<substring>=grounded:<mechanism>" '
                   'or "<substring>=cut". Repeatable. The receipt is REFUSED while any '
                   "such phrase in the body is unruled — run it once to see the list.")
def email_review_receipt(repo, agent, account, client, body_file, caught, verdict,
                         commitments):
    """Record that agent-turn-review ran against THIS body, unblocking the send.

    The receipt is keyed to a fingerprint of the body as it will be rendered, so revising
    the draft invalidates it — a review of an earlier revision never carries over. That is
    the point: it makes "reviewed v1, sent v3" impossible rather than merely discouraged.
    """
    from orchestrator import review_receipt
    try:
        ident = _identity_from_opts(repo, agent, account, client)
    except AgentEmailError as e:
        raise click.ClickException(str(e))
    body = body_file.read()
    try:
        path = review_receipt.record(ident.slug, body, caught=list(caught), verdict=verdict,
                                     commitments=list(commitments))
    except AgentEmailError as e:
        # Unruled commitment-class phrases: the refusal enumerates them and how to rule.
        raise click.ClickException(str(e))
    click.echo(json.dumps({
        "recorded": True,
        "slug": ident.slug,
        "fingerprint": review_receipt.fingerprint(body),
        "verdict": verdict,
        "caught": list(caught),
        "commitments": list(commitments),
        "receipt": str(path),
    }, indent=2))


@email_group.command("mark-read")
@_with_identity_options
@click.argument("thread_ids", nargs=-1, required=True)
def email_mark_read(repo, agent, account, client, thread_ids):
    """Remove the UNREAD label from THREAD_IDS (gog thread modify; API reads don't clear it)."""
    try:
        ident = _identity_from_opts(repo, agent, account, client)
        results = mark_read(ident, list(thread_ids))
    except AgentEmailError as e:
        raise click.ClickException(str(e))
    failed = 0
    for res in results:
        if res["ok"]:
            click.echo(f"{res['thread_id']} -> read")
        else:
            failed += 1
            click.echo(f"{res['thread_id']} -> ERROR {res['error']}")
    if failed:
        sys.exit(1)


@email_group.command("read")
@_with_identity_options
@click.argument("thread_id")
def email_read(repo, agent, account, client, thread_id):
    """Read a Gmail THREAD as the agent → normalized JSON: per message the headers, the
    decoded plain-text `body_text` (quoted tail trimmed), and each attachment's id; plus a
    thread-level `reply_all` recipient set (To/Cc/reply_to_message_id). Pass a THREAD id
    (gog 404s on a bare message id). Attachment ids feed `fetch-attachment`. The fleet's
    sanctioned inbound read path — replaces per-agent gog read + base64-decode wrappers."""
    try:
        ident = _identity_from_opts(repo, agent, account, client)
        result = read_thread(ident, thread_id)
    except AgentEmailError as e:
        raise click.ClickException(str(e))
    click.echo(json.dumps(result, indent=2))


@email_group.command("fetch-attachment")
@_with_identity_options
@click.argument("message_id")
@click.argument("attachment_id")
@click.option("--out", "out_dir", type=click.Path(file_okay=False),
              help="Copy the downloaded file into this dir (else it stays in gog's cache).")
def email_fetch_attachment(repo, agent, account, client, message_id, attachment_id, out_dir):
    """Download ONE attachment as the agent → JSON {path, bytes, saved_to}. There is NO -o
    flag; the file lands in gog's cache and this returns its .path (no base64 `data`). Get
    the attachment_id from `email read`'s attachments[]. Parse xlsx/docx with Python stdlib
    (zipfile+xml) — the env is externally-managed, so don't pip install."""
    try:
        ident = _identity_from_opts(repo, agent, account, client)
        result = fetch_attachment(ident, message_id, attachment_id, out_dir=out_dir)
    except AgentEmailError as e:
        raise click.ClickException(str(e))
    click.echo(json.dumps(result, indent=2))


@email_group.command("apply-filters")
@click.option("--all", "all_agents", is_flag=True, help="Apply to EVERY discovered agent mailbox.")
@click.option("--repo", type=click.Path(exists=True, file_okay=False), help="Agent repo (resolve its mailbox).")
@click.option("--agent", help="Agent slug (locate its repo).")
@click.option("--account", help="Explicit mailbox.")
@click.option("--client", help="Explicit gog client.")
@click.option("--sweep", is_flag=True,
              help="Also retroactively archive+mark-read matching mail ALREADY in the inbox "
                   "(clears the junk backlog so it doesn't spawn turns).")
@click.option("--dry-run", is_flag=True)
def email_apply_filters(all_agents, repo, agent, account, client, sweep, dry_run):
    """Push the fleet inbox filters (orchestrator/inbox_filters.py — the single source of
    truth) to one mailbox or, with --all, every agent's. Idempotent."""
    from orchestrator import inbox_filters
    targets = []  # (label, account, client)
    if all_agents:
        import json as _json
        from orchestrator.fleet_align import discover_agents
        for a in discover_agents():
            aj = a.path / "config" / "agent.json"
            if not aj.is_file():
                continue
            d = _json.loads(aj.read_text())
            acct = d.get("email") or d.get("mailbox")
            if acct:
                targets.append((a.slug, acct, d.get("gog_client") or "canopy"))
    else:
        try:
            ident = _identity_from_opts(repo, agent, account, client)
        except AgentEmailError as e:
            raise click.ClickException(str(e))
        targets.append((ident.slug, ident.account, ident.client))
    if not targets:
        raise click.ClickException("no target mailboxes (use --all, or --agent/--repo/--account+--client)")
    for label, acct, cli in targets:
        try:
            res = inbox_filters.apply_filters(acct, cli, dry_run=dry_run)
            line = f"{label} ({acct}): applied={res['applied']} skipped={res['skipped']}"
            if sweep:
                line += f" | swept-existing={inbox_filters.sweep_existing(acct, cli, dry_run=dry_run)}"
        except inbox_filters.FilterError as e:
            line = f"{label} ({acct}): ERROR {e}"
        click.echo(line)


@email_group.command("preflight")
@_with_identity_options
def email_preflight(repo, agent, account, client):
    """Check the agent's gog Gmail auth is alive; print the exact remediation if not."""
    try:
        ident = _identity_from_opts(repo, agent, account, client)
    except AgentEmailError as e:
        raise click.ClickException(str(e))
    ok, lines = preflight(ident)
    for line in lines:
        click.echo(line)
    stale = engine_staleness_error()
    if stale:
        ok = False
        click.echo(f"FIX: {stale}")
    if not ok:
        sys.exit(1)
