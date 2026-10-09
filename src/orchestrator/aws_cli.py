"""`canopy aws login` — get an AWS SSO session approved from the owner's phone.

An agent turn that hits an expired AWS SSO session used to stop and ask a person
to run `aws sso login --profile labs` in a terminal on the box. This runs the
real CLI in its device-code form instead:

    canopy aws login --profile labs

`aws sso login --use-device-code --no-browser` prints a URL with the code
already filled in and then waits. This command sends that URL to canopy-web
(`POST /api/harness/runners/<id>/sign-in-request`), which pushes it to the
runner owner's phone. The owner taps it, approves on the AWS page, and the CLI
here finishes by itself. This command then confirms the session with
`aws sts get-caller-identity`.

It speaks AS THE RUNNER, using `~/.canopy/runner.json`, and not as the agent.
That is the only identity canopy-web lets put an approval in front of the
runner's owner. An approved device code grants AWS access to whoever started
it, so being able to send one must already mean being the owner.

When no device was reached (push not enabled, no runner on this box, canopy-web
unreachable) it prints the link so the caller can deliver it another way. It
still waits, because the link works from any browser.

It blocks for up to `--timeout` seconds (default 600, AWS's own code lifetime).
From an agent's Bash tool, run it with a 10-minute timeout or in the background.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

import click

from orchestrator import canopy_web

RUNNER_CONFIG_ENV = "CANOPY_RUNNER_CONFIG"
DEFAULT_RUNNER_CONFIG = Path.home() / ".canopy" / "runner.json"
# The line `aws sso login --use-device-code` prints with the code already in it.
_DEVICE_URL = re.compile(r"(https://\S*[?&]user_code=[A-Za-z0-9-]+)")


def parse_device_url(line: str) -> str:
    m = _DEVICE_URL.search(line or "")
    return m.group(1) if m else ""


def load_runner_config(path: Optional[Path] = None) -> Optional[dict]:
    """`{base_url, token, runner_id}` from the runner's own config, or None.

    `token` may be `@<file>`, which is how canopy-runner stores it
    (canopy_runner/config.py).
    """
    path = path or Path(os.environ.get(RUNNER_CONFIG_ENV) or DEFAULT_RUNNER_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        token = str(raw["token"])
        if token.startswith("@"):
            token = Path(token[1:]).expanduser().read_text(encoding="utf-8").strip()
        return {"base_url": str(raw["base_url"]).rstrip("/"), "token": token,
                "runner_id": str(raw["runner_id"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def push_sign_in_request(cfg: dict, *, url: str, label: str, requested_by: str,
                         call: Callable = canopy_web.call) -> int:
    """Devices reached. Raises CanopyError when canopy-web refused or was unreachable."""
    out = call("POST", f"/api/harness/runners/{cfg['runner_id']}/sign-in-request",
               {"provider": "aws", "url": url, "label": label, "requested_by": requested_by},
               base_url=cfg["base_url"], token=cfg["token"])
    return int(out.get("sent", 0))


def caller_identity(profile: str) -> str:
    """The signed-in ARN, or "" when the profile has no live session."""
    try:
        r = subprocess.run(["aws", "sts", "get-caller-identity", "--profile", profile,
                            "--output", "json"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode != 0:
        return ""
    try:
        return str(json.loads(r.stdout).get("Arn", ""))
    except ValueError:
        return ""


def _read_device_url(proc: subprocess.Popen, found: threading.Event, box: list) -> None:
    """Drain the CLI's output (so it can never block on a full pipe), keeping the
    first device URL it prints."""
    assert proc.stdout is not None
    for line in proc.stdout:
        if not box:
            url = parse_device_url(line)
            if url:
                box.append(url)
                found.set()
    found.set()  # the CLI exited without printing one: stop waiting for it


@click.group("aws")
def aws_group():
    """AWS sessions an agent can get approved without a terminal."""


@aws_group.command("login")
@click.option("--profile", default="labs", show_default=True, help="AWS profile to sign in.")
@click.option("--requested-by", default="",
              help="Who is waiting on it, shown in the notification. Default: this agent's slug.")
@click.option("--timeout", default=600, show_default=True, type=int,
              help="Seconds to wait for the approval.")
@click.option("--force", is_flag=True, help="Sign in again even if the session is live.")
def login_cmd(profile: str, requested_by: str, timeout: int, force: bool):
    """Push an AWS SSO approval to the runner owner's phone and wait for it."""
    if not force:
        arn = caller_identity(profile)
        if arn:
            click.echo(f"aws/{profile}: already signed in as {arn}")
            return

    proc = subprocess.Popen(
        ["aws", "sso", "login", "--profile", profile, "--use-device-code", "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    found, box = threading.Event(), []
    threading.Thread(target=_read_device_url, args=(proc, found, box), daemon=True).start()
    found.wait(timeout=60)
    if not box:
        proc.kill()
        raise click.ClickException(
            "`aws sso login --use-device-code` printed no approval URL. Is this profile "
            "an IAM Identity Center (SSO) profile, and is the AWS CLI v2.22 or newer?")
    url = box[0]

    who = requested_by or canopy_web.agent_context_slug() or "An agent"
    cfg = load_runner_config()
    sent, why = 0, ""
    if cfg is None:
        why = "no runner config on this box"
    else:
        try:
            sent = push_sign_in_request(cfg, url=url, label=profile, requested_by=who)
        except canopy_web.CanopyError as exc:
            why = str(exc)
    if sent:
        click.echo(f"aws/{profile}: approval pushed to the runner owner's phone "
                   f"({sent} device{'s' if sent != 1 else ''}). Waiting up to {timeout}s…")
    else:
        click.echo(f"aws/{profile}: could not push the approval ({why or 'no device has notifications on'}).\n"
                   f"Deliver this link to the person who signs in. It works from any browser:\n  {url}\n"
                   f"Waiting up to {timeout}s…")

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise click.ClickException(f"aws/{profile}: not approved within {timeout}s. The link has expired.")
    arn = caller_identity(profile) if rc == 0 else ""
    if not arn:
        raise click.ClickException(f"aws/{profile}: sign-in did not complete (aws exited {rc}).")
    click.echo(f"aws/{profile}: signed in as {arn}")
