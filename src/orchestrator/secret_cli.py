"""`canopy secret` — spend a secret a person shared with a chat, without reading it.

A person hands a chat a secret through canopy-web's "Share a secret…" (the
`SessionSecret` model). The chat receives only a reference:

    canopy-secret://<session-id>/<NAME>

and the agent spends it here:

    canopy secret exec --stdin canopy-secret://<session>/GH_TOKEN -- \\
        gh secret set GH_TOKEN --repo dimagi-internal/canopy                  # on stdin
    canopy secret exec canopy-secret://<session>/GH_TOKEN -- \\
        sh -c 'op item edit gh-token "credential=$GH_TOKEN"'                  # as $GH_TOKEN

The env-var form needs the `sh -c '…'` with SINGLE quotes: `"$GH_TOKEN"` typed
straight into the calling shell is expanded there, where it is unset, before
this command ever runs.

The value goes to exactly one child process and never to this process's output:
the child's stdout and stderr are relayed with every occurrence of the value
replaced by `***`. That is the property the feature exists for — the model
driving the terminal reads the command's output, so an unmasked `echo` or an
error that quotes its input would put the secret straight into its context.

There is deliberately no `get` / `print` verb. A way to show the value is a
way for it to end up in a transcript.
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

REF_RE = re.compile(r"^canopy-secret://([0-9a-fA-F-]{36})/([A-Z][A-Z0-9_]{0,63})$")
MASK = "***"


def parse_ref(ref: str) -> tuple[str, str]:
    m = REF_RE.match((ref or "").strip())
    if not m:
        raise click.BadParameter(
            "expected canopy-secret://<session-id>/<NAME> (copy it from the chat message)",
            param_hint="REF",
        )
    return m.group(1), m.group(2)


def fetch_value(session_id: str, name: str, *, call=None) -> str:
    """The plaintext. Errors say what failed and never carry the value."""
    call = call or canopy_web.call
    try:
        body = call("GET", f"/api/canopy-sessions/{session_id}/secrets/{name}/value")
    except canopy_web.CanopyError as exc:
        raise click.ClickException(
            f"could not fetch {name}: {exc}. A 404 means no such secret, or this identity "
            f"is neither a writer of that chat nor its agent."
        ) from None
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
    """Spend a secret shared with a chat by reference, without reading it."""


@secret_group.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("--env", "env_name", default=None,
              help="Env var to set in the command (default: the secret's NAME).")
@click.option("--stdin", "use_stdin", is_flag=True,
              help="Pipe the value to the command's stdin instead of setting an env var.")
@click.argument("ref")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def exec_cmd(env_name: Optional[str], use_stdin: bool, ref: str, command: tuple[str, ...]) -> None:
    """Run COMMAND with the secret at REF, masking it out of the output.

    REF is the `canopy-secret://<session>/<NAME>` from the chat. Put `--` before
    COMMAND. Reference the value as "$NAME" inside a `sh -c '…'` if the command
    needs it as an argument — single quotes, so YOUR shell does not expand it.
    """
    session_id, name = parse_ref(ref)
    # With interspersed args off, click stops option parsing at REF and hands
    # the `--` through as the first word of COMMAND.
    argv = list(command[1:] if command and command[0] == "--" else command)
    if not argv:
        raise click.UsageError("no command after --")
    value = fetch_value(session_id, name)
    target = None if use_stdin else (env_name or name)
    sys.exit(run_masked(argv, value, env_name=target, stdin=use_stdin))
