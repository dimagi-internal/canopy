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
  with a plain-text alternative, and send both. Body-file contract: paragraphs separated
  by blank lines; bullet lines ("- ", "* ", "1. ") one per line. Line breaks WITHIN a
  paragraph are preserved as <br> — write a timeline or heading block as separate lines
  and it arrives that way; normalize() rejoins only lines that look hard-wrapped (a long
  line continued by a lowercase one).
  Emits a JSON result with `message_id` + `thread_id`; the SEND-SIDE CONTRACT is that
  every caller records `thread_id` into the agent's state layer (ACE: run comms-log;
  echo: contact-memory) so inbound triage can route the reply.
- `mark-read` — remove the UNREAD label via `gog gmail thread modify` (API reads don't
  clear the flag). Auth rides gog's own token bucket — never the macOS Keychain, which
  blocks forever on a GUI prompt in non-interactive shells (dimagi-internal/ace#827).
- `archive` — the other half of turn housekeeping: drop INBOX + UNREAD so a handled or
  non-actionable thread leaves the agent's own inbox instead of lingering. Own mailbox
  only, reversible, no approval gate (agent-core/turn.md Step 2).
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
from collections.abc import Sequence
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


@dataclass
class ClientReconciliation:
    """What client this mailbox should actually use on THIS machine, and why.

    `declared` is what the repo pinned; `client` is what to use. `candidates` is None when
    gog could not be introspected at all (no evidence — never guess), [] when the mailbox
    holds no token under any client.
    """
    account: str
    declared: str
    client: str
    changed: bool = False
    candidates: list[str] | None = None
    ambiguous: list[str] | None = None
    note: str = ""


def token_pairs(*, runner=subprocess.run) -> set[tuple[str, str]] | None:
    """Every stored (client, mailbox) pair, from `gog auth tokens list --json`.

    This is the ONLY authoritative per-pair source gog exposes: its keys are literally
    `token:<client>:<mailbox>`, one per credential. `gog auth list` cannot answer this —
    it collapses to one row per ACCOUNT (verified 2026-08-17: a box holding both
    `token:canopy:echo@…` and `token:echo:echo@…` lists echo exactly once, as client
    `echo`), and `--client` does not scope the listing either.

    Returns None when the subcommand cannot be run or parsed, so callers can fall back to
    the account listing rather than concluding a mailbox has no tokens.
    """
    try:
        r = runner(["gog", "auth", "tokens", "list", "--json"],
                   capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        keys = json.loads(r.stdout or "{}").get("keys", [])
    except (ValueError, TypeError):
        return None
    pairs: set[tuple[str, str]] = set()
    for key in keys:
        # `token:<client>:<mailbox>` — split from the left, exactly twice: a mailbox never
        # contains a colon, so anything after the second one belongs to the mailbox.
        parts = str(key).split(":", 2)
        if len(parts) == 3 and parts[0] == "token" and parts[1] and parts[2]:
            pairs.add((parts[1], parts[2].lower()))
    return pairs


def clients_for_account(account: str, *, runner=subprocess.run) -> list[dict] | None:
    """Every stored (client, services) for a mailbox.

    WHICH clients hold the mailbox comes from `gog auth tokens list` (see `token_pairs`);
    WHAT each was granted comes from `gog auth list`. They are separate calls because gog
    answers only half the question in each: the token store knows every (client, mailbox)
    pair and no scopes, the account listing knows scopes and collapses to ONE row per
    account. Deriving the client set from the account listing alone — as this did until
    2026-08-17 — hides every credential but one, which made `reconcile_client` "self-heal"
    a correctly-pinned mailbox ONTO a stale client while the working token sat beside it
    (canopy#489: observed on ACE 2026-08-14, and reproduced live on echo, whose repo pins
    `canopy`, whose `token:canopy:echo@…` was valid, and which still resolved to `echo`).

    `services` is therefore `set | None`, and **None means UNKNOWN, not empty** — gog
    publishes no per-pair scope data, so a client visible only in the token store has no
    readable grant set. Callers must not treat that as "granted nothing"; doing so would
    manufacture false MISSING-scope findings, which is a worse failure than the silent skip
    this whole area exists to remove.

    Returns None when gog cannot be introspected at all and [] when it can and this mailbox
    simply has no token. Collapsing those two into one falsy value is precisely what let the
    doctor SKIP its services check in the one state it exists to catch (2026-08-10, echo on
    the cloud box: the account was authed, just under a different client, so the (account,
    client) lookup missed and the check reported "not introspectable").
    """
    want = (account or "").lower()
    pairs = token_pairs(runner=runner)

    rows: list[dict] | None
    try:
        r = runner(["gog", "auth", "list", "--json"],
                   capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        rows = None
    else:
        if r.returncode != 0:
            rows = None
        else:
            try:
                accounts = json.loads(r.stdout or "{}").get("accounts", [])
            except (ValueError, TypeError):
                rows = None
            else:
                rows = [
                    {"client": a.get("client") or "", "services": set(a.get("services") or [])}
                    for a in accounts
                    if str(a.get("email", "")).lower() == want
                ]

    if rows is None and pairs is None:
        return None          # neither surface answered — genuinely cannot look
    if pairs is None:
        return rows          # older gog without `auth tokens list`: degrade to the old view
    if rows is None:
        # The account listing failed but the token store answered. That is still a LOOK:
        # we know exactly which clients hold this mailbox, just not what they were granted.
        return [{"client": c, "services": None}
                for c in sorted(c for c, m in pairs if m == want)]

    seen = {e["client"] for e in rows}
    extra = sorted(c for c, m in pairs if m == want and c not in seen)
    return rows + [{"client": c, "services": None} for c in extra]


def reconcile_client(
    identity: EmailIdentity,
    *,
    runner=subprocess.run,
    apply: bool = False,
) -> ClientReconciliation:
    """Resolve the gog client this mailbox can actually authenticate with on this machine.

    `gog` keys credentials on the PAIR (mailbox, client), while `gog_client` is a single
    repo-global value and the token store is per-machine. So a fleet migration onto the
    shared `canopy` client — which has to be performed box by box — leaves any un-migrated
    box pinned to a pair that has no token, and every read and send dies with
    `No auth for gmail <mailbox>`. Echo hit this twice, in opposite directions: the pin was
    wrong for the laptop until 2026-08-06 and wrong for the cloud box from then on. Pinning
    the other value does not fix it; it only moves which box is broken.

    The agent operating model already says which half is the identity: the MAILBOX is the
    per-agent, never-shared thing, and which OAuth app brokers the token is plumbing. So the
    account is never touched here — borrowing a sibling's token would be identity bleed, the
    fleet's one hard rule — and only the client follows the evidence:

      * a token exists for the declared pair            -> unchanged (the fast path)
      * exactly one OTHER client holds this mailbox     -> use it, and say so
      * two or more do                                  -> unchanged; guessing is a coin flip
      * gog unreadable, or no token for this mailbox    -> unchanged; fail exactly as before

    `apply=True` writes the resolved client back onto `identity` — what the runtime path
    wants, so the correction lands before any gog command is built.
    """
    declared = identity.client
    found = clients_for_account(identity.account, runner=runner)
    out = ClientReconciliation(account=identity.account, declared=declared,
                               client=declared, candidates=None)
    if found is None:
        return out
    out.candidates = [c["client"] for c in found]
    if declared in out.candidates:
        return out
    others = [c for c in out.candidates if c]
    if len(others) == 1:
        out.client = others[0]
        out.changed = True
        out.note = (
            f"{identity.account} has no token under the configured `{declared}` gog client, "
            f"but is authed under `{others[0]}` — using that. Identity is the mailbox; the "
            f"client is plumbing. Re-login under `{declared}` (or update the repo's "
            f"gog_client) to make this box match its config."
        )
        if apply:
            identity.client = out.client
        return out
    if len(others) > 1:
        out.ambiguous = sorted(others)
        out.note = (
            f"{identity.account} is authed under {out.ambiguous} but not under the configured "
            f"`{declared}` — refusing to guess which one is intended."
        )
    return out


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

# A hard-wrapped line breaks near a fixed column, so it is LONG. A short line ended
# because its author meant it to.
WRAP_MIN_LEN = 64


def _is_wrap_continuation(prev: str, cur: str) -> bool:
    """True when `cur` is the wrapped remainder of `prev` rather than a line of its own.

    Hard-wrapped prose breaks near the wrap column and resumes mid-sentence, so a real
    continuation follows a LONG line and starts lowercase. Everything else — a short
    line, or one opening with a capital, a digit or a clock time — is deliberate
    structure (`BETH`, `09:00 MT · Gates Foundation`) and keeps its break.

    The asymmetry is the whole point: guessing wrong here costs one stray line break,
    while guessing wrong the other way silently flattens a structured block into a
    run-on paragraph, which is exactly what shipped for weeks.
    """
    return len(prev) >= WRAP_MIN_LEN and cur[:1].islower()


def normalize(text: str) -> str:
    """Keep the author's line breaks; re-join only genuinely hard-wrapped prose.

    Deliberate single-line structure — headings, timeline rows, address blocks — survives
    verbatim. Previously EVERY run of adjacent non-bullet lines was joined with a space,
    which flattened each agent's briefing timeline into one unreadable paragraph in BOTH
    the plain part and the HTML built from it. Only markdown bullets escaped, so the
    damage tracked whether an author happened to prefix rows with "- ".
    """
    out: list[str] = []

    def can_extend() -> bool:
        """The last emitted line is real text a continuation could attach to."""
        return bool(out) and out[-1] != ""

    mergeable = False
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            out.append("")
            mergeable = False
        elif LIST_RE.match(s):
            out.append(s)
            mergeable = True          # a wrapped bullet rejoins its own bullet
        else:
            if mergeable and can_extend() and _is_wrap_continuation(out[-1], s):
                out[-1] = f"{out[-1]} {s}"
            else:
                out.append(s)
            mergeable = True
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
            # <br> between the lines, not a space: normalize() has already rejoined the
            # genuinely hard-wrapped ones, so every break still standing is deliberate.
            rendered = "<br>".join(
                _linkify(html.escape(l.strip(), quote=False)) for l in para
            )
            parts.append(f"<p>{rendered}</p>")
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
    attachments: Sequence[str] = (),
) -> list[str]:
    cmd = ["gog", "gmail", "send", "--account", identity.account, "--client", identity.client,
           "--to", to, "--subject", subject,
           "--body-file", plain_path, "--body-html", html_body, "--json"]
    if cc:
        cmd += ["--cc", cc]
    if reply_to_message_id:
        cmd += ["--reply-to-message-id", reply_to_message_id]
    # gog has carried `--attach` (repeatable file path) all along; the engine simply never
    # passed it, so "forward that to <person>" silently degraded to a body-text summary and
    # whatever links happened to be in the original (canopy#462).
    for path in attachments:
        cmd += ["--attach", path]
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
    attachments: Sequence[str] = (),
    dry_run: bool = False,
    runner=subprocess.run,
) -> dict:
    """Send an HTML multipart email as the agent. Returns the normalized JSON result.

    dry_run renders the plain + HTML bodies without invoking gog; its result carries the
    same message_id/thread_id keys (empty) as a real send so scripted callers never branch.
    """
    plain = normalize(body_text)
    html_body = to_html(plain)
    attachments = [str(p) for p in attachments]
    # Fail before the review rail, not after gog: a missing file is the agent's mistake and
    # the error should name it, rather than surfacing as an opaque gog exit code.
    missing = [p for p in attachments if not os.path.isfile(p)]
    if missing:
        raise AgentEmailError(
            "cannot attach — file(s) not found: " + ", ".join(missing)
        )
    if dry_run:
        # cc must appear here even when empty — the dry-run is HOW an agent verifies
        # recipients before approval, and omitting it hides cc'd people (same failure
        # class as the raw text mail view dropping the Cc: line). `attachments` is here
        # for exactly the same reason: a forward that silently lost its PDFs is the
        # failure canopy#462 recorded, and the dry-run is where an agent must be able
        # to SEE what will actually ride along.
        return {"dry_run": True, "message_id": "", "thread_id": "",
                "account": identity.account, "client": identity.client,
                "to": to, "cc": cc or "", "subject": subject,
                "attachments": attachments,
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
            attachments=attachments,
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
# mark-read / archive — the turn's own-mailbox housekeeping pair
# --------------------------------------------------------------------------------------

def _remove_labels(
    identity: EmailIdentity,
    thread_ids: list[str],
    labels: str,
    *,
    runner=subprocess.run,
) -> list[dict]:
    """Remove LABELS from each thread as the agent. Per-thread results, keeps going.

    Shells out to `gog gmail thread modify` — gog's own token bucket handles auth, same
    as every other gog call in a turn. The previous implementation minted an access
    token itself via the macOS Keychain `security` call, which blocks FOREVER on a GUI
    prompt in non-interactive agent shells (dimagi-internal/ace#827) — never reintroduce
    a Keychain read here.

    Own-mailbox only: `identity.account` is always the AGENT's box, so this can never
    touch a sibling's mail. Label removal is reversible, which is why the turn procedure
    lets an agent do it as housekeeping without a human approval gate.
    """
    results = []
    for th in thread_ids:
        try:
            r = runner(
                ["gog", "gmail", "thread", "modify", th, "--remove", labels,
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


def mark_read(
    identity: EmailIdentity,
    thread_ids: list[str],
    *,
    runner=subprocess.run,
) -> list[dict]:
    """Remove the UNREAD label from each thread as the agent (API reads don't clear it)."""
    return _remove_labels(identity, thread_ids, "UNREAD", runner=runner)


def archive(
    identity: EmailIdentity,
    thread_ids: list[str],
    *,
    runner=subprocess.run,
) -> list[dict]:
    """Archive each thread out of the agent's OWN inbox — removes INBOX *and* UNREAD.

    Both labels in one call because that is exactly what the turn procedure asks for on a
    handled-or-not-actionable thread ("mark it read and archive it"): a thread left UNREAD
    in the archive still shows as unread mail, and re-surfaces on the next poll. Before
    this existed, `canopy email` offered only `mark-read`, so every agent hand-rolled the
    archive half as a raw `gog gmail thread modify … --remove INBOX` rediscovered from
    scratch each turn (eva, 2026-07-29: four tool calls spent finding the flag spelling).
    """
    return _remove_labels(identity, thread_ids, "INBOX,UNREAD", runner=runner)


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

    Reads `clients_for_account` — gog's own record of what each stored account was
    authorized for — and returns the service-name set (e.g. {"gmail","drive","appscript"}).
    Returns None if gog is unavailable, the account isn't found, **or gog holds a token for
    this exact pair but publishes no scopes for it**, so the caller can decide whether
    "can't tell" is a hard failure (the doctor treats it as skip, not fail).

    That last case is why None must keep meaning "can't tell" and never "granted nothing":
    a client known only from the token store carries `services: None`, and reading it as an
    empty grant set would report a fully-authorized mailbox as missing every scope.

    NOTE the (account, client) pair: a mailbox authed under a DIFFERENT client reads as None
    here, same as no gog at all. Callers that need to tell those apart — the doctor does,
    because "authed under the wrong client" is a finding and "cannot look" is not — should
    use `clients_for_account`, which distinguishes them."""
    found = clients_for_account(identity.account, runner=runner)
    if found is None:
        return None
    for entry in found:
        if entry["client"] == identity.client:
            return entry["services"]
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


def _decode_mime(payload: dict, want: str) -> str:
    """Recursively concatenate the base64url-decoded parts of `payload` whose mimeType
    starts with `want`. gog's --json hands back base64 MIME parts (not decoded text) —
    this is the decode every read wrapper reimplemented (echo's bin/echo_read.py._plain)."""
    import base64
    out = ""
    body = payload.get("body", {}) or {}
    if payload.get("mimeType", "").startswith(want) and body.get("data"):
        out += base64.urlsafe_b64decode(body["data"] + "===").decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        out += _decode_mime(part, want)
    return out


# Tags whose boundaries are line breaks once the markup is gone.
_HTML_BLOCK = r"p|div|br|tr|li|h[1-6]|table|blockquote|section|article|ul|ol|pre"


def _html_to_text(html: str) -> str:
    """Flatten an HTML mail body to readable plain text.

    Not a general-purpose renderer — just enough that an agent (and the human reading
    its turn) sees the words: script/style dropped, block boundaries become newlines,
    tags stripped, entities unescaped, runaway blank lines collapsed."""
    import html as _html
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", html)
    t = re.sub(r"(?is)<br\s*/?>", "\n", t)
    t = re.sub(rf"(?is)</(?:{_HTML_BLOCK})\s*>", "\n", t)
    t = re.sub(rf"(?is)<(?:{_HTML_BLOCK})\b[^>]*>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", "", t)          # every remaining tag
    t = _html.unescape(t)
    t = t.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(ln.rstrip() for ln in t.split("\n"))
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _decode_body(payload: dict) -> str:
    """The message's readable text: text/plain when the sender provided it, else the
    text/html twin flattened.

    The fallback is load-bearing. A message whose ONLY body part is `text/html` — an
    out-of-office auto-reply is the canonical case — used to decode to the empty string,
    so the fleet's sanctioned reader rendered it as "(no plain-text body)" and an agent
    could not show the human what it had been handed without hand-rolling a raw gog +
    base64 read. (Origin: 2026-08-19, an echo turn scoped to exactly such a thread.)"""
    plain = _decode_mime(payload, "text/plain")
    if plain.strip():
        return plain
    return _html_to_text(_decode_mime(payload, "text/html"))


# Header evidence that a machine, not a person, generated a message. turn.md requires
# classifying on these BEFORE deciding an action — and warns about the spoof where a
# notification sets From: to a real human while Sender:/Auto-Submitted say machine.
_MACHINE_SENDER = re.compile(
    r"(noreply|no-reply|donotreply|do-not-reply|bounces?|mailer-daemon|"
    r"notification|notifications|automated)[@.-]", re.I)


def _automation_of(h: dict) -> dict:
    """Classify a message as machine- or human-generated from its headers alone."""
    auto_submitted = h.get("auto-submitted", "")
    precedence = h.get("precedence", "")
    sender = h.get("sender", "")
    x_autoreply = h.get("x-autoreply", "")
    automated = bool(
        (auto_submitted and auto_submitted.strip().lower() != "no")
        or precedence.strip().lower() in {"bulk", "list", "junk", "auto_reply"}
        or (x_autoreply and x_autoreply.strip().lower() not in {"", "no"})
        or (sender and _MACHINE_SENDER.search(sender))
    )
    return {"auto_submitted": auto_submitted, "precedence": precedence,
            "sender": sender, "is_automated": automated}


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
                   attachments: [{attachment_id, filename, mime_type, size}],
                   auto_submitted, precedence, sender, is_automated}],
       reply_all: {to, cc, reply_to_message_id}}

    `body_text` is the message's readable text (base64 MIME parts decoded, quoted reply
    tail trimmed) — text/plain when present, otherwise the text/html twin flattened, so an
    HTML-only message (a classic out-of-office auto-reply) is never returned blank.
    `is_automated` answers turn.md's classify-the-machine-mail-FIRST step from the
    `Auto-Submitted` / `Precedence` / `Sender` headers, which are carried alongside it so
    an agent needn't drop to raw gog to triage. `reply_all` is the ready recipient set
    (latest non-self sender = To, the rest = Cc, minus self). Together those are the whole reason agents hand-rolled read wrappers
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
            "body_text": _trim_quoted_tail(_decode_body(m.get("payload", {}) or {})),
            "attachments": _attachments_of(m),
            **_automation_of(h),
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


def collect_thread_attachments(
    identity: EmailIdentity,
    thread_id: str,
    *,
    out_dir: str | None = None,
    runner=subprocess.run,
) -> list[dict]:
    """Download EVERY attachment on a thread → [{filename, path, message_id, ...}].

    This is the forward path. Re-attaching an inbound document used to mean a manual
    `email read` → eyeball the attachment ids → N× `fetch-attachment` → N× `--attach`
    round trip, and the recorded outcome (canopy#462) is that agents skipped it and
    degraded the forward into a body-text summary instead. One call, so the cheap thing
    and the correct thing are the same thing.

    De-duplicates by (filename, size): a thread whose attachment was quoted forward and
    back carries the same file on several messages, and a forward should not ship it
    three times.
    """
    thread = read_thread(identity, thread_id, runner=runner)
    seen: set[tuple[str, object]] = set()
    out: list[dict] = []
    for msg in thread.get("messages", []):
        for att in msg.get("attachments", []):
            key = (att.get("filename", ""), att.get("size"))
            if key in seen:
                continue
            seen.add(key)
            got = fetch_attachment(
                identity, msg["message_id"], att["attachment_id"],
                out_dir=out_dir, runner=runner,
            )
            out.append({**got, "filename": att.get("filename", ""),
                        "mime_type": att.get("mime_type", "")})
    return out


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
        return ident_with_client(ident, client)
    # No explicit override: let the client follow the token this machine actually holds for
    # the mailbox. The repo pins ONE gog_client for every box while the token store is
    # per-machine, so an un-migrated box is pinned to a pair with no credentials and dies on
    # `No auth for gmail <mailbox>` with a working token sitting beside it. Only the client
    # moves — never the account.
    rec = reconcile_client(ident, runner=subprocess.run, apply=True)
    if rec.changed:
        sys.stderr.write(f"NOTE: {rec.note}\n")
    return ident


def ident_with_client(ident: EmailIdentity, client: str) -> EmailIdentity:
    """An explicit `--client` is a human decision — honour it verbatim, no reconciliation."""
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
@click.option("--attach", "attach", multiple=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Attach a file. Repeatable. Use for a deliverable that must ride ON the "
                   "message; a substantial artifact still belongs in a shared gdoc.")
@click.option("--attach-from-thread", "attach_from_thread",
              help="Re-attach EVERY attachment on this thread (de-duplicated) — the "
                   "forward path. Without it a forward silently degrades to a body-text "
                   "summary plus whatever links were in the original (canopy#462). "
                   "Combines with --attach.")
@click.option("--dry-run", is_flag=True, help="Render plain + HTML bodies without sending.")
def email_send(repo, agent, account, client, to, cc, subject, body_file,
               reply_to_message_id, thread_id, reply_all, narrow,
               attach, attach_from_thread, dry_run):
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
        attachments = list(attach)
        if attach_from_thread:
            attachments += [
                a["saved_to"] or a["path"]
                for a in collect_thread_attachments(ident, attach_from_thread)
            ]
        result = send(
            ident, to=to, subject=subject, body_text=Path(body_file).read_text(),
            cc=cc, reply_to_message_id=reply_to_message_id,
            attachments=attachments, dry_run=dry_run,
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


@email_group.command("archive")
@_with_identity_options
@click.argument("thread_ids", nargs=-1, required=True)
def email_archive(repo, agent, account, client, thread_ids):
    """Archive THREAD_IDS out of the agent's OWN inbox (removes INBOX + UNREAD).

    Turn housekeeping for a handled or not-actionable thread — own mailbox only,
    reversible, so it needs no approval gate. Name it in the turn closeout.
    """
    try:
        ident = _identity_from_opts(repo, agent, account, client)
        results = archive(ident, list(thread_ids))
    except AgentEmailError as e:
        raise click.ClickException(str(e))
    failed = 0
    for res in results:
        if res["ok"]:
            click.echo(f"{res['thread_id']} -> archived")
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
    decoded `body_text` (quoted tail trimmed; text/plain, falling back to the flattened
    text/html twin so an HTML-only message is never blank), an `is_automated` flag with the
    `Auto-Submitted`/`Precedence`/`Sender` headers behind it, and each attachment's id; plus
    a thread-level `reply_all` recipient set (To/Cc/reply_to_message_id). Pass a THREAD id
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
