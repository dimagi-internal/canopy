"""Injectable session-corpus sources — the seam that stops the cross-user blind spot.

WHY this exists: `agent_coverage.coverage_report` reports which of an agent's
declared skills ever FIRED, by scanning that agent's Claude Code transcripts. It
found them via `agent_review.find_turn_transcripts(repo, hours,
projects_dir=CLAUDE_PROJECTS)` where `CLAUDE_PROJECTS = Path.home() / ".claude" /
"projects"` — ONE place: the CURRENT user's home. This machine has TWO macOS
accounts (jjackson + acedimagi) JJ alternates between when he runs out of
tokens. Scanning only one inverts the report's conclusions: `hal/architect` was
reported `never_live` ("13 commits, never fired") when it had actually fired
three times — on the OTHER account, in a worktree checkout invisible to the
single-home scan. Every "never fired" claim was untrustworthy until this fix.

`harvest.py`'s module docstring already states the rule this module encodes:
"Cross-user or the conclusion inverts. JJ alternates macOS accounts (acedimagi +
jjackson)" and "Flag own blindness. `confidence: half-blind` is a first-class
field whenever any user's corpus is unreadable." `harvest.user_session_roots`
already did the local glob + readable check for exactly one caller — this
module pulls that logic out from under its one hardcoded call site into a
typed, N-source SEAM, so the next reader doesn't collapse it back into a single
`/Users/*/.claude/projects` glob duplicated across modules.

The human's requirement: "our skills should have N places they can look,
including cloud runtimes when we add those." So this is a typed list of
sources, one adapter per `kind` — NOT a hardcoded two-account glob. Only the
`local` adapter is implemented today (no speculative cloud code). A configured
source whose `kind` has no registered adapter comes back `readable=False` with
a non-empty `reason` — NEVER silently dropped. That is the forward-compat
property: someone configures a cloud runtime before its adapter exists and the
report degrades LOUD (half-blind) instead of quietly under-reporting.

The second adapter, `canopy-web`, is that cloud runtime. An agent claimed by the
cloud runner (`cloud-ec2-1`, Linux) writes its transcripts on THAT box, never into
any `/Users/*/.claude/projects` — so `agent-review echo` on the laptop read a
whole, readable corpus and attributed zero turns, and on the cloud box itself the
macOS glob found nothing at all. canopy-web already retains those transcripts
(a turn's raw JSONL at `/api/harness/turns/<id>/transcript` when `has_transcript`,
else the chat session the runner drove), so the adapter lists the agent's turns
in the window, fetches each transcript ONCE into a local cache under
`~/.claude/canopy/session-cache/canopy-web/`, and hands back plain JSONL paths —
the shape every local consumer (`friction_signals`, `agent_coverage`) already
reads. It is in the default source set whenever a canopy-web PAT resolves, and a
session that is ALSO on local disk is skipped (matched on the Claude session id),
so one turn is never reviewed twice. A listing or fetch that fails flips the
source to `readable=False` with the reason — half-blind, never silently partial.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from orchestrator import canopy_web
from orchestrator.paths import CANOPY_DIR

# Canopy already keeps state under ~/.claude/canopy/ (workbench-token, campaigns/,
# repo-map.json, ...) -- this config lives alongside it, not in a repo-local file.
DEFAULT_CONFIG_PATH = CANOPY_DIR / "session-sources.json"

# Fetched canopy-web transcripts, one dir per turn. A FINISHED turn's transcript
# never changes, so a cached copy is reused forever; a still-running turn's copy goes
# under `live/` and is refetched every time (it is still growing).
WEB_CACHE_DIR = CANOPY_DIR / "session-cache" / "canopy-web"

# Set to 0/false/no/off to keep canopy-web OUT of the auto-discovered default set
# (offline work, or a test suite that must never touch the network).
WEB_SOURCE_ENV = "CANOPY_SESSION_WEB"

WEB_KIND = "canopy-web"


@dataclass
class SessionSource:
    name: str        # "local:jjackson" / "canopy-web:labs.connect.dimagi.com"
    kind: str        # "local" | "canopy-web"
    location: str    # a path for local; the canopy-web base URL for canopy-web
    readable: bool
    reason: str = ""  # why unreadable, when it isn't


def _dir_listable(path: str) -> bool:
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


def discover_local_sources(users_root: str = "/Users",
                           home: Optional[Path] = None) -> list[SessionSource]:
    """Every macOS user's ~/.claude/projects on this box, with a readability flag.

    The local glob + readable check (moved here from harvest.user_session_roots,
    which is now a thin wrapper over this — see its docstring).

    When `users_root` holds no homes at all (a Linux box: the cloud runner has no
    `/Users`), fall back to the CURRENT user's own `~/.claude/projects` — otherwise
    the runner reads zero local sources and its own sessions vanish from the corpus.
    """
    out: list[SessionSource] = []
    for h in sorted(glob.glob(os.path.join(users_root, "*"))):
        p = os.path.join(h, ".claude", "projects")
        if not os.path.isdir(p):
            continue
        readable = _dir_listable(p)
        out.append(SessionSource(
            name=f"local:{os.path.basename(h)}", kind="local", location=p,
            readable=readable, reason="" if readable else "not readable",
        ))
    if not out:
        own = Path(home) if home is not None else Path.home()
        p = own / ".claude" / "projects"
        if p.is_dir():
            readable = _dir_listable(str(p))
            out.append(SessionSource(
                name=f"local:{own.name}", kind="local", location=str(p),
                readable=readable, reason="" if readable else "not readable",
            ))
    return out


def _web_source_name(base_url: str) -> str:
    return f"{WEB_KIND}:{urlparse(base_url).netloc or base_url}"


def _web_token_problem() -> str:
    """"" when a canopy-web PAT resolves, else why not (no network)."""
    try:
        canopy_web.resolve_token(None)
        return ""
    except RuntimeError as exc:
        return str(exc)


def _web_disabled_by_env() -> bool:
    return os.environ.get(WEB_SOURCE_ENV, "").strip().lower() in ("0", "false", "no", "off")


def discover_web_source() -> Optional[SessionSource]:
    """The canopy-web source for the default set — present only when a PAT resolves
    (and `CANOPY_SESSION_WEB` doesn't switch it off). No PAT is not an error here:
    a machine that has never minted one simply has no web corpus to offer."""
    if _web_disabled_by_env() or _web_token_problem():
        return None
    base = canopy_web.resolve_base_url(None)
    return SessionSource(name=_web_source_name(base), kind=WEB_KIND,
                         location=base, readable=True)


def _adapt_local(entry: dict) -> SessionSource:
    location = str(entry.get("location") or "")
    name = entry.get("name") or f"local:{location}"
    readable = os.path.isdir(location) and _dir_listable(location)
    return SessionSource(name=name, kind="local", location=location,
                         readable=readable, reason="" if readable else "not readable")


def _adapt_canopy_web(entry: dict) -> SessionSource:
    # Explicitly configured, so a missing PAT IS an error: say so (half-blind)
    # rather than dropping the source the operator asked for.
    base = canopy_web.resolve_base_url(entry.get("location") or None)
    name = entry.get("name") or _web_source_name(base)
    problem = _web_token_problem()
    return SessionSource(name=name, kind=WEB_KIND, location=base,
                         readable=not problem, reason=problem)


def _adapt_unknown(entry: dict) -> SessionSource:
    kind = str(entry.get("kind") or "")
    location = str(entry.get("location") or "")
    name = entry.get("name") or f"{kind}:{location}"
    # Degrade LOUD, never silent: an unrecognized kind still produces a source
    # row (so it shows up in corpus.sources / drives confidence to half-blind)
    # instead of being dropped, which would quietly under-report the corpus.
    return SessionSource(name=name, kind=kind, location=location, readable=False,
                         reason=f"no adapter for kind {kind!r}")


# One adapter per `kind`. Registering a future "cloud" adapter here is the
# whole extension point -- no caller of `session_sources()` needs to change.
_ADAPTERS: dict[str, Callable[[dict], SessionSource]] = {
    "local": _adapt_local,
    WEB_KIND: _adapt_canopy_web,
}


def _from_config_entry(entry: dict) -> SessionSource:
    adapter = _ADAPTERS.get(entry.get("kind"))
    return adapter(entry) if adapter else _adapt_unknown(entry)


def session_sources(config_path: Optional[Path] = None,
                    users_root: str = "/Users",
                    include_web: Optional[bool] = None) -> list[SessionSource]:
    """All configured sources, or auto-discovery when no config exists.

    Config lives at ~/.claude/canopy/session-sources.json (override via
    `config_path`, mainly for tests):
        {"sources": [{"name": "...", "kind": "local", "location": "/path"},
                     {"kind": "canopy-web"}, ...]}

    A configured source wins over auto-discovery entirely (it's an explicit
    statement of where to look); with no config file, auto-discover local
    sources plus canopy-web (when a PAT resolves) so this works out of the box
    on a fresh machine — including a cloud runner with no `/Users` at all.
    `include_web` forces the web source in (True; unreadable if no PAT) or out
    (False); None means auto.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return [_from_config_entry(e) for e in data.get("sources", [])]
    sources = discover_local_sources(users_root=users_root)
    if include_web is None:
        web = discover_web_source()
    elif include_web:
        web = _adapt_canopy_web({})
    else:
        web = None
    if web is not None:
        sources.append(web)
    return sources


def local_transcript_dirs(sources: list[SessionSource]) -> list[Path]:
    """The readable local sources' paths -- ready for
    `find_turn_transcripts(repo, hours, projects_dir=d)`."""
    return [Path(s.location) for s in sources if s.kind == "local" and s.readable]


def corpus_confidence(sources: list[SessionSource]) -> str:
    """`"whole-corpus"` if every source is readable, else `"half-blind"` --
    harvest's established convention for flagging its own blindness."""
    return "whole-corpus" if all(s.readable for s in sources) else "half-blind"


# --------------------------------------------------------------------------------
# canopy-web: fetch an agent's turn transcripts into the local cache
# --------------------------------------------------------------------------------

# `/api/agents/<slug>/turns/` is newest-first, takes only `limit` (no offset), and
# clamps it server-side. If a full page is still inside the window, older turns were
# cut off — that is reported (`truncated`), never silently absorbed.
WEB_LIST_LIMIT = 500
_TERMINAL = frozenset({"done", "failed", "cancelled"})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass
class WebCorpus:
    """What the canopy-web sources contributed: transcript paths (cached JSONL, the
    shape local consumers read), the ones belonging to still-running turns, and
    per-source accounting so a caller can say what it could NOT see."""
    transcripts: list[Path] = field(default_factory=list)
    running: set[str] = field(default_factory=set)
    stats: dict[str, dict] = field(default_factory=dict)


def _parse_iso(value) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)


def _turn_time(turn: dict) -> Optional[_dt.datetime]:
    """Last-known activity, the web analogue of a local transcript's mtime."""
    for k in ("ended_at", "finished_at", "started_at", "created_at"):
        ts = _parse_iso(turn.get(k))
        if ts:
            return ts
    return None


def _turn_session_ids(turn: dict) -> set[str]:
    """Every id this turn's Claude session could be filed under locally. A cloud
    runner stamps the Claude session id as `session_key`; a laptop's is an emdash
    task name, so only a UUID-shaped key counts."""
    ids = {str(turn.get("cli_session_id") or "").strip()}
    key = str(turn.get("session_key") or "").strip()
    if _UUID.match(key):
        ids.add(key)
    return ids - {""}


def _jsonl_session_id(lines: list[dict]) -> str:
    for e in lines:
        sid = e.get("sessionId") or e.get("session_id")
        if sid:
            return str(sid)
    return ""


def _session_messages_to_entries(detail: dict) -> list[dict]:
    """A canopy-web chat session's parsed `messages` back into Claude-JSONL entries
    (user / assistant text / tool_use / tool_result with `is_error`) — the subset the
    friction extractors read. Lossy by nature; the provenance entry says so."""
    out: list[dict] = []
    for m in detail.get("messages") or []:
        role, text = m.get("role"), m.get("plaintext") or ""
        content = m.get("content") or {}
        ts = m.get("created_at")
        if role == "user":
            out.append({"type": "user", "timestamp": ts,
                        "message": {"role": "user", "content": text}})
        elif role == "assistant":
            out.append({"type": "assistant", "timestamp": ts,
                        "message": {"role": "assistant",
                                    "content": [{"type": "text", "text": text}]}})
        elif role == "tool_use" and content.get("id"):
            out.append({"type": "assistant", "timestamp": ts,
                        "message": {"role": "assistant", "content": [{
                            "type": "tool_use", "id": content["id"],
                            "name": content.get("name", ""),
                            "input": content.get("input") or {}}]}})
        elif role == "tool_result" and content.get("tool_use_id"):
            out.append({"type": "user", "timestamp": ts,
                        "message": {"role": "user", "content": [{
                            "type": "tool_result",
                            "tool_use_id": content["tool_use_id"],
                            "content": text,
                            "is_error": content.get("is_error")}]}})
    return out


def _parse_jsonl(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _normalize(entries: list[dict], sid: str, fallback_ts: str) -> list[dict]:
    """`claude -p` stream-json says `session_id` and often omits `timestamp`; local
    transcripts say `sessionId` and always carry one. Fill both so session-keyed and
    time-keyed consumers (agent_coverage's bursts) see the web copy like a local one."""
    for e in entries:
        if sid and not e.get("sessionId"):
            e["sessionId"] = sid
        if fallback_ts and not e.get("timestamp"):
            e["timestamp"] = fallback_ts
    return entries


def _cached(cache_root: Path, turn_id: str) -> Optional[Path]:
    hits = sorted((cache_root / turn_id).glob("*.jsonl"))
    return hits[0] if hits else None


def _write_cache(dest_dir: Path, sid: str, entries: list[dict],
                 when: Optional[_dt.datetime]) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob("*.jsonl"):
        old.unlink()
    path = dest_dir / f"{sid}.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    tmp.replace(path)
    if when is not None:
        # mtime = when the turn last ran, so a finished turn never reads as a
        # session still being written (session_liveness keys off mtime).
        stamp = when.timestamp()
        os.utime(path, (stamp, stamp))
    return path


def _fetch_one_web_source(src: SessionSource, agent: str, hours: float, *,
                          local_session_ids: set[str], call, get_text,
                          cache_dir: Path, now: _dt.datetime,
                          out: WebCorpus) -> dict:
    st = {"turns_in_window": 0, "fetched": 0, "cached": 0, "already_local": 0,
          "no_transcript": [], "hidden": [], "errors": [], "truncated": False}
    base = src.location
    try:
        page = call("GET", f"/api/agents/{agent}/turns/?limit={WEB_LIST_LIMIT}",
                    base_url=base)
    except Exception as exc:  # noqa: BLE001 — any failure is a blind source, said aloud
        src.readable = False
        src.reason = f"could not list {agent}'s turns: {exc}"[:400]
        st["errors"].append(src.reason)
        return st
    items = (page.get("items") if isinstance(page, dict) else page) or []
    cutoff = now - _dt.timedelta(hours=hours)
    window = [t for t in items if (_turn_time(t) or cutoff) > cutoff]
    st["turns_in_window"] = len(window)
    st["truncated"] = len(items) >= WEB_LIST_LIMIT and len(window) == len(items)

    cache_root = cache_dir / agent
    seen_sids: set[str] = set()
    seen_chats: set[str] = set()
    for t in window:
        tid = str(t.get("id") or "")
        if not tid:
            continue
        ids = _turn_session_ids(t)
        if ids & local_session_ids:
            st["already_local"] += 1
            continue
        if t.get("content_hidden"):
            st["hidden"].append(tid)       # LOGS_READ denied: a real blind spot
            continue
        terminal = str(t.get("status") or "") in _TERMINAL
        when = _turn_time(t)
        hit = _cached(cache_root, tid) if terminal else None
        if hit is not None:
            if hit.stem in local_session_ids or hit.stem in seen_sids:
                st["already_local"] += 1
                continue
            seen_sids.add(hit.stem)
            out.transcripts.append(hit)
            st["cached"] += 1
            continue

        chat = str(t.get("chat_session_id") or "")
        try:
            if t.get("has_transcript"):
                entries = _parse_jsonl(get_text(
                    "GET", f"/api/harness/turns/{tid}/transcript", base_url=base))
                via = "turn-transcript"
            elif chat and chat not in seen_chats:
                seen_chats.add(chat)
                detail = call("GET", f"/api/canopy-sessions/{chat}?full=true",
                              base_url=base) or {}
                entries = _session_messages_to_entries(detail)
                via = "session-messages"
            else:
                st["no_transcript"].append(tid)
                continue
        except Exception as exc:  # noqa: BLE001
            st["errors"].append(f"turn {tid}: {exc}"[:300])
            continue
        if not entries:
            st["no_transcript"].append(tid)
            continue

        sid = _jsonl_session_id(entries) or next(iter(sorted(ids)), "") or tid
        if sid in local_session_ids or sid in seen_sids:
            st["already_local"] += 1
            continue
        seen_sids.add(sid)
        fallback_ts = (when.isoformat() if when else "")
        entries = _normalize(entries, sid, fallback_ts)
        entries.insert(0, {"type": "canopy-web-provenance", "turn_id": tid,
                           "via": via, "base_url": base, "status": t.get("status")})
        dest = cache_root / tid if terminal else cache_root / "live" / tid
        path = _write_cache(dest, sid, entries, when if terminal else None)
        out.transcripts.append(path)
        if not terminal:
            out.running.add(str(path))
        st["fetched"] += 1

    problems = []
    if st["errors"]:
        problems.append(f"{len(st['errors'])} transcript fetch(es) failed")
    if st["hidden"]:
        problems.append(f"{len(st['hidden'])} turn(s) hidden from this PAT (no LOGS_READ)")
    if st["truncated"]:
        problems.append(f"listing capped at {WEB_LIST_LIMIT} turns, all in window — "
                        f"older turns not seen")
    if problems:
        src.readable = False
        src.reason = "partial: " + "; ".join(problems)
    return st


def collect_web_transcripts(sources: list[SessionSource], agent: str, hours: float, *,
                            local_session_ids: set[str] = frozenset(),
                            call: Optional[Callable] = None,
                            get_text: Optional[Callable] = None,
                            cache_dir: Optional[Path] = None,
                            now: Optional[_dt.datetime] = None) -> WebCorpus:
    """Every readable `canopy-web` source's transcripts for `agent`'s turns in the
    last `hours`, cached locally and returned as JSONL paths.

    A turn whose Claude session id is in `local_session_ids` is skipped — the local
    copy already covers it. A failed listing, a failed fetch, a permission-hidden
    turn, or a capped listing flips that source to `readable=False` with the reason
    (so `corpus_confidence` reads half-blind). `call` / `get_text` default to the
    real canopy-web client and are injectable so tests never touch the network.
    """
    out = WebCorpus()
    call = call or canopy_web.call
    get_text = get_text or canopy_web.call_text
    cache_dir = Path(cache_dir) if cache_dir else WEB_CACHE_DIR
    now = now or _dt.datetime.now(_dt.timezone.utc)
    local_ids = set(local_session_ids)
    for src in sources:
        if src.kind != WEB_KIND or not src.readable:
            continue
        out.stats[src.name] = _fetch_one_web_source(
            src, agent, hours, local_session_ids=local_ids, call=call,
            get_text=get_text, cache_dir=cache_dir, now=now, out=out)
    return out
