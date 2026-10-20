"""`canopy private-runner` / `canopy local` — use an agent in your OWN Claude Code, on the record.

canopy-web#1704. Three objects:

* a **private runner** — this machine, registered once with YOUR canopy token
  (`canopy private-runner register`). It belongs to you, serves every agent you use,
  and never claims a turn. Recorded in ``~/.canopy/private-runner.json``.
* a **local turn** — one Claude Code session here using one agent. The canopy
  plugin's SessionStart hook opens it (`canopy local open`), the prompt hook
  renews it and shows its caller envelope, and the SessionEnd hook uploads the
  transcript and finishes it (`canopy local close`). State per session lives in
  ``~/.canopy/local/<claude session id>.json``; the envelope beside the runner's,
  in ``~/.canopy/caller/<turn id>.json``.
* a **license** (person × agent) — canopy-web issues it on first use when the
  agent's caller list gives you the whole agent; credential reads go against it.

Which agent a session uses is ``$CANOPY_AGENT`` — set by every agent repo's
``.claude/settings.json``, and by `canopy local use <slug>` for any other project.

Every call here acts as YOU (the PAT `/canopy:canopy-web-pat-mint` wrote), never as
the agent: an agent's own login is refused, because a local turn's initiator is the
person whose session it is.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import click

from orchestrator import canopy_web

#: Upload a transcript in batches this big (the route caps one request's lines).
TRANSCRIPT_BATCH_BYTES = 512 * 1024
#: The prompt hook renews the turn's lease at most this often.
RENEW_EVERY = 600


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


# ──────────────────────────────────────────────────────────────────────────────
# canopy private-runner
# ──────────────────────────────────────────────────────────────────────────────

@click.group("private-runner")
def private_runner_group():
    """This machine as one of YOUR private runners (use agents locally, on the record)."""


@private_runner_group.command("register")
@click.option("--name", default=None, help="What to call this machine (default: its short hostname).")
@click.option("--no-runner", is_flag=True,
              help="Do not link the runner app on this machine (~/.canopy/runner.json), if any.")
@click.option("--json", "as_json", is_flag=True)
def private_runner_register(name, no_runner, as_json):
    """Register this machine as your private runner. Idempotent: re-running it is safe.

    It lets a Claude Code session here that uses an agent be recorded as a local turn —
    yours, with the agent's envelope, rails and (when its caller list allows you) its
    credentials. When this machine also runs the runner app, the two are linked into one
    private runner (canopy-web#1705): sessions here prove themselves with the runner's
    device key, and `canopy private-runner act-as` sets the role they are judged as."""
    from orchestrator.runner_pin import _machine_runner_id

    host = socket.gethostname()
    name = (name or host.split(".")[0] or "laptop")[:100]
    runner_id = "" if no_runner else _machine_runner_id()
    payload = {"name": name, "host": host, **({"runner": runner_id} if runner_id else {})}
    body = canopy_web.call("POST", "/api/private-runners/", payload, token=person_token())
    doc = {"id": body["id"], "name": body["name"], "host": body.get("host", host),
           "runner_id": str(body.get("runner_id") or ""),
           "base_url": canopy_web.resolve_base_url(None), "registered_at": time.time()}
    _write_private(private_runner_file(), doc)
    if as_json:
        click.echo(json.dumps(doc))
    else:
        click.echo(f"private runner '{doc['name']}' registered ({doc['id']}). Sessions here that use an "
                   "agent ($CANOPY_AGENT, or `canopy local use <slug>`) are now recorded as local turns.")


ACT_AS = ("self", "editor", "viewer")


@private_runner_group.command("act-as")
@click.argument("role", type=click.Choice(ACT_AS + ("shared",)))
@click.option("--json", "as_json", is_flag=True)
def private_runner_act_as(role, as_json):
    """Make this machine's runner your PRIVATE runner, judged as ROLE.

    \b
    self    private, and you act as yourself (owner/admin where you are one)
    editor  private, and everything from this box is judged as a workspace editor:
            licenses, manual-only turns, credential reads under your license
    viewer  private, judged as a viewer (most agents refuse a local turn)
    shared  back to a shared fleet runner (routed work again; no role cap)

    A private runner takes no fleet work — only what you pin or bind to it, and the
    local sessions here. The cap only ever lowers what you may do (canopy-web#1705)."""
    doc = read_private_runner()
    rid = doc.get("runner_id") or ""
    if not rid:
        from orchestrator.runner_pin import _machine_runner_id

        rid = _machine_runner_id()
    if not rid:
        raise click.ClickException("this machine has no runner (~/.canopy/runner.json) — pair the "
                                   "runner app first, then `canopy private-runner register`")
    private = role != "shared"
    acts_as = role if role in ("editor", "viewer") else ""
    body = canopy_web.call("PUT", f"/api/harness/runners/{rid}/private",
                           {"private": private, "acts_as": acts_as}, token=person_token())
    out = {"runner_id": rid, "name": body.get("name", ""), "private": body.get("private", private),
           "acts_as": body.get("acts_as", acts_as)}
    if as_json:
        click.echo(json.dumps(out))
    elif not out["private"]:
        click.echo(f"runner '{out['name']}' is a shared fleet runner again (routed work; no role cap)")
    else:
        who = f"acting as {out['acts_as']}" if out["acts_as"] else "acting as yourself"
        click.echo(f"runner '{out['name']}' is now your private runner, {who}. "
                   "Start a NEW Claude Code session for local sessions to pick it up.")


@private_runner_group.command("show")
@click.option("--json", "as_json", is_flag=True)
def private_runner_show(as_json):
    """This machine's registration. Exit 1 when it is not a private runner."""
    doc = read_private_runner()
    if not doc:
        raise click.ClickException("this machine is not a private runner — `canopy private-runner register`")
    click.echo(json.dumps(doc) if as_json else f"private runner '{doc['name']}' ({doc['id']})")


# ──────────────────────────────────────────────────────────────────────────────
# canopy local
# ──────────────────────────────────────────────────────────────────────────────

@click.group("local")
def local_group():
    """A Claude Code session on your private runner, recorded as a local turn (hook-facing)."""


@local_group.command("use")
@click.argument("slug")
@click.option("--dir", "directory", default=".", type=click.Path(file_okay=False),
              help="The project whose sessions use the agent (default: here).")
def local_use(slug, directory):
    """Make sessions in this project use agent SLUG: sets $CANOPY_AGENT in
    .claude/settings.local.json (personal, not committed)."""
    path = Path(directory) / ".claude" / "settings.local.json"
    doc = _read(path)
    env = doc.get("env") if isinstance(doc.get("env"), dict) else {}
    env["CANOPY_AGENT"] = slug
    doc["env"] = env
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    click.echo(f"sessions in {Path(directory).resolve()} now use {slug} — start a new Claude Code session")


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
    ws = read_private_runner()
    if not ws:
        raise click.ClickException("this machine is not a private runner — `canopy private-runner register`")
    path = _session_file(session_id)
    catch_up(exclude=session_id)
    body = canopy_web.call("POST", "/api/harness/local-turns/", {
        "agent": slug, "private_runner": ws["id"], "claude_session_id": session_id,
        "cwd": cwd[:300], "turn_mode": "auto" if want_auto else "",
    }, token=person_token(slug))
    turn_id = str(body["turn_id"])
    env_path = caller_dir() / f"{turn_id}.json"
    envelope = body.get("envelope") or {}
    if isinstance(envelope, dict):
        envelope.setdefault("turn_id", turn_id)
    _write_private(env_path, envelope)
    state = {"turn_id": turn_id, "slug": slug, "session_id": session_id,
             "private_runner": ws["name"], "turn_mode": body.get("turn_mode", ""),
             "turn_mode_basis": body.get("turn_mode_basis", ""), "url": body.get("url", ""),
             "envelope_path": str(env_path), "opened_at": time.time(), "renewed_at": time.time()}
    if transcript:
        state["transcript"] = transcript
    _write_private(path, state)
    click.echo(json.dumps({**state, "created": bool(body.get("created"))}))


@local_group.command("renew")
@click.option("--session-id", required=True)
@click.option("--force", is_flag=True, help="Renew now, whatever the throttle says.")
def local_renew(session_id, force):
    """Keep this session's local turn alive (at most every 10 minutes unless --force)."""
    state = read_session(session_id)
    if not state:
        return
    if not force and time.time() - float(state.get("renewed_at") or 0) < RENEW_EVERY:
        return
    canopy_web.call("POST", f"/api/harness/local-turns/{state['turn_id']}/renew",
                    token=person_token(state.get("slug", "")))
    state["renewed_at"] = time.time()
    _write_private(_session_file(session_id), state)


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


def upload_transcript(turn_id: str, transcript: Path, token: str, *, start: int = 0) -> int:
    """Append the transcript's lines from `start` to the turn. Returns the line count
    sent up to. Each batch carries an id, so a retried batch is not appended twice."""
    lines = [ln for ln in transcript.read_text(encoding="utf-8", errors="replace").splitlines()
             if ln.strip()]
    sent = start
    for i, batch in enumerate(_batches(lines[start:])):
        canopy_web.call("POST", f"/api/harness/turns/{turn_id}/transcript",
                        {"lines": batch, "batch_id": f"local-{turn_id}-{sent}-{i}"}, token=token)
        sent += len(batch)
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
    silent past STALE_AFTER. Best-effort: a failure is reported and skipped."""
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
            done.append(close_session(sid, status="done"))
        except Exception as e:  # noqa: BLE001 — never block opening the new session
            click.echo(f"could not catch up local session {sid}: {str(e).splitlines()[0][:200]}", err=True)
    return done


def close_session(session_id: str, *, transcript: str = "", status: str = "done") -> str:
    """Upload a session's transcript (the one named, else the one recorded at open)
    to its local turn, then finish the turn. Returns one line saying what happened."""
    state = read_session(session_id)
    if not state:
        return "no local turn for this session"
    transcript = transcript or str(state.get("transcript") or "")
    tok = person_token(state.get("slug", ""))
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
