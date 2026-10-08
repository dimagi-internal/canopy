"""Shared canopy-web transport + auth — the one place PAT/base-url resolution
and HTTP live. stdlib urllib only (the canopy plugin has no `requests` dep)."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# Every request says what made it and which turn/session it came from (see
# provenance.py). Imported by name so tests can monkeypatch it here.
from orchestrator.provenance import provenance_headers

DEFAULT_API = "https://canopy.dimagi.com"
TOKEN_FILE = Path.home() / ".claude" / "canopy" / "workbench-token"

Transport = Callable[[str, str, dict, Optional[bytes]], "tuple[int, str]"]


class CanopyError(RuntimeError):
    """A non-2xx response from canopy-web."""


def resolve_base_url(base_url: Optional[str]) -> str:
    if base_url:
        return base_url.rstrip("/")
    from_env = os.environ.get("CANOPY_WEB_API_URL", "").strip()
    if from_env:
        return from_env.rstrip("/")
    return DEFAULT_API


# Product apps that canopy-web scopes to a workspace. A path like
# ``/api/walkthroughs/…`` is rewritten to ``/api/w/<ws>/walkthroughs/…`` when a
# workspace is active; unscoped apps (sessions, system, me, …) are
# left alone. Mirrors WS_SCOPED_API_PREFIXES on the canopy-web frontend.
SCOPED_APPS = ("projects", "walkthroughs", "reviews", "shareouts", "ddd", "timeline")


def resolve_workspace(workspace: Optional[str]) -> Optional[str]:
    """The active canopy-web workspace slug, or None (→ flat routes → the org
    default). Precedence: explicit arg → env ``CANOPY_WEB_WORKSPACE`` → None.
    The DDD layer adds a per-repo config source on top of this (see
    ``scripts/ddd/auth.resolve_ddd_workspace``)."""
    if workspace:
        return workspace.strip() or None
    from_env = os.environ.get("CANOPY_WEB_WORKSPACE", "").strip()
    return from_env or None


def scoped_api_path(path: str, workspace: Optional[str] = None) -> str:
    """Rewrite a flat ``/api/<app>/…`` path to the tenant path
    ``/api/w/<ws>/<app>/…`` when a workspace is active and ``<app>`` is scoped.
    A no-op when there is no workspace, the path isn't under ``/api/``, or the
    app isn't workspace-scoped."""
    ws = resolve_workspace(workspace)
    if not ws or not path.startswith("/api/"):
        return path
    rest = path[len("/api"):]  # "/walkthroughs/…"
    app = rest.lstrip("/").split("/", 1)[0]
    if app not in SCOPED_APPS:
        return path
    return f"/api/w/{ws}{rest}"


def scoped_app_path(path: str, workspace: Optional[str] = None) -> str:
    """Rewrite a flat browser route (e.g. ``/ddd/<slug>/<run>``) to its tenant
    form ``/w/<ws>/ddd/<slug>/<run>`` when a workspace is active — so package /
    landing links a human clicks open in the right workspace. No-op when there
    is no workspace."""
    ws = resolve_workspace(workspace)
    if not ws or not path.startswith("/"):
        return path
    return f"/w/{ws}{path}"


# Artifact pages canopy-web serves ONLY under /w/<workspace>/ (canopy-web#1289,
# #1337, #1338, #1340): the flat forms are a plain 404, never redirected. A link the CLI prints or stores is always built by
# app_url / scope_link, never by gluing a flat path onto the base URL — a flat
# /review/<id> copied out of `narrative post` went to an external reviewer
# (ace, 2026-10-08).
# ddd-release before ddd: a regex alternation takes the first that fits.
ARTIFACT_ROUTES = ("walkthrough", "review", "share", "ddd-release", "ddd",
                   "storyboard", "narrative")

#: An absolute link whose path STARTS with a flat artifact route — the page, its
#: ``/content`` byte stream (scoped too since canopy-web#1338) — or with the
#: pre-tenancy ``/w/<uuid>`` walkthrough form. canopy-web answers every one with
#: a 404. The ratchet in tests/test_scoped_artifact_urls.py runs every
#: link-printing path through this, and ``canopy email send`` refuses a body
#: that carries one (:func:`flat_canopy_links`).
FLAT_ARTIFACT_URL_RE = re.compile(
    r"https?://(?P<host>[^/\s\"'<>]+)(?:/canopy)?"
    r"/(?:(?:%s)/|w/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![\w-]))"
    r"[^\s\"'<>)\]]*" % "|".join(ARTIFACT_ROUTES)
)

#: Hosts that serve canopy-web, for checks on free text (an email body) where a
#: third party's ``https://other.site/share/…`` is not ours to judge.
CANOPY_HOSTS = ("canopy.dimagi.com", "labs.connect.dimagi.com")


def flat_canopy_links(text: str, base_url: Optional[str] = None) -> list:
    """Every flat canopy-web artifact link in ``text`` — on a canopy host
    (:data:`CANOPY_HOSTS` plus the resolved base URL's), so a stranger's
    ``/share/`` link in a quoted thread is not flagged."""
    from urllib.parse import urlsplit

    hosts = set(CANOPY_HOSTS)
    hosts.add((urlsplit(resolve_base_url(base_url)).hostname or "").lower())
    out = []
    for m in FLAT_ARTIFACT_URL_RE.finditer(text or ""):
        host = m.group("host").split(":", 1)[0].lower()
        if host in hosts:
            out.append(m.group(0))
    return out

_ARTIFACT_PATH_RE = re.compile(
    # prefix: the deployment mount (/canopy on labs, CANOPY_PUBLIC_BASE_URL) —
    # nothing else, so a third-party URL with /review/ deeper in it is left alone.
    r"^(?P<prefix>(?:/canopy)?)(?:/w/(?P<ws>[^/?#]+))?/(?P<kind>%s)(?P<rest>/.*)?$"
    % "|".join(ARTIFACT_ROUTES)
)


def app_url(path: str, workspace: Optional[str], base_url: Optional[str] = None) -> str:
    """The absolute link a human opens for an artifact page:
    ``https://<host>/w/<workspace><path>``.

    ``workspace`` is the workspace the artifact was written into (a write's
    resolved target, or the slug the server echoed back). There is no flat
    fallback: with none, this raises :class:`WorkspaceRequiredError` rather than
    print a link that 404s once canopy-web#1337 lands."""
    ws = (workspace or "").strip()
    if not ws:
        raise WorkspaceRequiredError(
            f"cannot build a canopy-web link for {path!r} without its workspace — "
            "artifact pages exist only under /w/<workspace>/ (canopy-web#1337)."
        )
    if not path.startswith("/"):
        path = "/" + path
    return f"{resolve_base_url(base_url)}/w/{ws}{path}"


def scope_link(url: str, workspace: Optional[str], base_url: Optional[str] = None) -> str:
    """Normalize an artifact link canopy-web returned (relative or absolute,
    flat or scoped) to its absolute ``/w/<workspace>/…`` form, keeping the
    query (``?t=``) and fragment.

    A link already under ``/w/<ws>/`` keeps the server's workspace. A relative
    link, or one minted on ``localhost`` (canopy-web#1289's MCP-in-process bug),
    is rebased onto ``base_url``. A non-artifact URL (an external embed, a
    Drive link) is returned unchanged. A flat artifact link with no workspace
    to put it under raises :class:`WorkspaceRequiredError`."""
    raw = (url or "").strip()
    if not raw:
        return raw
    from urllib.parse import urlsplit

    parts = urlsplit(raw)
    if parts.scheme and parts.scheme not in ("http", "https"):
        return raw
    m = _ARTIFACT_PATH_RE.match(parts.path or "")
    if not m:
        return raw
    host = (parts.hostname or "").lower()
    if parts.netloc and host not in ("localhost", "127.0.0.1"):
        origin = f"{parts.scheme}://{parts.netloc}{m.group('prefix')}"
    else:
        origin = resolve_base_url(base_url)
    ws = m.group("ws") or (workspace or "").strip()
    path = f"/{m.group('kind')}{m.group('rest') or ''}"
    scoped = app_url(path, ws, origin)
    if parts.query:
        scoped += f"?{parts.query}"
    if parts.fragment:
        scoped += f"#{parts.fragment}"
    return scoped


# Plugins that carry a `.claude-plugin/plugin.json` but are NOT fleet agents. The
# canopy runtime (where every `python -m scripts.ddd.*` runs) sits inside the
# canopy plugin's own install dir, so without this every DDD call claimed to be
# "in the 'canopy' agent repo" and warned about a borrowed identity that is
# simply the operator's (0.2.528, M5).
_NON_AGENT_SLUGS = frozenset({"canopy"})


def _agent_slug_for_cwd(start: Optional[Path] = None) -> str:
    """The agent slug of the repo we're standing in, or "" if this isn't one.

    An agent turn runs INSIDE the agent's repo, so the repo is the identity: walk
    up for `.claude-plugin/plugin.json` and take its `name`.
    """
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        manifest = d / ".claude-plugin" / "plugin.json"
        if not manifest.is_file():
            continue
        try:
            slug = (json.loads(manifest.read_text(encoding="utf-8")) or {}).get("name") or ""
        except (OSError, ValueError):
            return ""
        return "" if slug in _NON_AGENT_SLUGS else slug
    return ""


# One warning per process: a single command makes many calls, and warning on each
# would train people to scroll past it.
_WARNED_BORROWED_IDENTITY = False


def _reset_identity_warning() -> None:
    """Test seam — clear the once-per-process latch."""
    global _WARNED_BORROWED_IDENTITY
    _WARNED_BORROWED_IDENTITY = False


def _warn_borrowed_identity(slug: str) -> None:
    """Say out loud that an agent is about to act as the operator.

    This fallback is legitimate (a human working in an agent repo must not be
    blocked), so this warns rather than refuses. What it must not be is SILENT:
    ACE ran for a full day attributed to a human because `~/.ace/.env` was never
    materialized, and nothing anywhere said so (dimagi-internal/ace#1005).
    """
    global _WARNED_BORROWED_IDENTITY
    if _WARNED_BORROWED_IDENTITY:
        return
    _WARNED_BORROWED_IDENTITY = True
    print(
        f"[canopy] WARNING: this is agent '{slug}''s session, but ~/.{slug}/.env has no "
        f"CANOPY_WEB_PAT — falling back to the operator's workbench-token.\n"
        f"[canopy] Calls will be attributed to the OPERATOR, not to '{slug}', and "
        f"will see the operator's workspaces.\n"
        f"[canopy] If you are {slug}: materialize its env "
        f"(`op inject -i .env.tpl -o ~/.{slug}/.env`) or set CANOPY_WEB_PAT.",
        file=sys.stderr,
    )


def _slug_env_pat(slug: str) -> str:
    """CANOPY_WEB_PAT out of agent ``slug``'s provisioned ``~/.<slug>/.env``, or ""."""
    if not slug or slug in _NON_AGENT_SLUGS or not _SLUG_RE.match(slug):
        return ""
    env_file = Path.home() / f".{slug}" / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("CANOPY_WEB_PAT="):
                return line.partition("=")[2].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _agent_env_pat(start: Optional[Path] = None) -> str:
    """This agent's OWN PAT from `~/.<slug>/.env`, or "" if there isn't one.

    An agent turn runs INSIDE the agent's repo, so the repo is the identity: walk
    up from cwd for `.claude-plugin/plugin.json`, take its `name` as the slug, and
    read CANOPY_WEB_PAT out of that agent's provisioned env file.

    Without this, per-agent PATs only work where a runner happens to inject the
    env. The cloud runner does; the laptop runner drives emdash's UI over CDP and
    never builds an env at all — so on a laptop every agent silently fell through
    to the operator's own workbench-token and acted as the HUMAN, with nothing
    failing to reveal it. Resolving from the repo makes identity follow the agent
    on every host instead of depending on how it happened to be launched.
    """
    return _slug_env_pat(_agent_slug_for_cwd(start))


# Env vars that name the agent whose session this is. `CANOPY_AGENT` is set by
# every agent repo's settings.json and by the cloud runner for every agent turn;
# `CANOPY_AGENT_SLUG` is the harness's name for the same thing (run_store, shareout).
_AGENT_ENV_VARS = ("CANOPY_AGENT", "CANOPY_AGENT_SLUG")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.I)


def _dispatched_agent_slug() -> str:
    """The agent a canopy-web turn was dispatched AT, or "" when this isn't one.

    canopy writes a caller envelope for every turn it delivers; its `agent` names
    the agent the turn belongs to (null for a turn aimed at a repo). Found the same
    way provenance finds the parent turn — `CANOPY_CALLER`, else the turn id (env, or
    the laptop's by-task record) → `<caller_root>/<turn_id>.json` — so a dispatched
    turn whose cwd is not the agent's repo is still recognised as the agent's.
    """
    from orchestrator import provenance  # local: provenance is only needed here

    paths = []
    caller = os.environ.get("CANOPY_CALLER", "").strip()
    if caller:
        paths.append(Path(caller))
    try:
        turn = provenance.resolve_parent().get("turn_id") or ""
    except Exception:  # noqa: BLE001 — identity lookup must never crash a call
        turn = ""
    if turn:
        paths.append(provenance.caller_root() / f"{turn}.json")
    for path in paths:
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        slug = env.get("agent") if isinstance(env, dict) else None
        if isinstance(slug, str) and _SLUG_RE.match(slug.strip()):
            slug = slug.strip()
            return "" if slug in _NON_AGENT_SLUGS else slug
    return ""


def agent_context_slug(start: Optional[Path] = None) -> str:
    """The agent this process is working FOR, or "" when it is a human's own.

    Three signals, strongest first: `$CANOPY_AGENT` / `$CANOPY_AGENT_SLUG` (set by
    every agent repo's settings.json and the cloud runner), the turn canopy-web
    dispatched (its caller envelope's `agent`), then the cwd's agent repo.

    The env wins over the cwd walk-up because the cwd lies in exactly the case that
    matters: every `python -m scripts.ddd.*` runs with cwd = the canopy runtime
    (inside the canopy plugin, a non-agent), so an ACE session posting a narrative
    looked like nobody's — and fell through to the operator's token (ace#2805).
    """
    for var in _AGENT_ENV_VARS:
        slug = os.environ.get(var, "").strip()
        if slug and slug not in _NON_AGENT_SLUGS and _SLUG_RE.match(slug):
            return slug
    return _dispatched_agent_slug() or _agent_slug_for_cwd(start)


class AgentIdentityError(RuntimeError):
    """An agent's session has no PAT of its own, and strict mode refused to borrow
    the operator's workbench-token."""


# The one deliberate way for a HUMAN working inside an agent's repo to act as
# themselves through the fallback. Never set by a runner; DDD writes ignore it.
ALLOW_OPERATOR_ENV = "CANOPY_ALLOW_OPERATOR_IDENTITY"


def _operator_token() -> str:
    """The operator's workbench-token, or "" — read only to compare or fall back."""
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else ""
    except OSError:
        return ""


def _agent_identity_error(slug: str, why: str) -> "AgentIdentityError":
    return AgentIdentityError(
        f"this is agent '{slug}'s session, but {why}. Refusing to act as the operator "
        f"(workbench-token {TOKEN_FILE}): canopy-web would attribute every call to the "
        f"human with the human's rights, and a write would land in the human's "
        f"workspaces (canopy#813, ace#2805). Set CANOPY_WEB_PAT to {slug}'s own PAT, or "
        f"materialize its env (`op inject -i .env.tpl -o ~/.{slug}/.env`). A human "
        f"deliberately working as themselves in this repo: {ALLOW_OPERATOR_ENV}=1."
    )


def resolve_token(token: Optional[str], *, agent_strict: bool = False) -> str:
    """The canopy-web PAT this process acts with.

    Precedence: explicit arg → env ``CANOPY_WEB_PAT`` → the agent's own PAT
    (``~/.<slug>/.env``, slug from :func:`agent_context_slug`) → the operator's
    ``TOKEN_FILE``.

    In an agent's session (:func:`agent_context_slug` is non-empty) that last tier
    is never used, and neither is a ``CANOPY_WEB_PAT`` that is just the operator's
    token copied into the env: the agent acts as itself or raises
    :class:`AgentIdentityError` (canopy#813 — the board drain and dispatch acted as
    the machine's owner). ``CANOPY_ALLOW_OPERATOR_IDENTITY=1`` lets a human working
    in an agent repo opt back into the warned fallback; ``agent_strict=True`` (DDD
    and walkthrough-share writes, which publish under a name) ignores even that.
    """
    if token:
        return token
    slug = agent_context_slug()
    allow_operator = bool(slug) and not agent_strict and \
        os.environ.get(ALLOW_OPERATOR_ENV, "").strip() == "1"
    # Explicit env wins: it is how a runner pins the identity for a turn.
    from_env = os.environ.get("CANOPY_WEB_PAT", "").strip()
    if from_env:
        if slug and not allow_operator and from_env == _operator_token():
            raise _agent_identity_error(
                slug, "CANOPY_WEB_PAT holds the operator's workbench-token, not its own PAT")
        return from_env
    # Then the agent's own PAT, so an agent acts as ITSELF rather than as whoever
    # owns TOKEN_FILE. This must come BEFORE the global file — that file exists on
    # every operator laptop, so checking it first is exactly what masked the bug.
    agent_pat = _slug_env_pat(slug) or _agent_env_pat()
    if agent_pat:
        return agent_pat
    if slug and not allow_operator:
        raise _agent_identity_error(
            slug, f"no PAT of its own resolved (CANOPY_WEB_PAT is unset and "
                  f"~/.{slug}/.env has no CANOPY_WEB_PAT)")
    stored = _operator_token()
    if stored:
        # Only reached in an agent's session through the explicit opt-in. Still
        # an identity swap, so it must not happen quietly.
        if slug:
            _warn_borrowed_identity(slug)
        return stored
    raise RuntimeError(
        f"no canopy-web PAT — run /canopy:canopy-web-pat-mint to mint one, "
        f"or set CANOPY_WEB_PAT. Expected token at {TOKEN_FILE}."
    )


# --- Say who a write acts as -------------------------------------------------
#
# canopy#813: nothing on the write path said WHOSE identity it carried, so the
# fleet's board drains ran as the owner for as long as nobody looked. Before the
# first write with a resolved token, read `/api/me/` back and print it — once per
# (server, token) per process, on stderr so stdout stays machine-readable.

ME_PATH = "/api/me/"
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ANNOUNCED: dict = {}


def _reset_identity_announcements() -> None:
    """Test seam — forget which identities this process has announced."""
    _ANNOUNCED.clear()


def announce_identity(base: str, tok: str, transport: "Transport") -> str:
    """Print (once) the identity ``tok`` acts as on ``base``; return its email, or ""
    if the read failed. A failed read warns and never blocks the write."""
    key = (base, tok)
    if key in _ANNOUNCED:
        return _ANNOUNCED[key]
    slug = agent_context_slug()
    who = name = ""
    try:
        status, text = transport("GET", base + ME_PATH,
                                 {**provenance_headers(), "Authorization": f"Bearer {tok}"}, None)
        me = json.loads(text) if 200 <= status < 300 and text.strip() else {}
        if isinstance(me, dict):
            who = str(me.get("email") or me.get("username") or "").strip()
            name = str(me.get("name") or "").strip()
    except Exception:  # noqa: BLE001 — never fail the write over the read-back
        who = name = ""
    _ANNOUNCED[key] = who
    ctx = f" for agent '{slug}'" if slug else ""
    if who:
        label = f"{who} ({name})" if name and name != who else who
        print(f"[canopy] writing as {label}{ctx} — per {ME_PATH}", file=sys.stderr)
    else:
        print(f"[canopy] WARNING: could not read {ME_PATH} — writing{ctx} with an "
              f"identity canopy did not confirm", file=sys.stderr)
    return who


# The real implementation, kept addressable so a test can restore it after the
# suite-wide fixture swaps `announce_identity` out.
_announce_identity_impl = announce_identity


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes]) -> "tuple[int, str]":
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def call(method: str, path: str, body=None, *,
         base_url: Optional[str] = None, token: Optional[str] = None,
         workspace: Optional[str] = None,
         transport: Optional[Transport] = None,
         headers: Optional[dict] = None) -> dict:
    base = resolve_base_url(base_url)
    tok = resolve_token(token)
    path = scoped_api_path(path, workspace)  # → /api/w/<ws>/… when a workspace is active
    transport = transport or urllib_transport
    # A token the caller handed in is its own statement of identity; one canopy
    # RESOLVED is what goes unexamined, so that is the one a write reads back.
    if not token and method.upper() in _WRITE_METHODS:
        announce_identity(base, tok, transport)
    # Provenance first, so a caller's own header (or the bearer) always wins.
    headers = {**provenance_headers(), **(headers or {}), "Authorization": f"Bearer {tok}",
               "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    status, text = transport(method, base + path, headers, data)
    if not (200 <= status < 300):
        raise CanopyError(f"{method} {path} -> {status}: {text[:400]}")
    return json.loads(text) if text.strip() else {}


def call_text(method: str, path: str, *,
              base_url: Optional[str] = None, token: Optional[str] = None,
              workspace: Optional[str] = None,
              transport: Optional[Transport] = None) -> str:
    """`call`, for a route whose body is NOT JSON (e.g. a turn's raw JSONL
    transcript at ``/api/harness/turns/<id>/transcript``). Same auth, base-url and
    error contract — a non-2xx raises `CanopyError` — but the body comes back as
    text, undecoded."""
    base = resolve_base_url(base_url)
    tok = resolve_token(token)
    path = scoped_api_path(path, workspace)
    transport = transport or urllib_transport
    status, text = transport(method, base + path,
                             {**provenance_headers(), "Authorization": f"Bearer {tok}"}, None)
    if not (200 <= status < 300):
        raise CanopyError(f"{method} {path} -> {status}: {text[:400]}")
    return text


# --- Writes that must land in a NAMED workspace -------------------------------
#
# A write with no workspace goes to the flat route, and canopy-web files it in the
# caller's default workspace — for a human in dimagi + connect that is `dimagi`,
# whatever the content is about. ace#2805 / canopy-web#1289: an ACE session posted
# the chlorine narrative (Connect's) into dimagi, as the operator, and nothing said
# so. These helpers make a write name its tenant up front and confirm afterwards
# where it actually landed.

class WorkspaceRequiredError(RuntimeError):
    """A canopy-web write refused because the caller named no workspace."""


class WorkspaceMismatchError(RuntimeError):
    """A write succeeded but the object is not readable in the workspace it was
    meant for — it landed somewhere else."""


def require_write_workspace(workspace: Optional[str], *,
                            how_to_set: str = "set CANOPY_WEB_WORKSPACE=<slug>") -> str:
    """The workspace a write goes INTO — always one the caller named.

    ``workspace`` is the caller's already-resolved choice (arg/env/config). Empty
    raises :class:`WorkspaceRequiredError`: a flat route lets the server pick its
    default, and guessing from memberships is the same guess one step removed.
    """
    ws = (workspace or "").strip()
    if ws:
        return ws
    raise WorkspaceRequiredError(
        f"no canopy-web workspace named for this write. Refusing to let the server "
        f"pick its default (that is how a Connect narrative landed in dimagi, "
        f"ace#2805) — {how_to_set}."
    )


def confirm_landed(app: str, obj_id: str, workspace: str, *, query: Optional[dict] = None,
                   base_url: Optional[str] = None, token: Optional[str] = None,
                   transport: Optional[Transport] = None) -> dict:
    """Read a just-written object back through the TENANT-PINNED list
    (``/api/w/<ws>/<app>/``) and return its row.

    The pinned list holds only that workspace's rows, so finding the id there is
    proof of where it landed (a by-id GET is not: a link-visibility object reads
    from any tenant). Prints the workspace it landed in. Raises
    :class:`WorkspaceMismatchError` when the id is absent; a failed READ (network,
    5xx) only warns — the write already happened and a flaky read must not hide it.
    """
    import urllib.parse

    path = f"/api/w/{workspace}/{app}/"
    if query:
        path += "?" + urllib.parse.urlencode(query)
    try:
        rows = call("GET", path, base_url=base_url, token=token, transport=transport) or []
    except (CanopyError, OSError, ValueError) as exc:
        print(f"[canopy] WARNING: wrote {app} {obj_id} but could not read it back from "
              f"workspace '{workspace}' to confirm where it landed: {exc}", file=sys.stderr)
        return {}
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    for row in rows:
        if isinstance(row, dict) and str(row.get("id")) == str(obj_id):
            who = f", owner {row['owner_email']}" if row.get("owner_email") else ""
            print(f"[canopy] landed in workspace '{workspace}' ({app} {obj_id}{who})",
                  file=sys.stderr)
            return row
    raise WorkspaceMismatchError(
        f"wrote {app} {obj_id} but it is NOT in workspace '{workspace}' — it landed in "
        f"another workspace. Check which identity wrote it (CANOPY_WEB_PAT) and move or "
        f"delete it."
    )
