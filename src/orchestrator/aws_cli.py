"""`canopy aws login` — get an AWS SSO session approved from the owner's phone.

An agent turn that hits an expired AWS SSO session used to stop and ask a person
to run `aws sso login --profile labs` in a terminal on the box. This runs the
real CLI in its device-code form instead:

    canopy aws login --profile labs --reason "confirm the 5xx cause in /ecs/labs-jj-canopy-web"

`--reason` is required. The alert reads "<Agent> needs AWS sign-in (labs)" /
"On <runner>: <reason>", so the owner can decide from the lock screen without
opening the session to find out what the approval is for.

`aws sso login --use-device-code --no-browser` prints a URL with the code
already filled in and then waits. This command sends that URL to canopy-web
(`POST /api/harness/runners/<id>/sign-in-request`), which pushes it to the
runner owner's phone and lists it on their supervisor feed, for a phone that
never shows the push. The owner taps it, approves on the AWS page, and the CLI
here finishes by itself. This command then confirms the session with
`aws sts get-caller-identity` and reports the outcome back
(`.../sign-in-requests/<id>/result`), which takes it off the feed.

It speaks AS THE RUNNER, and not as the agent. That is the only identity
canopy-web lets put an approval in front of the runner's owner. An approved
device code grants AWS access to whoever started it, so being able to send one
must already mean being the owner.

"As the runner" means the credential the live runner itself signs in with: the
hour-long runner token its approved device key buys (`runner_session`, cached in
`~/.canopy/runner-session.json`). Only a box with no device key falls back to
the legacy `token` in `~/.canopy/runner.json`. A paired laptop keeps that field
EMPTY, and an empty token used to fall through to whatever PAT the session held
(the agent's), which owns no runner: canopy-web answered 404 "runner not found"
for a runner that was online (jj-mbp-cdp, 2026-10-29).

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
    (canopy_runner/config.py), or "" on a box that signs in with its device key.
    """
    path = path or Path(os.environ.get(RUNNER_CONFIG_ENV) or DEFAULT_RUNNER_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        token = str(raw.get("token") or "").strip()
        if token.startswith("@"):
            token = Path(token[1:]).expanduser().read_text(encoding="utf-8").strip()
        return {"base_url": str(raw["base_url"]).rstrip("/"), "token": token,
                "runner_id": str(raw["runner_id"])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def runner_credential(config: Optional[dict] = None, *,
                      session_token: Optional[Callable[[str], str]] = None,
                      key_runner_id: Optional[Callable[[], str]] = None,
                      ) -> tuple[Optional[dict], str]:
    """`({base_url, token, runner_id, source}, "")`, or `(None, why)`.

    The token is the one the live runner uses: its device key's runner token
    (`source` "runner-session"). Only when the box has none does the legacy
    runner.json token stand in (`source` "runner.json"). Never an empty token,
    which `canopy_web.call` would replace with the session's own PAT.
    """
    if session_token is None:
        from orchestrator import runner_session

        session_token = runner_session.token
    if key_runner_id is None:
        def key_runner_id() -> str:
            from orchestrator import device_key

            return str(device_key.read_status().get("runner") or "").strip()

    cfg = config if config is not None else load_runner_config()
    runner_id = (cfg or {}).get("runner_id") or key_runner_id()
    if not runner_id:
        return None, ("this box is no canopy runner (no ~/.canopy/runner.json and no "
                      "device key registered for a runner)")
    base_url = (cfg or {}).get("base_url") or canopy_web.resolve_base_url(None)
    try:
        tok = session_token(base_url)
    except Exception:  # noqa: BLE001 — runner_session.token never raises; a stub might
        tok = ""
    if tok:
        return {"base_url": base_url, "token": tok, "runner_id": runner_id,
                "source": "runner-session"}, ""
    legacy = (cfg or {}).get("token") or ""
    if legacy:
        return {"base_url": base_url, "token": legacy, "runner_id": runner_id,
                "source": "runner.json"}, ""
    return None, (f"no runner credential for runner {runner_id}: its device key bought no "
                  "runner token (is the key approved? `canopy vault device --refresh` shows its status) "
                  "and ~/.canopy/runner.json has no legacy token")


def explain_push_error(exc: Exception, cfg: dict) -> str:
    """What to do about a refused push, in one line. A 404 on the runner's own
    route means the credential is not the runner owner's, not that the runner
    is gone."""
    text = str(exc)
    if "-> 404" in text and "runner not found" in text:
        return (f"canopy-web answered 404 \"runner not found\" for runner {cfg['runner_id']} "
                f"using the {cfg.get('source', 'runner')} credential: that token does not "
                f"belong to the runner's owner on {cfg['base_url']}. Check the runner is not "
                "retired, then check its device key (`canopy vault device --refresh`; on a "
                "box without one, re-pair the runner)")
    if "-> 401" in text:
        return (f"canopy-web refused the {cfg.get('source', 'runner')} credential (401): "
                f"{text[:200]}. Check the device key with `canopy vault device --refresh`")
    return text


def push_sign_in_request(cfg: dict, *, url: str, label: str, requested_by: str, reason: str,
                         call: Callable = canopy_web.call) -> tuple[int, str]:
    """(devices reached, the stored request's id). The id is "" from a
    canopy-web that stores nothing. Raises CanopyError when canopy-web refused
    or was unreachable."""
    if not cfg.get("token"):
        # canopy_web.call reads "" as "no token given" and sends the session's own
        # PAT, which owns no runner: a 404 that looks like the runner is gone.
        raise canopy_web.CanopyError("no runner token to sign the request with")
    out = call("POST", f"/api/harness/runners/{cfg['runner_id']}/sign-in-request",
               {"provider": "aws", "url": url, "label": label, "requested_by": requested_by,
                "reason": reason},
               base_url=cfg["base_url"], token=cfg["token"])
    return int(out.get("sent", 0)), str(out.get("id") or "")


def report_sign_in_result(cfg: dict, request_id: str, outcome: str,
                          call: Optional[Callable] = None) -> None:
    """Tell canopy-web how the sign-in ended ("approved", "failed", "expired"),
    so the owner's feed stops offering the link. Best effort: a report that
    does not land leaves a row that expires with its code anyway."""
    if not request_id:
        return
    if cfg.get("source") == "runner-session" and call is None:
        # The login waits up to 10 minutes: report with a token that is fresh now.
        cfg = runner_credential()[0] or cfg
    call = call or canopy_web.call
    try:
        call("POST", f"/api/harness/runners/{cfg['runner_id']}/sign-in-requests/{request_id}/result",
             {"outcome": outcome}, base_url=cfg["base_url"], token=cfg["token"])
    except canopy_web.CanopyError as exc:
        click.echo(f"(could not report the sign-in outcome to canopy-web: {exc})", err=True)


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
@click.option("--reason", required=True,
              help="Why AWS is needed, in one specific line. Shown on the owner's lock screen.")
@click.option("--requested-by", default="",
              help="Who is waiting on it, shown in the notification. Default: this agent's name.")
@click.option("--timeout", default=600, show_default=True, type=int,
              help="Seconds to wait for the approval.")
@click.option("--force", is_flag=True, help="Sign in again even if the session is live.")
def login_cmd(profile: str, reason: str, requested_by: str, timeout: int, force: bool):
    """Push an AWS SSO approval to the runner owner's phone and wait for it."""
    reason = " ".join(reason.split())
    if not reason:
        raise click.UsageError("--reason must say what AWS is needed for; the owner reads it to decide.")
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

    who = requested_by or canopy_web.agent_context_slug().capitalize() or "An agent"
    cfg, why = runner_credential()
    sent, request_id = 0, ""
    if cfg is not None:
        try:
            sent, request_id = push_sign_in_request(cfg, url=url, label=profile, requested_by=who,
                                                    reason=reason)
        except canopy_web.CanopyError as exc:
            if "-> 401" in str(exc) and cfg.get("source") == "runner-session":
                from orchestrator import runner_session

                runner_session.forget()  # refused early: the next call buys a fresh one
            why = explain_push_error(exc, cfg)
    if sent:
        feed = " and listed on their supervisor feed" if request_id else ""
        click.echo(f"aws/{profile}: approval pushed to the runner owner's phone "
                   f"({sent} device{'s' if sent != 1 else ''}){feed}. Waiting up to {timeout}s…")
    else:
        feed = "; it is on the owner's supervisor feed" if request_id else ""
        click.echo(f"aws/{profile}: could not push the approval ({why or 'no device has notifications on'}){feed}.\n"
                   f"Deliver this link to the person who signs in. It works from any browser:\n  {url}\n"
                   f"Waiting up to {timeout}s…")

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        report_sign_in_result(cfg, request_id, "expired")
        raise click.ClickException(f"aws/{profile}: not approved within {timeout}s. The link has expired.")
    arn = caller_identity(profile) if rc == 0 else ""
    if not arn:
        report_sign_in_result(cfg, request_id, "failed")
        raise click.ClickException(f"aws/{profile}: sign-in did not complete (aws exited {rc}).")
    report_sign_in_result(cfg, request_id, "approved")
    click.echo(f"aws/{profile}: signed in as {arn}")


# ──────────────────────────────────────────────────────────────────────────────
# `canopy aws credentials` — one hour of a NAMED read-only role, with no SSO
# ──────────────────────────────────────────────────────────────────────────────
#
# For the AWS CLI's `credential_process`, never for a person or a transcript:
#
#     [profile labs-observe]
#     credential_process = canopy aws credentials --role labs-observe
#     region = us-east-1
#
# `labs-observe` is connect-labs' `labs-jj-observe-readonly`: CloudWatch metrics,
# alarms and resource metadata, with explicit denies on log events, object bodies,
# secrets, task commands/env and database content. Two sources, first that answers:
#
# 1. a cloud runner's root-owned broker (canopy-web runner/ec2/aws_broker.py,
#    `/run/canopy-aws/sock`): the instance role assumes the role for this runner's
#    uid — works even when canopy-web is down;
# 2. canopy-web, `POST /api/agents/<slug>/aws-credentials`, as this agent's own
#    login (canopy-web's task role assumes the role).
#
# Either way the session is stamped with the agent, so CloudTrail says who made each
# call. Keys are cached (0600) until 5 minutes before they expire, because the AWS CLI
# runs credential_process on every invocation.

AWS_BROKER_SOCKET_ENV = "CANOPY_AWS_BROKER_SOCKET"
DEFAULT_AWS_BROKER_SOCKET = "/run/canopy-aws/sock"
AWS_CREDENTIALS_CACHE = Path.home() / ".canopy" / "aws-credentials"
CREDENTIALS_MIN_LIFE = 300
#: The roles a profile can be written for, and the region each lives in.
KNOWN_ROLES = {"labs-observe": "us-east-1"}
_PROCESS_KEYS = ("Version", "AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")


def _expires_at(expiration: str) -> float:
    import calendar
    import time

    try:
        return float(calendar.timegm(time.strptime(str(expiration)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return 0.0


def _process_json(doc: dict) -> dict:
    out = {k: doc[k] for k in _PROCESS_KEYS if k in doc}
    out["Version"] = 1
    missing = [k for k in _PROCESS_KEYS if not out.get(k)]
    if missing:
        raise click.ClickException(f"the credential source returned no {', '.join(missing)}")
    return out


def from_broker(role: str, agent: str, socket_path: str) -> Optional[dict]:
    """The runner broker's answer, or None when there is no broker here.
    Raises ClickException when the broker answered with an error."""
    import socket

    if not os.path.exists(socket_path):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    try:
        s.connect(socket_path)
        s.sendall((json.dumps({"role": role, "agent": agent or "unknown"}) + "\n").encode())
        line = s.makefile("rb").readline()
    except OSError:
        return None   # a socket nobody serves: fall through to canopy-web
    finally:
        s.close()
    try:
        doc = json.loads(line or b"{}")
    except ValueError:
        raise click.ClickException("the AWS broker answered with something that is not JSON")
    if doc.get("error"):
        raise click.ClickException(f"the AWS broker refused: {doc['error']}")
    return doc


def from_canopy_web(role: str, agent: str, call: Callable = canopy_web.call) -> dict:
    if not agent:
        raise click.ClickException(
            "no agent: canopy vends AWS roles to an agent (run inside an agent's session, "
            "or pass --agent <slug>)")
    try:
        return call("POST", f"/api/agents/{agent}/aws-credentials", {"role": role})
    except canopy_web.CanopyError as exc:
        raise click.ClickException(f"canopy-web refused AWS role {role} for {agent}: {exc}")


def aws_credentials(role: str, agent: str, *, socket_path: Optional[str] = None,
                    cache_dir: Optional[Path] = None, now: Optional[float] = None,
                    call: Callable = canopy_web.call) -> dict:
    """credential_process JSON for `role` as `agent`: cached, else the box's broker,
    else canopy-web."""
    import time

    now = time.time() if now is None else now
    cache_dir = cache_dir or AWS_CREDENTIALS_CACHE
    cached_path = cache_dir / f"{agent or 'unknown'}--{role}.json"
    try:
        cached = json.loads(cached_path.read_text(encoding="utf-8"))
        if _expires_at(cached.get("Expiration", "")) - now > CREDENTIALS_MIN_LIFE:
            return _process_json(cached)
    except (OSError, ValueError, click.ClickException):
        pass
    socket_path = socket_path or os.environ.get(AWS_BROKER_SOCKET_ENV) or DEFAULT_AWS_BROKER_SOCKET
    doc = from_broker(role, agent, socket_path)
    if doc is None:
        doc = from_canopy_web(role, agent, call=call)
    out = _process_json(doc)
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(cached_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return out


@aws_group.command("credentials")
@click.option("--role", required=True, help="The named role, e.g. labs-observe.")
@click.option("--agent", default="", help="Which agent is asking. Default: this session's agent.")
def credentials_cmd(role: str, agent: str):
    """credential_process JSON for a named read-only role (for the AWS CLI, not people).

    Put it in a profile with `canopy aws profile --role labs-observe`, then use
    `aws ... --profile labs-observe`. Run directly, it prints live AWS keys."""
    agent = agent or canopy_web.agent_context_slug()
    click.echo(json.dumps(aws_credentials(role, agent)))


def profile_block(role: str) -> str:
    region = KNOWN_ROLES[role]
    return (f"[profile {role}]\ncredential_process = canopy aws credentials --role {role}\n"
            f"region = {region}\n")


def write_profile(role: str, config_path: Optional[Path] = None) -> bool:
    """Add `[profile <role>]` to ~/.aws/config unless a profile of that name exists.
    Returns whether it wrote. Never edits an existing profile."""
    config_path = config_path or Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if re.search(rf"^\[profile {re.escape(role)}\]\s*$", text, re.M):
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    with open(config_path, "a", encoding="utf-8") as fh:
        fh.write(sep + profile_block(role))
    return True


@aws_group.command("profile")
@click.option("--role", required=True, type=click.Choice(sorted(KNOWN_ROLES)),
              help="The named role to write a profile for.")
def profile_cmd(role: str):
    """Add an AWS CLI profile that gets its keys from `canopy aws credentials` (idempotent)."""
    wrote = write_profile(role)
    click.echo(f"aws/{role}: {'added' if wrote else 'already in'} ~/.aws/config — "
               f"use `aws ... --profile {role}`")
