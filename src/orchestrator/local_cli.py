"""`canopy private-runner` / `canopy local` — use an agent in your OWN Claude Code, on the record.

What a person runs is `canopy agent use <slug>` (agent_use.py), which does all of local use
in one go; this module holds the pieces it calls and the hook-facing `canopy local` verbs.

canopy-web#1704. Three objects:

* **this computer** — registered once for local use with YOUR canopy token (canopy-web
  calls the record a "private runner"; `register_this_computer`, run by `canopy agent use`).
  It belongs to you and serves every agent you use.
  When the machine also runs the runner app, the two are linked and the runner works
  only for you (canopy-web#1705); otherwise it never claims a turn. Recorded in
  ``~/.canopy/private-runner.json``.
* a **local turn** — one Claude Code session here using one agent. The canopy
  plugin's SessionStart hook opens it (`canopy local open`), the prompt hook
  renews it and shows its caller envelope, the transcript streams up as the session
  goes (`canopy local sync`, after every reply and at most every minute between tool
  calls — so a live session can be debugged from canopy-web, not only after it ends),
  and the SessionEnd hook uploads the rest and finishes it (`canopy local close`).
  State per session lives in ``~/.canopy/local/<claude session id>.json``; the envelope beside the runner's,
  in ``~/.canopy/caller/<turn id>.json``.
* a **license** (person × agent) — canopy-web issues it on first use when the
  agent's caller list gives you the whole agent; credential reads go against it.

Which agent a session uses is ``$CANOPY_AGENT`` — set by every agent repo's
``.claude/settings.json``, and by `canopy agent use <slug>` for any other project.

Every call here acts as YOU (the PAT `/canopy:canopy-web-pat-mint` wrote), never as
the agent: an agent's own login is refused, because a local turn's initiator is the
person whose session it is.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import time
from pathlib import Path

import click

from orchestrator import canopy_web

#: Upload a transcript in batches this big (the route caps one request's lines).
TRANSCRIPT_BATCH_BYTES = 512 * 1024
#: The prompt hook renews the turn's lease at most this often.
RENEW_EVERY = 600
#: Set to "0" to stop streaming a live session's transcript; it then goes up only when
#: the session ends (`canopy local close`), as before.
LIVE_SYNC_ENV = "CANOPY_LOCAL_LIVE_TRANSCRIPT"
#: A sync lock older than this belongs to a sync that died; the next one takes it over.
SYNC_LOCK_STALE = 300


def _canopy_dir() -> Path:
    return Path.home() / ".canopy"


def private_runner_file() -> Path:
    return _canopy_dir() / "private-runner.json"


def local_dir() -> Path:
    return _canopy_dir() / "local"


def caller_dir() -> Path:
    return _canopy_dir() / "caller"


def _write_private(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def read_private_runner() -> dict:
    """This machine's registration, or {} when it is not a private runner."""
    doc = _read(private_runner_file())
    return doc if doc.get("id") else {}


def _session_file(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_.")[:100]
    if not safe:
        raise click.ClickException("a Claude session id is required")
    return local_dir() / f"{safe}.json"


def read_session(session_id: str) -> dict:
    try:
        return _read(_session_file(session_id))
    except click.ClickException:
        return {}


def person_token(slug: str = "") -> str:
    """YOUR canopy-web PAT — never an agent's own (canopy-web refuses an agent's login
    as a local turn's person, and acting as the agent is exactly what this replaces)."""
    from orchestrator.agent_bootstrap import _operator_token, is_agents_own_token

    try:
        tok = _operator_token()
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"no canopy-web token for you on this machine ({e}); "
                                   "run /canopy:canopy-web-pat-mint") from None
    if not tok:
        raise click.ClickException("no canopy-web token for you on this machine; "
                                   "run /canopy:canopy-web-pat-mint")
    if slug and is_agents_own_token(slug, tok):
        raise click.ClickException(
            f"the only canopy-web token here is {slug}'s own; a local turn is YOURS — "
            "run /canopy:canopy-web-pat-mint as yourself")
    return tok


def session_token(slug: str = "") -> str:
    """The token a local session's own calls use: on your linked private runner the box's
    key-bought runner token (no PAT needed, canopy-web#1705), else your PAT."""
    from orchestrator import private_runner_session

    return private_runner_session.token() or person_token(slug)


# ──────────────────────────────────────────────────────────────────────────────
# canopy private-runner
# ──────────────────────────────────────────────────────────────────────────────

@click.group("private-runner")
def private_runner_group():
    """This computer: its registration for local use, and whether its runner app is private.

    To use an agent here, run `canopy agent use <slug>` — it registers this computer too."""


def register_this_computer(name: str | None = None, *, link_runner: bool = True) -> dict:
    """Register this computer for local use (idempotent) and record it in
    ``~/.canopy/private-runner.json``. Returns the recorded doc. When the runner app runs
    here too (``~/.canopy/runner.json``), the two are linked (canopy-web#1705)."""
    from orchestrator.runner_pin import _machine_runner_id

    host = socket.gethostname()
    name = (name or host.split(".")[0] or "laptop")[:100]
    runner_id = _machine_runner_id() if link_runner else ""
    payload = {"name": name, "host": host, **({"runner": runner_id} if runner_id else {})}
    body = canopy_web.call("POST", "/api/private-runners/", payload, token=person_token())
    doc = {"id": body["id"], "name": body["name"], "host": body.get("host", host),
           "runner_id": str(body.get("runner_id") or ""),
           "base_url": canopy_web.resolve_base_url(None), "registered_at": time.time()}
    _write_private(private_runner_file(), doc)
    return doc


@private_runner_group.command("register", hidden=True)
@click.option("--name", default=None, help="What to call this computer (default: its short hostname).")
@click.option("--no-runner", is_flag=True,
              help="Do not link the runner app on this computer (~/.canopy/runner.json), if any.")
@click.option("--json", "as_json", is_flag=True)
def private_runner_register(name, no_runner, as_json):
    """Register this computer for local use (`canopy agent use <slug>` does this for you).

    Idempotent. It lets a Claude Code session here that uses an agent be recorded as a
    local turn — yours, with the agent's envelope, rails and (when its caller list allows
    you) its credentials. When this computer also runs the runner app, the two are linked
    (canopy-web#1705): sessions here prove themselves with the runner's device key, and
    `canopy private-runner set private|shared` says which it is."""
    doc = register_this_computer(name, link_runner=not no_runner)
    if as_json:
        click.echo(json.dumps(doc))
    else:
        click.echo(f"this computer is registered for local use as '{doc['name']}' ({doc['id']}). "
                   "Sessions here that use an agent are now recorded as local turns — "
                   "`canopy agent use <slug>` sets the rest up.")


@private_runner_group.command("set")
@click.argument("mode", type=click.Choice(["private", "shared"]))
@click.option("--json", "as_json", is_flag=True)
def private_runner_set(mode, as_json):
    """Make this machine's runner your PRIVATE runner, or a SHARED one again.

    \b
    private  no fleet work — only what you pin or bind to it, and the local sessions here
    shared   back to a shared fleet runner (routed work again)

    To run as a lower role, start the TURN that way (`canopy agent dispatch --as
    editor`), or put a client in that mode with CANOPY_ACTS_AS=editor|viewer — a
    property of a turn or a process, never a setting left on a machine (canopy-web#1705)."""
    doc = read_private_runner()
    rid = doc.get("runner_id") or ""
    if not rid:
        from orchestrator.runner_pin import _machine_runner_id

        rid = _machine_runner_id()
    if not rid:
        raise click.ClickException("this computer has no runner app (~/.canopy/runner.json) — pair "
                                   "the runner app first, then `canopy agent use <slug>`")
    private = mode == "private"
    body = canopy_web.call("PUT", f"/api/harness/runners/{rid}/private", {"private": private},
                           token=person_token())
    out = {"runner_id": rid, "name": body.get("name", ""), "private": body.get("private", private)}
    if as_json:
        click.echo(json.dumps(out))
    elif not out["private"]:
        click.echo(f"runner '{out['name']}' is a shared fleet runner again (routed work)")
    else:
        click.echo(f"runner '{out['name']}' is now your private runner. "
                   "Start a NEW Claude Code session for local sessions to pick it up.")


@private_runner_group.command("show")
@click.option("--json", "as_json", is_flag=True)
def private_runner_show(as_json):
    """This machine's registration. Exit 1 when it is not a private runner."""
    doc = read_private_runner()
    if not doc:
        raise click.ClickException("this computer is not registered for local use — "
                                   "`canopy agent use <slug>`")
    click.echo(json.dumps(doc) if as_json else
               f"this computer is registered for local use as '{doc['name']}' ({doc['id']})")


# ──────────────────────────────────────────────────────────────────────────────
# canopy local
# ──────────────────────────────────────────────────────────────────────────────

@click.group("local")
def local_group():
    """A Claude Code session on this computer, recorded as a local turn (hook-facing).

    To set up an agent here, run `canopy agent use <slug>`."""


def set_session_agent(slug: str, directory: str = ".") -> bool:
    """Make sessions in `directory` use agent `slug`: ``$CANOPY_AGENT`` in its
    ``.claude/settings.local.json`` (personal, not committed). True when it changed."""
    path = Path(directory) / ".claude" / "settings.local.json"
    doc = _read(path)
    env = doc.get("env") if isinstance(doc.get("env"), dict) else {}
    if env.get("CANOPY_AGENT") == slug and path.is_file():
        return False
    env["CANOPY_AGENT"] = slug
    doc["env"] = env
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return True


@local_group.command("use", context_settings={"ignore_unknown_options": False})
@click.argument("slug")
@click.option("--dir", "directory", default=".", type=click.Path(file_okay=False),
              help="The project whose sessions use the agent (default: here).")
@click.pass_context
def local_use(ctx, slug, directory):
    """Same as `canopy agent use SLUG` (kept as an alias)."""
    from orchestrator.agent_cli import agent_use

    ctx.invoke(agent_use, slug=slug, directory=directory)


@local_group.command("open")
@click.option("--slug", envvar="CANOPY_AGENT", required=True)
@click.option("--session-id", required=True, help="The Claude Code session id.")
@click.option("--cwd", default="")
@click.option("--transcript", "transcript", default="",
              help="The session's transcript JSONL (kept, so a missed close can be caught up).")
@click.option("--auto", "want_auto", is_flag=True,
              help="Ask for auto mode (honoured only for an admin of the agent).")
def local_open(slug, session_id, cwd, transcript, want_auto):
    """Open (or re-open) this session's local turn. Prints JSON:
    {turn_id, turn_mode, turn_mode_basis, url, envelope_path, created}.

    First catches up any EARLIER session on this machine whose turn never closed (its
    SessionEnd never ran: a killed terminal, a crash) and that has been silent past the
    turn's lease — its transcript is uploaded and the turn finished, as its own close
    would have."""
    if not read_private_runner():
        raise click.ClickException(f"this computer is not registered for local use — "
                                   f"`canopy agent use {slug}`")
    path = _session_file(session_id)
    catch_up(exclude=session_id)
    state = open_turn(slug, session_id, cwd=cwd, transcript=transcript,
                      requested_mode="auto" if want_auto else "",
                      env_file=os.environ.get("CLAUDE_ENV_FILE", "").strip())
    created = state.pop("created")
    _write_private(path, state)
    click.echo(json.dumps({**state, "created": created}))


def open_turn(slug: str, session_id: str, *, cwd: str = "", transcript: str = "",
              requested_mode: str = "", env_file: str = "") -> dict:
    """Open (or re-open) a session's local turn on canopy-web and write its envelope.
    Returns the session state to record, with `created`. Everything a later re-open
    needs is kept in it: the cwd, the mode asked for, and the session's env file (the
    `$CLAUDE_ENV_FILE` the SessionStart hook exports the turn id into)."""
    ws = read_private_runner()
    if not ws:
        raise click.ClickException(f"this computer is not registered for local use — "
                                   f"`canopy agent use {slug}`")
    body = canopy_web.call("POST", "/api/harness/local-turns/", {
        "agent": slug, "private_runner": ws["id"], "claude_session_id": session_id,
        "cwd": cwd[:300], "turn_mode": requested_mode,
    }, token=session_token(slug))
    turn_id = str(body["turn_id"])
    env_path = caller_dir() / f"{turn_id}.json"
    envelope = body.get("envelope") or {}
    if isinstance(envelope, dict):
        envelope.setdefault("turn_id", turn_id)
    _write_private(env_path, envelope)
    state = {"turn_id": turn_id, "slug": slug, "session_id": session_id,
             "private_runner": ws["name"], "turn_mode": body.get("turn_mode", ""),
             "turn_mode_basis": body.get("turn_mode_basis", ""), "url": body.get("url", ""),
             "envelope_path": str(env_path), "opened_at": time.time(), "renewed_at": time.time(),
             "cwd": cwd[:300], "created": bool(body.get("created"))}
    if requested_mode:
        state["requested_mode"] = requested_mode
    if transcript:
        state["transcript"] = transcript
    if env_file:
        state["env_file"] = env_file
    return state


@local_group.command("renew")
@click.option("--session-id", required=True)
@click.option("--force", is_flag=True, help="Renew now, whatever the throttle says.")
def local_renew(session_id, force):
    """Keep this session's local turn alive (at most every 10 minutes unless --force).

    When canopy-web says the turn is no longer running — swept `lost` after the session
    sat idle past its 8-hour lease, or finished — the session would otherwise go on
    naming a dead turn (credential reads refused, close a no-op). So a NEW local turn is
    opened for the same session, its id appended to the session's env file (sourced
    before every later command) and recorded in the session state, whose next prompt
    shows the new envelope. When that cannot be done, one line on stderr says so."""
    state = read_session(session_id)
    if not state or state.get("closed_at"):
        return
    if not force and time.time() - float(state.get("renewed_at") or 0) < RENEW_EVERY:
        return
    slug = state.get("slug", "")
    try:
        body = canopy_web.call("POST", f"/api/harness/local-turns/{state['turn_id']}/renew",
                               token=session_token(slug)) or {}
        status = str(body.get("status") or "running") if isinstance(body, dict) else "running"
    except canopy_web.CanopyError as e:
        if "-> 404:" not in str(e):
            raise
        status = "gone"   # not this person's local turn any more (or never was)
    if status != "running":
        state = reopen(state, status)
        if not state:
            return
    else:
        # Re-read: a `sync` may have recorded a later upload offset meanwhile.
        state = read_session(session_id) or state
    state["renewed_at"] = time.time()
    _write_private(_session_file(session_id), state)


def live_sync_enabled() -> bool:
    return os.environ.get(LIVE_SYNC_ENV, "").strip() != "0"


@local_group.command("sync")
@click.option("--session-id", required=True)
@click.option("--transcript", "transcript", default="", help="The session's transcript JSONL.")
def local_sync(session_id, transcript):
    """Append what this live session's transcript gained since the last upload to its
    local turn (hook-facing; the Stop and PostToolUse hooks run it detached)."""
    click.echo(sync_session(session_id, transcript=transcript))


def sync_session(session_id: str, *, transcript: str = "") -> str:
    """Upload the complete lines a live session's transcript gained since the last
    upload, and record how far it got. Returns one line saying what happened.

    Only whole lines go up (the last one may still be being written); `close` sends the
    rest. One sync at a time per session: canopy-web dedups only a retry of the batch
    just before, so two syncs racing from the same offset would append twice."""
    if not live_sync_enabled():
        return f"live transcript off ({LIVE_SYNC_ENV}=0)"
    state = read_session(session_id)
    if not state or state.get("closed_at") or not state.get("turn_id"):
        return "no live local turn for this session"
    transcript = transcript or str(state.get("transcript") or "")
    if not transcript or not Path(transcript).is_file():
        return "no transcript to sync"
    lock = _session_file(session_id).with_suffix(".sync.lock")
    if not _take_lock(lock):
        return "another sync is running"
    try:
        turn_id = state["turn_id"]
        start = int(state.get("uploaded") or 0)

        def record(sent: int) -> None:
            # After EVERY batch, so a sync that fails halfway does not resend what went up.
            # Re-read: a renew may have written the state (or re-opened the turn) meanwhile.
            fresh = read_session(session_id)
            if fresh.get("turn_id") != turn_id or fresh.get("closed_at"):
                return
            fresh.update(uploaded=sent, synced_at=time.time())
            fresh.setdefault("transcript", transcript)
            _write_private(_session_file(session_id), fresh)

        sent = upload_transcript(turn_id, Path(transcript), session_token(state.get("slug", "")),
                                 start=start, complete_only=True, on_batch=record)
        record(sent)
        return f"local turn {turn_id}: {sent - start} new lines ({sent} in all)"
    finally:
        lock.unlink(missing_ok=True)


def _take_lock(lock: Path, *, wait: float = 0) -> bool:
    """Create `lock` exclusively, taking over one left by a sync that died; with `wait`,
    retry for up to that many seconds."""
    deadline = time.time() + wait
    while True:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > SYNC_LOCK_STALE:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def reopen(state: dict, status: str) -> dict:
    """Open a new local turn for a session whose turn ended (`status`) and point the
    session at it. Returns the new state, or {} after a stderr warning."""
    old, sid, slug = state.get("turn_id", ""), state.get("session_id", ""), state.get("slug", "")
    try:
        new = open_turn(slug, sid, cwd=str(state.get("cwd") or ""),
                        transcript=str(state.get("transcript") or ""),
                        requested_mode=str(state.get("requested_mode") or ""),
                        env_file=str(state.get("env_file") or ""))
    except Exception as e:  # noqa: BLE001 — a renew never fails the session
        click.echo(f"canopy: local turn {old} of {slug} is {status} and a new one could not be "
                   f"opened ({str(e).splitlines()[0][:200] if str(e) else type(e).__name__}) — "
                   "this session's credential reads will fail; start a new session", err=True)
        return {}
    new.pop("created", None)
    new["envelope_shown"] = False   # the next prompt shows the new turn's envelope
    new["previous_turn_ids"] = [*state.get("previous_turn_ids", []), old]
    env_file = new.get("env_file") or ""
    if env_file:
        try:
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write(f"export CANOPY_TURN_ID={shlex.quote(new['turn_id'])}\n")
        except OSError as e:
            click.echo(f"canopy: re-opened local turn {new['turn_id']} but could not export it "
                       f"to the session env ({e}) — later commands still name {old}", err=True)
    else:
        click.echo(f"canopy: re-opened local turn {new['turn_id']} (the old one, {old}, is "
                   f"{status}); this session has no env file, so later commands still name {old}",
                   err=True)
    return new


def _batches(lines: list[str]):
    batch, size = [], 0
    for line in lines:
        n = len(line.encode("utf-8"))
        if batch and size + n > TRANSCRIPT_BATCH_BYTES:
            yield batch
            batch, size = [], 0
        batch.append(line)
        size += n
    if batch:
        yield batch


def upload_transcript(turn_id: str, transcript: Path, token: str, *, start: int = 0,
                      complete_only: bool = False, on_batch=None) -> int:
    """Append the transcript's lines from `start` to the turn. Returns the line count
    sent up to. Each batch carries an id, so a retried batch is not appended twice.
    `complete_only` leaves out a last line with no newline yet (still being written);
    `on_batch(sent)` is told after each batch lands."""
    text = transcript.read_text(encoding="utf-8", errors="replace")
    if complete_only:
        text = text[:text.rfind("\n") + 1]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    sent = start
    for i, batch in enumerate(_batches(lines[start:])):
        canopy_web.call("POST", f"/api/harness/turns/{turn_id}/transcript",
                        {"lines": batch, "batch_id": f"local-{turn_id}-{sent}-{i}"}, token=token)
        sent += len(batch)
        if on_batch:
            on_batch(sent)
    return sent


@local_group.command("close")
@click.option("--session-id", required=True)
@click.option("--transcript", "transcript", default="", help="The session's transcript JSONL.")
@click.option("--status", "status", type=click.Choice(["done", "failed"]), default="done")
def local_close(session_id, transcript, status):
    """Upload this session's transcript to its local turn, then finish the turn."""
    click.echo(close_session(session_id, transcript=transcript, status=status))


#: A session whose state was not renewed for this long is dead (the turn's own lease
#: on canopy-web is 8 hours): `catch_up` closes it.
STALE_AFTER = 8 * 3600


def catch_up(*, exclude: str = "", now: float | None = None) -> list[str]:
    """Close every earlier session on this machine that never closed and has been
    silent past STALE_AFTER. Best-effort: a failure is reported and skipped.

    Such a session died without its SessionEnd (a killed terminal, a crash), so its
    turn is finished `failed`, not `done`: nothing says its work ended well. (By now
    canopy-web has usually swept it `lost` already, and the finish is a no-op there.)"""
    now = time.time() if now is None else now
    done = []
    for f in sorted(local_dir().glob("*.json")) if local_dir().is_dir() else []:
        state = _read(f)
        sid = state.get("session_id") or f.stem
        if sid == exclude or not state.get("turn_id") or state.get("closed_at"):
            continue
        if now - float(state.get("renewed_at") or state.get("opened_at") or 0) < STALE_AFTER:
            continue
        try:
            done.append(close_session(sid, status="failed"))
        except Exception as e:  # noqa: BLE001 — never block opening the new session
            click.echo(f"could not catch up local session {sid}: {str(e).splitlines()[0][:200]}", err=True)
    return done


def close_session(session_id: str, *, transcript: str = "", status: str = "done") -> str:
    """Upload a session's transcript (the one named, else the one recorded at open)
    to its local turn, then finish the turn. Returns one line saying what happened."""
    state = read_session(session_id)
    if not state:
        return "no local turn for this session"
    lock = _session_file(session_id).with_suffix(".sync.lock")
    locked = _take_lock(lock, wait=30)   # let a sync in flight finish, then read its offset
    try:
        return _close(session_id, read_session(session_id) or state, transcript, status)
    finally:
        if locked:
            lock.unlink(missing_ok=True)


def _close(session_id: str, state: dict, transcript: str, status: str) -> str:
    transcript = transcript or str(state.get("transcript") or "")
    tok = session_token(state.get("slug", ""))
    turn_id = state["turn_id"]
    note = ""
    if transcript and Path(transcript).is_file():
        sent = upload_transcript(turn_id, Path(transcript), tok, start=int(state.get("uploaded") or 0))
        state["uploaded"] = sent
        note = f"transcript: {sent} lines"
    canopy_web.call("POST", f"/api/harness/turns/{turn_id}/finish",
                    {"status": status, "result_note": f"local session ended ({note or 'no transcript'})",
                     "session_key": session_id}, token=tok)
    state["closed_at"] = time.time()
    _write_private(_session_file(session_id), state)
    return (f"local turn {turn_id} {status}" + (f" — {note}" if note else "") +
            (f" — {state['url']}" if state.get("url") else ""))
