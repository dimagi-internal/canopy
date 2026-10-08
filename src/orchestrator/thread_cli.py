"""`canopy thread …` — agent threads: bounded, moderated, direct conversations between agents.

canopy-web stores the thread (`/api/threads/`) and enforces its limits — budget, deadline, turn
order, who may speak — on the turn-create path, so nothing here is trusted to. Each message is a
one-shot harness turn tagged `origin_ref.kind = thread_message`; canopy-web derives the messages
back out of those tags. This CLI only opens a thread, sends a message, runs the moderator loop,
and shows the transcript. The pure half (prompt, block, decision) is `thread.py`.

    open  → POST /api/threads/ (idempotent: an open thread with the same parent and
            participants comes back as-is)                                 → thread JSON
    say   → render message n's prompt and dispatch it to one participant
    run   → the MODERATOR loop, foreground. Stateless and resumable: every step is derived from
            GET /api/threads/<id>. Exit 0 = closed, 3 = still waiting (run it again),
            2 = refusal.
    show  → a readable transcript

How a participant answers: `plugins/canopy/agent-core/thread.md`. Contract:
`docs/architecture/agent-threads.md`.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import click

from orchestrator import canopy_web
from orchestrator import thread as T
from orchestrator.agent_client import CanopyError, _rows

THREADS_PATH = "/api/threads/"

# Seams for tests.
_sleep = time.sleep


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ThreadError(click.ClickException):
    """A refusal with a reason; exit 2 (a usage problem, not a crash)."""
    exit_code = 2


def _emit(obj) -> None:
    click.echo(json.dumps(obj, indent=2, default=str))


def _call(method: str, path: str, body=None):
    return canopy_web.call(method, path, body)


def _http_status(err: Exception) -> int:
    m = re.search(r"-> (\d{3})", str(err))
    return int(m.group(1)) if m else 0


def get_thread(tid: str) -> dict:
    try:
        return _call("GET", f"{THREADS_PATH}{quote(tid, safe='')}")
    except CanopyError as e:
        raise ThreadError(f"cannot read thread {tid!r} from canopy-web: {e}")


def list_threads(**filters) -> list[dict]:
    """GET /api/threads/?parent_key=…&parent_value=…&agent=…&status=… (newest first)."""
    q = urlencode({k: v for k, v in filters.items() if v})
    return _rows(_call("GET", THREADS_PATH + (f"?{q}" if q else "")))


def open_thread(*, kind: str, purpose: str, participants: list[dict], moderator: str,
                parent: dict | None = None, context: str = "", max_messages: int | None = None,
                deadline_minutes: int | None = None) -> dict:
    body = {"kind": kind, "purpose": purpose, "participants": participants,
            "moderator": moderator, "parent": parent or {}, "context": context or ""}
    if max_messages is not None:
        body["max_messages"] = int(max_messages)
    if deadline_minutes is not None:
        body["deadline_minutes"] = int(deadline_minutes)
    return _call("POST", THREADS_PATH, body)


def close_thread(tid: str, status: str, outcome: dict) -> dict:
    return _call("POST", f"{THREADS_PATH}{quote(tid, safe='')}/close",
                 {"status": status, "outcome": outcome})


def _is_mode_refusal(err: Exception) -> bool:
    msg = str(err).lower()
    return ("-> 403" in msg or "-> 422" in msg) and "mode" in msg


def send_message(thread: dict, to: str, n: int, mode: str, now: dt.datetime) -> dict:
    """Dispatch message n to `to` through the fleet's one dispatch path — tagged, never pinned,
    one isolated session per thread, stamped with who sent it. Re-sending the same n dedupes
    (idempotency key `thread-<id>-n<n>`)."""
    from orchestrator.agent_dispatch import TURNS_PATH, build_turn_payload
    tid = thread["id"]
    text = T.render_message_prompt(thread, to, n, now)
    payload = build_turn_payload(to, prompt=text, idempotency_key=T.idempotency_key(tid, n),
                                 sender=thread.get("moderator") or None,
                                 turn_mode=None if mode == "none" else mode)
    payload["origin_ref"].update(T.message_origin_ref(tid, n, to))
    fallback = False
    try:
        turn = _call("POST", TURNS_PATH, payload)
    except CanopyError as e:
        if "turn_mode" not in payload or not _is_mode_refusal(e):
            raise
        click.echo(f"[thread] canopy-web refused turn_mode={payload['turn_mode']} for {to} "
                   f"({str(e)[:160]}); re-sending without a mode — its own rules decide.",
                   err=True)
        payload.pop("turn_mode")
        fallback = True
        turn = _call("POST", TURNS_PATH, payload)
    return {"thread": tid, "n": n, "to": to, "turn_id": turn.get("id"),
            "status": turn.get("status"), "idempotency_key": T.idempotency_key(tid, n),
            "mode_fallback": fallback}


# ── commands ─────────────────────────────────────────────────────────────────────
@click.group("thread")
def thread_group():
    """Bounded, moderated, direct conversations between fleet agents (agent-core/thread.md)."""


def _participant(raw: str) -> dict:
    slug, _, role = raw.partition(":")
    if not slug.strip():
        raise ThreadError(f"--participant {raw!r}: write it slug:role, e.g. eva:author")
    return {"agent": slug.strip(), "role": role.strip() or "participant"}


@thread_group.command("open")
@click.option("--kind", required=True, help="e.g. agreement")
@click.option("--purpose", required=True, help="One line: what the thread is for.")
@click.option("--participant", "participants", multiple=True, required=True,
              help="slug:role, repeatable (2..6, distinct). Agreement: author first, then asker.")
@click.option("--moderator", default="", help="Moderating agent (default: this repo's agent).")
@click.option("--parent", "parents", multiple=True, help="k=v, repeatable (e.g. huddle=<id>).")
@click.option("--context-file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Opening material every message prompt quotes verbatim.")
@click.option("--max-messages", default=T.DEFAULT_MAX_MESSAGES, show_default=True, type=int)
@click.option("--deadline-minutes", default=T.DEFAULT_DEADLINE_MINUTES, show_default=True,
              type=int)
def open_cmd(kind, purpose, participants, moderator, parents, context_file, max_messages,
             deadline_minutes):
    """Open a thread (or get back the open one with the same parent and participants)."""
    from orchestrator.agent_dispatch import local_agent_slug
    moderator = moderator or local_agent_slug()
    if not moderator:
        raise ThreadError("no --moderator and this is not an agent repo")
    people = [_participant(p) for p in participants]
    slugs = [p["agent"] for p in people]
    if not 2 <= len(people) <= 6 or len(set(slugs)) != len(slugs):
        raise ThreadError("a thread needs 2..6 distinct participants")
    parent = {}
    for kv in parents:
        k, sep, v = kv.partition("=")
        if not sep or not k.strip():
            raise ThreadError(f"--parent {kv!r}: write it key=value")
        parent[k.strip()] = v.strip()
    context = Path(context_file).read_text(encoding="utf-8") if context_file else ""
    try:
        th = open_thread(kind=kind, purpose=purpose, participants=people, moderator=moderator,
                         parent=parent, context=context, max_messages=max_messages,
                         deadline_minutes=deadline_minutes)
    except CanopyError as e:
        raise ThreadError(f"canopy-web refused the thread: {e}")
    _emit(th)


@thread_group.command("say")
@click.option("--thread", "tid", required=True)
@click.option("--to", "to", required=True, help="The participant who speaks next.")
@click.option("--mode", type=click.Choice(["auto", "manual", "none"]), default="auto",
              show_default=True, help="Requested turn mode; `none` lets canopy-web decide.")
@click.option("--prompt-out", default=None, type=click.Path(dir_okay=False),
              help="Also write the rendered prompt here.")
def say_cmd(tid, to, mode, prompt_out):
    """Send the next message of a thread to one participant (`run` does this for you)."""
    thread = get_thread(tid)
    now = _now()
    if (thread.get("status") or T.OPEN) != T.OPEN:
        raise ThreadError(f"thread {tid} is {thread.get('status')}")
    if to not in T.agents_of(thread):
        raise ThreadError(f"{to} is not a participant of {tid} "
                          f"({', '.join(T.agents_of(thread))})")
    msgs = T.messages_of(thread)
    if msgs and not T.settled_state(T.message_state(msgs[-1], thread, now)[0]):
        raise ThreadError(f"message {msgs[-1].get('n')} ({msgs[-1].get('speaker')}) is still "
                          f"out — wait for it (`canopy thread run --thread {tid}`)")
    n = T.messages_used(thread) + 1
    if prompt_out:
        Path(prompt_out).write_text(T.render_message_prompt(thread, to, n, now), encoding="utf-8")
    try:
        _emit(send_message(thread, to, n, mode, now))
    except CanopyError as e:
        raise ThreadError(f"message {n} to {to} refused: {e}")


@thread_group.command("run")
@click.option("--thread", "tid", required=True)
@click.option("--budget-seconds", default=540, show_default=True, type=int,
              help="How long this call works before exiting 3 (run it again).")
@click.option("--poll", default=30, show_default=True, type=int)
@click.option("--mode", type=click.Choice(["auto", "manual", "none"]), default="auto",
              show_default=True, help="Requested turn mode for each message.")
def run_cmd(tid, budget_seconds, poll, mode):
    """MODERATE a thread in the FOREGROUND: send each next message, wait for it, close the
    thread when it settles. Exit 0 = closed (prints the outcome), 3 = still waiting on a
    message — run the SAME command again, 2 = refusal. Resumable from anywhere."""
    started = time.monotonic()
    refused_at = None
    while True:
        thread = get_thread(tid)
        now = _now()
        step = T.decide(thread, now)
        act = step["action"]
        if act == "closed":
            _emit(_summary(thread))
            return
        if act == "close":
            try:
                thread = close_thread(tid, step["status"], step["outcome"])
            except CanopyError as e:
                if _http_status(e) != 409:
                    raise ThreadError(f"could not close {tid}: {e}")
                thread = get_thread(tid)       # closed under us — report what it is
            _emit(_summary(thread))
            return
        if act == "send":
            try:
                sent = send_message(thread, step["to"], step["n"], mode, now)
            except CanopyError as e:
                # The server's guard said no (deadline / budget / order). Re-read once — the
                # next decision closes it — but never loop on the same refusal.
                if _http_status(e) in (409, 422) and refused_at != step["n"]:
                    refused_at = step["n"]
                    click.echo(f"[thread] message {step['n']} refused ({str(e)[:200]}); "
                               "re-reading the thread", err=True)
                    continue
                raise ThreadError(f"message {step['n']} to {step['to']} refused: {e}")
            click.echo(f"[thread] {tid}: sent message {sent['n']} to {sent['to']} "
                       f"(turn {sent['turn_id']})", err=True)
        else:   # wait
            click.echo(f"[thread] {tid}: waiting on message {step['n']} from {step['speaker']} "
                       f"({step['state']})", err=True)
        if time.monotonic() - started >= budget_seconds:
            out = _summary(get_thread(tid))
            out["next"] = f"canopy thread run --thread {tid}"
            _emit(out)
            sys.exit(3)
        _sleep(max(1, poll))


def _summary(thread: dict) -> dict:
    now = _now()
    msgs = []
    for m in T.messages_of(thread):
        st, probs = T.message_state(m, thread, now)
        b = m.get("block") if isinstance(m.get("block"), dict) else {}
        msgs.append({"n": m.get("n"), "speaker": m.get("speaker"), "state": st,
                     "position": T.norm_position(b.get("position")) if b else None,
                     **({"problems": probs} if probs else {})})
    return {"thread": thread.get("id"), "kind": thread.get("kind"),
            "status": thread.get("status"), "result": T.result_of(thread),
            "outcome": thread.get("outcome") or {}, "messages_used": T.messages_used(thread),
            "max_messages": thread.get("max_messages"), "messages": msgs}


def show_text(thread: dict, now: dt.datetime) -> str:
    head = (f"{thread.get('id')} — {thread.get('kind')} · {thread.get('status')} · "
            f"{T.messages_used(thread)}/{thread.get('max_messages')} messages · moderated by "
            f"{thread.get('moderator')}")
    lines = [head, f"Purpose: {thread.get('purpose')}",
             "Participants: " + ", ".join(f"{p.get('agent')} ({p.get('role')})"
                                          for p in thread.get("participants") or [])]
    if thread.get("status") != T.OPEN:
        out = thread.get("outcome") or {}
        lines.append(f"Outcome: {T.result_of(thread)} — {T.why_of(thread)}")
        if out.get("proposal"):
            lines.append("Adopted proposal:\n" + json.dumps(out["proposal"], indent=2))
    lines += ["", T.transcript_text(thread, now)]
    return "\n".join(lines)


@thread_group.command("show")
@click.option("--thread", "tid", required=True)
@click.option("--json", "as_json", is_flag=True, help="The raw thread record.")
def show_cmd(tid, as_json):
    """A readable transcript of the thread."""
    thread = get_thread(tid)
    if as_json:
        _emit(thread)
    else:
        click.echo(show_text(thread, _now()))
