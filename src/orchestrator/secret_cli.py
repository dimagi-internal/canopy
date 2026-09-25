"""`canopy secret` — use a secret a person shared with THIS chat, without reading it.

A person adds a secret from the chat's "Secrets…" menu in canopy-web (the
`SessionSecret` model). Nothing is posted into the chat; they just mention it by
name. The session running that chat then:

    canopy secret list                                   # what this chat holds
    canopy secret exec --stdin GH_TOKEN -- \\
        gh secret set GH_TOKEN --repo dimagi-internal/canopy          # on stdin
    canopy secret exec GH_TOKEN -- \\
        sh -c 'op item edit gh-token "credential=$GH_TOKEN"'          # as $GH_TOKEN

The env-var form needs the `sh -c '…'` with SINGLE quotes: `"$GH_TOKEN"` typed
straight into the calling shell is expanded there, where it is unset, before
this command ever runs.

**Only this session.** The session names itself by `CLAUDE_CODE_SESSION_ID`,
which the canopy runner reports as the chat binding's transcript id, and
canopy-web releases a secret only to the conversation bound to the chat that
holds it. There is deliberately no flag to name another session or chat.

The value goes to exactly one child process and never to this process's output:
the child's stdout and stderr are relayed with every occurrence of the value
replaced by `***`. That is the point — the model driving the terminal reads the
command's output, so an unmasked `echo` or an error that quotes its input would
put the secret straight into its context. There is no verb that prints a value.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from typing import IO, Optional

import click

from orchestrator import canopy_web

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MASK = "***"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


def this_session() -> str:
    sid = os.environ.get(SESSION_ENV, "").strip()
    if not sid:
        raise click.ClickException(
            f"${SESSION_ENV} is not set — `canopy secret` only works inside the Claude Code "
            f"session a chat is bound to, and uses that session's own secrets."
        )
    return sid


def _get(path: str, *, call=None):
    call = call or canopy_web.call
    try:
        return call("GET", path)
    except canopy_web.CanopyError as exc:
        raise click.ClickException(
            f"{exc}. A 404 means this session is not bound to a chat you can act for, "
            f"or the secret does not exist or has expired (secrets live 30 minutes)."
        ) from None


def list_secrets(session_id: str, *, call=None) -> list[dict]:
    return _get(f"/api/session-secrets/{session_id}", call=call) or []


def fetch_value(session_id: str, name: str, *, call=None) -> str:
    """The plaintext. Errors name the secret and never carry a value."""
    body = _get(f"/api/session-secrets/{session_id}/{name}", call=call)
    value = (body or {}).get("value") or ""
    if not value:
        raise click.ClickException(f"{name} came back empty")
    return value


def _relay(src: IO[bytes], dst: IO[bytes], secret: bytes) -> None:
    """Copy line by line, masking the secret. Per line, not per chunk, so a
    value can never straddle two reads and slip through half-masked."""
    for line in iter(src.readline, b""):
        dst.write(line.replace(secret, MASK.encode()))
        dst.flush()
    src.close()


def run_masked(argv: list[str], value: str, *, env_name: Optional[str], stdin: bool) -> int:
    env = dict(os.environ)
    if env_name:
        env[env_name] = value
    proc = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    secret = value.encode()
    threads = [
        threading.Thread(target=_relay, args=(proc.stdout, sys.stdout.buffer, secret), daemon=True),
        threading.Thread(target=_relay, args=(proc.stderr, sys.stderr.buffer, secret), daemon=True),
    ]
    for t in threads:
        t.start()
    if stdin:
        try:
            proc.stdin.write(secret)
            proc.stdin.close()
        except BrokenPipeError:
            pass
    code = proc.wait()
    for t in threads:
        t.join()
    return code


@click.group("secret")
def secret_group() -> None:
    """Use secrets shared with this chat, without reading them."""


@secret_group.command("list")
def list_cmd() -> None:
    """The secrets shared with the chat this session is bound to (names only)."""
    rows = list_secrets(this_session())
    if not rows:
        click.echo("No secrets shared with this chat.")
        return
    for r in rows:
        used = "used" if r.get("last_used_at") else "not used yet"
        click.echo(f"{r['name']}\t{used}\texpires {r.get('expires_at', '?')}")


@secret_group.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("--env", "env_name", default=None,
              help="Env var to set in the command (default: the secret's NAME).")
@click.option("--stdin", "use_stdin", is_flag=True,
              help="Pipe the value to the command's stdin instead of setting an env var.")
@click.argument("name")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def exec_cmd(env_name: Optional[str], use_stdin: bool, name: str, command: tuple[str, ...]) -> None:
    """Run COMMAND with the secret NAME from this chat, masking it out of the output.

    Put `--` before COMMAND. Reference the value as "$NAME" inside a
    `sh -c '…'` if the command needs it as an argument — single quotes, so YOUR
    shell does not expand it.
    """
    if not NAME_RE.match(name):
        raise click.BadParameter("a secret name like GH_TOKEN (see `canopy secret list`)",
                                 param_hint="NAME")
    # With interspersed args off, click stops option parsing at NAME and hands
    # the `--` through as the first word of COMMAND.
    argv = list(command[1:] if command and command[0] == "--" else command)
    if not argv:
        raise click.UsageError("no command after --")
    value = fetch_value(this_session(), name)
    target = None if use_stdin else (env_name or name)
    sys.exit(run_masked(argv, value, env_name=target, stdin=use_stdin))
