"""`canopy brain preview` — what canopy's brain WOULD do with a window of sessions, persisting nothing.

The owner steps up as himself and asks: for every canopy session of the last day, which project
links would the brain store, would it be allowed to read the session now (and if everyone in it
had agreed), and what would the learner recommend? canopy-web answers the first two from
``GET /api/brain/preview`` — pure compute, it writes nothing. This
command pages that endpoint and lays the answer out on disk for the ``brain-preview`` skill, which
runs the learner step locally and renders one HTML report.

    canopy brain preview [--since 24h] [--until ISO] [--view full|conversation|userinput|opening]
                         [--out DIR] [--step-up] [--limit N] [--json-output]
    canopy brain report DIR        # DIR/report.html from the preview + the learner's notes

**Who asks.** The endpoint answers a PERSON only (browser, PAT, or an act-as token). In your own
session the command reads with your canopy-web login. In an agent's session pass ``--step-up``:
it reads through the act-as grant this turn already holds (``canopy act-as request --scope read``,
skill ``canopy:act-as-you``), in process — the credential stays inside ``act_as_cli``.

**What it writes.** ``DIR/<session id>.json`` per session (the server's session object plus
``url``, the session page on canopy-web) and ``DIR/index.json`` (window, counts, one row per
session). DIR defaults to a fresh temp dir. Nothing goes to canopy-web: every call is a GET.

**Paging.** ``after`` / ``limit`` until ``done`` (or no ``next_after``). A long session whose
transcript is split across pages arrives under the same id more than once; its messages are
joined in order (by ``index``) and the last page's ``truncated`` wins.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import tempfile
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterable, Optional

import click

from orchestrator import canopy_web

PREVIEW_PATH = "/api/brain/preview"
#: canopy-web's transcript views, widest first: everything incl. tool calls; user + assistant
#: text; only what people typed (`userinput`); the first few typed lines.
VIEWS = ("full", "conversation", "userinput", "opening")
TIERS = ("core", "adjacent", "candidate")
DEFAULT_LIMIT = 10
MAX_PAGES = 1000

_REL = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.I)
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

#: Test seam: the clock.
NOW: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(_dt.timezone.utc)

#: One preview page for these query parameters.
Fetch = Callable[[dict], dict]


class PreviewError(click.ClickException):
    pass


# --- time window -----------------------------------------------------------------

def _iso(when: _dt.datetime) -> str:
    return when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(value: str, now: _dt.datetime) -> str:
    """``24h`` / ``90m`` / ``7d`` / ``2w`` (that long before ``now``) or an ISO date/time
    (naive = UTC) → an ISO-8601 UTC timestamp."""
    raw = (value or "").strip()
    m = _REL.match(raw)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return _iso(now - _dt.timedelta(**{_UNITS[unit]: n}))
    try:
        when = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise click.BadParameter(f"{value!r}: use 24h / 90m / 7d / 2w or an ISO date-time") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return _iso(when)


# --- transport: as yourself, or as the person via the act-as grant -------------------

def _not_there(detail: str) -> PreviewError:
    return PreviewError(
        f"this canopy-web has no brain preview yet ({PREVIEW_PATH} answered 404: {detail}) — "
        "nothing was read.")


def _refused(detail: str, step_up: bool) -> PreviewError:
    if step_up:
        return PreviewError(f"canopy-web refused the preview as the person: {detail}")
    return PreviewError(
        f"canopy-web refused the preview ({detail}). It answers a PERSON only — in an agent's "
        "session ask the person to approve a read-scope step-up (skill canopy:act-as-you: "
        "`canopy act-as request --scope read --no-wait --reason …`, then `canopy act-as status "
        "--wait`) and run again with --step-up.")


def person_fetch() -> Fetch:
    """Read with this session's own canopy-web login (a person's PAT in their own session)."""
    def fetch(params: dict) -> dict:
        try:
            return canopy_web.call("GET", page_path(params))
        except canopy_web.CanopyError as exc:
            text = str(exc)
            if "-> 404:" in text:
                raise _not_there(text) from None
            if "-> 401:" in text or "-> 403:" in text:
                raise _refused(text, False) from None
            raise PreviewError(text) from None
        except RuntimeError as exc:  # no PAT, or an agent identity that must not borrow one
            raise _refused(str(exc), False) from None
    return fetch


def step_up_fetch() -> Fetch:
    """Read AS THE PERSON through this turn's approved act-as grant (read scope suffices)."""
    from orchestrator import act_as_cli

    def fetch(params: dict) -> dict:
        try:
            got = act_as_cli.as_person("GET", page_path(params))
        except act_as_cli.Refused as exc:
            if exc.status == 404:
                raise _not_there(exc.message) from None
            raise _refused(exc.message, True) from None
        if not isinstance(got, dict):
            raise PreviewError(f"canopy-web answered the preview with something not JSON: {str(got)[:200]}")
        return got
    return fetch


# --- paging ------------------------------------------------------------------------

def page_path(params: dict) -> str:
    return f"{PREVIEW_PATH}?{urllib.parse.urlencode(params)}"


def walk(fetch: Fetch, *, since: str, until: str, view: str, limit: int = DEFAULT_LIMIT,
         max_pages: int = MAX_PAGES) -> Iterable[dict]:
    """Every page of the preview for [since, until), in order."""
    after: Optional[str] = None
    for _ in range(max_pages):
        params = {"since": since, "until": until, "view": view, "limit": str(limit)}
        if after:
            params["after"] = after
        page = fetch(params)
        if not isinstance(page, dict):
            raise PreviewError(f"canopy-web answered a preview page that is not an object: {page!r:.200}")
        yield page
        nxt = page.get("next_after")
        if page.get("done") or not nxt:
            return
        if nxt == after:
            raise PreviewError(f"canopy-web handed back the same cursor twice ({nxt!r}) — stopping "
                               "rather than looping.")
        after = str(nxt)
    raise PreviewError(f"the preview did not finish in {max_pages} pages — narrow the window "
                       "(--since/--until).")


def merge_sessions(pages: Iterable[dict]) -> "tuple[list[dict], dict]":
    """Sessions in first-seen order, a session split across pages joined into one; plus the
    first page's ``window``/``view``."""
    order: list[str] = []
    byid: dict[str, dict] = {}
    head: dict = {}
    for page in pages:
        if not head:
            head = {"window": page.get("window") or {}, "view": page.get("view")}
        for s in page.get("sessions") or []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            sid = str(s["id"])
            if sid not in byid:
                order.append(sid)
                byid[sid] = s
                continue
            _join(byid[sid], s)
    return [byid[i] for i in order], head


def _join(into: dict, more: dict) -> None:
    a = into.setdefault("transcript", {}) or {}
    b = more.get("transcript") or {}
    seen = {m.get("index") for m in a.get("messages") or [] if isinstance(m, dict)}
    msgs = list(a.get("messages") or [])
    msgs += [m for m in b.get("messages") or [] if not (isinstance(m, dict) and m.get("index") in seen)]
    msgs.sort(key=lambda m: (m.get("index") is None, m.get("index") or 0) if isinstance(m, dict) else (True, 0))
    a["messages"] = msgs
    a["chars"] = int(a.get("chars") or 0) + int(b.get("chars") or 0)
    if "truncated" in b:
        a["truncated"] = bool(b["truncated"])
    into["transcript"] = a
    for k, v in more.items():  # anything the first page lacked (links on a later page, say)
        if k != "transcript" and not into.get(k) and v:
            into[k] = v


# --- the summary ---------------------------------------------------------------------

def read_classes(would_read) -> dict:
    """``{class: {"now": {...}, "if_everyone_agreed": {...}}}`` — the per-class shape, or the
    flat ``{"now", "if_everyone_agreed"}`` shape filed under class ``all``."""
    if not isinstance(would_read, dict):
        return {}
    if "now" in would_read or "if_everyone_agreed" in would_read:
        return {"all": would_read}
    return {k: v for k, v in would_read.items() if isinstance(v, dict)}


def _ok(v) -> bool:
    return bool(isinstance(v, dict) and v.get("ok"))


def session_url(session: dict, base: str) -> str:
    sid = urllib.parse.quote(str(session.get("id") or ""), safe="")
    ws = (session.get("workspace") or {}).get("slug") if isinstance(session.get("workspace"), dict) else ""
    if ws:
        return canopy_web.app_url(f"/sessions/{sid}", ws, base)
    return f"{base.rstrip('/')}/sessions/{sid}"


def counts(sessions: list[dict]) -> dict:
    reads: dict = defaultdict(lambda: {"now": 0, "if_everyone_agreed": 0})
    tiers: Counter = Counter()
    by_project: dict = defaultdict(Counter)
    truncated = 0
    for s in sessions:
        for cls, v in read_classes(s.get("would_read")).items():
            reads[cls]["now"] += _ok(v.get("now"))
            reads[cls]["if_everyone_agreed"] += _ok(v.get("if_everyone_agreed"))
        for ln in s.get("links") or []:
            if isinstance(ln, dict):
                tier = ln.get("tier") or "unknown"
                tiers[tier] += 1
                by_project[ln.get("project") or "?"][tier] += 1
        truncated += bool((s.get("transcript") or {}).get("truncated"))
    return {"sessions": len(sessions), "transcripts_truncated": truncated,
            "would_read": dict(reads),
            "links": {t: tiers.get(t, 0) for t in [*TIERS, *sorted(set(tiers) - set(TIERS))]},
            "links_by_project": {p: dict(c) for p, c in sorted(by_project.items())}}


def index_row(s: dict, file: str, url: str) -> dict:
    t = s.get("transcript") or {}
    return {"id": s.get("id"), "title": s.get("title") or "", "workspace": s.get("workspace"),
            "started_at": s.get("started_at"), "url": url, "file": file,
            "participants": s.get("participants") or [], "would_read": s.get("would_read") or {},
            "links": s.get("links") or [], "messages": len(t.get("messages") or []),
            "chars": t.get("chars"), "truncated": bool(t.get("truncated"))}


def write_out(out: Path, sessions: list[dict], head: dict, *, since: str, until: str, view: str,
              base: str, step_up: bool) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in sessions:
        url = session_url(s, base)
        name = _SAFE_ID.sub("_", str(s["id"])) + ".json"
        (out / name).write_text(json.dumps({**s, "url": url}, indent=2), encoding="utf-8")
        rows.append(index_row(s, name, url))
    index = {"window": head.get("window") or {"since": since, "until": until},
             "view": head.get("view") or view, "generated_at": _iso(NOW()), "base_url": base,
             "as": "person via act-as step-up" if step_up else "own login",
             "counts": counts(sessions), "sessions": rows}
    (out / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def summary_lines(index: dict, out: Path) -> list[str]:
    c = index["counts"]
    w = index.get("window") or {}
    lines = [f"Brain preview {w.get('since')} → {w.get('until')} (view {index.get('view')}, "
             f"{index.get('as')}) — nothing was written to canopy-web.",
             f"  sessions: {c['sessions']}"
             + (f" ({c['transcripts_truncated']} transcripts truncated)" if c["transcripts_truncated"] else "")]
    if c["would_read"]:
        lines.append(f"  {'would read':<14}{'now':>6}{'if everyone agreed':>21}")
        for cls, v in c["would_read"].items():
            lines.append(f"    {cls:<12}{v['now']:>6}{v['if_everyone_agreed']:>21}")
    links = ", ".join(f"{t} {n}" for t, n in c["links"].items())
    lines.append(f"  links it would store: {links} (across {len(c['links_by_project'])} projects)")
    lines.append(f"  wrote {c['sessions']} session files + index.json to {out}")
    return lines


# --- command -------------------------------------------------------------------------

@click.group("brain")
def brain_group():
    """canopy's brain on canopy-web — read-only previews of what it would do."""


@brain_group.command("preview")
@click.option("--since", default="24h", show_default=True,
              help="Window start: 24h / 90m / 7d / 2w back from now, or an ISO date-time.")
@click.option("--until", default=None, help="Window end (ISO, or 24h-style). Default: now.")
@click.option("--view", type=click.Choice(VIEWS), default="full", show_default=True,
              help="Which read of each transcript (widest first).")
@click.option("--out", "out_dir", type=click.Path(file_okay=False), default=None,
              help="Directory for <session>.json + index.json. Default: a new temp dir.")
@click.option("--step-up", is_flag=True,
              help="Read as the person through this turn's approved act-as grant (read scope).")
@click.option("--limit", type=click.IntRange(1, 100), default=DEFAULT_LIMIT, show_default=True,
              help="Sessions per page.")
@click.option("--json-output", "as_json", is_flag=True, help="Print index.json's counts as JSON.")
def preview_cmd(since, until, view, out_dir, step_up, limit, as_json):
    """Page the brain preview for a window and write one JSON per session + index.json."""
    now = NOW()
    since_iso = parse_when(since, now)
    until_iso = parse_when(until, now) if until else _iso(now)  # frozen for the whole walk
    if since_iso >= until_iso:
        raise click.BadParameter(f"--since {since_iso} is not before --until {until_iso}")
    fetch = step_up_fetch() if step_up else person_fetch()
    sessions, head = merge_sessions(walk(fetch, since=since_iso, until=until_iso, view=view, limit=limit))
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="brain-preview-"))
    base = canopy_web.resolve_base_url(None)
    index = write_out(out, sessions, head, since=since_iso, until=until_iso, view=view, base=base,
                      step_up=step_up)
    if as_json:
        click.echo(json.dumps({"out": str(out), "window": index["window"], "view": index["view"],
                               "counts": index["counts"]}, indent=2))
    else:
        for line in summary_lines(index, out):
            click.echo(line)


@brain_group.command("report")
@click.argument("out_dir", type=click.Path(exists=True, file_okay=False))
def report_cmd(out_dir):
    """Render DIR/report.html from a preview dir (+ the learner's notes, if any). Local only."""
    from orchestrator import brain_report

    click.echo(f"wrote {brain_report.build(Path(out_dir))}")
