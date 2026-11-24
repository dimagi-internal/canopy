"""`canopy aws credentials` / `canopy aws profile`: one hour of a named read-only role.

Pinned: the box's broker wins over canopy-web (it works when canopy-web is down); a
broker refusal is an error, not a silent fall-through; keys are cached 0600 and reused
until 5 minutes before expiry; only credential_process keys are printed; the profile is
added once and an existing one is never edited; and the fleet rail blocks a raw
`canopy aws credentials` statement (it would print keys into a transcript) while the
profile path, `--help` and grep pass.
"""
from __future__ import annotations

import json
import os
import re
import socket
import stat
import tempfile
import threading
from pathlib import Path

import click
import pytest

from orchestrator import aws_cli

GOOD = {"Version": 1, "AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "t",
        "Expiration": "2099-01-01T00:00:00Z", "RoleArn": "arn:aws:iam::1:role/labs-jj-observe-readonly"}


def _no_call(*a, **k):
    raise AssertionError("canopy-web must not be called")


def _broker(reply: dict):
    d = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(d, "s")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    seen = {}

    def serve():
        c, _ = srv.accept()
        seen["req"] = json.loads(c.makefile().readline())
        c.sendall((json.dumps(reply) + "\n").encode())
        c.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return path, seen


def test_the_box_broker_wins_and_only_process_keys_are_kept(tmp_path):
    path, seen = _broker(GOOD)
    out = aws_cli.aws_credentials("labs-observe", "hal", socket_path=path, cache_dir=tmp_path, call=_no_call)
    assert seen["req"] == {"role": "labs-observe", "agent": "hal"}
    assert set(out) == {"Version", "AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"}
    cached = tmp_path / "hal--labs-observe.json"
    assert stat.S_IMODE(cached.stat().st_mode) == 0o600


def test_a_broker_refusal_is_an_error_not_a_fall_through(tmp_path):
    path, _ = _broker({"error": "uid 0 is not a runner"})
    with pytest.raises(click.ClickException, match="broker refused"):
        aws_cli.aws_credentials("labs-observe", "hal", socket_path=path, cache_dir=tmp_path, call=_no_call)


def test_no_broker_means_canopy_web_as_the_agent(tmp_path):
    calls = []

    def call(method, path, body):
        calls.append((method, path, body))
        return GOOD

    out = aws_cli.aws_credentials("labs-observe", "hal", socket_path=str(tmp_path / "none"),
                                  cache_dir=tmp_path, call=call)
    assert calls == [("POST", "/api/agents/hal/aws-credentials", {"role": "labs-observe"})]
    assert out["SessionToken"] == "t"


def test_no_agent_and_no_broker_says_so(tmp_path):
    with pytest.raises(click.ClickException, match="no agent"):
        aws_cli.aws_credentials("labs-observe", "", socket_path=str(tmp_path / "none"),
                                cache_dir=tmp_path, call=_no_call)


def test_cached_keys_are_reused_until_near_expiry(tmp_path):
    (tmp_path / "hal--labs-observe.json").write_text(json.dumps(GOOD))
    out = aws_cli.aws_credentials("labs-observe", "hal", socket_path=str(tmp_path / "none"),
                                  cache_dir=tmp_path, call=_no_call)
    assert out["AccessKeyId"] == "ASIA"
    stale = dict(GOOD, Expiration="2000-01-01T00:00:00Z")
    (tmp_path / "hal--labs-observe.json").write_text(json.dumps(stale))
    fresh = dict(GOOD, AccessKeyId="ASIANEW")
    out = aws_cli.aws_credentials("labs-observe", "hal", socket_path=str(tmp_path / "none"),
                                  cache_dir=tmp_path, call=lambda *a: fresh)
    assert out["AccessKeyId"] == "ASIANEW"


def test_profile_is_added_once_and_never_edited(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("[profile labs]\nsso_session = ccc\n")
    assert aws_cli.write_profile("labs-observe", cfg) is True
    assert aws_cli.write_profile("labs-observe", cfg) is False
    text = cfg.read_text()
    assert text.count("[profile labs-observe]") == 1
    assert "credential_process = canopy aws credentials --role labs-observe" in text
    assert text.startswith("[profile labs]\nsso_session = ccc\n")


BASELINE = Path(__file__).parent.parent / "plugins" / "canopy" / "agent-core" / "gating-baseline.json"
RAILS = [r for r in json.loads(BASELINE.read_text())["always"]
         if r.get("tool") == "Bash" and "aws\\s+credentials" in r.get("pattern", "")]
_SPLIT = re.compile(r"[\n;]|&&|\|\||[|&]")


def blocks(cmd: str) -> bool:
    stmts = [s.strip() for s in _SPLIT.split(cmd) if s.strip()]
    return any(re.search(r["pattern"], s) for r in RAILS for s in stmts)


@pytest.mark.parametrize("cmd", [
    "canopy aws credentials --role labs-observe",
    "CANOPY_AGENT=hal canopy aws credentials --role labs-observe",
    "cd /tmp && canopy aws credentials --role labs-observe | jq .",
    "if true; then canopy aws credentials --role labs-observe; fi",
])
def test_rail_blocks_printing_keys(cmd):
    assert len(RAILS) == 1
    assert blocks(cmd)


@pytest.mark.parametrize("cmd", [
    "aws cloudwatch describe-alarms --profile labs-observe",
    "canopy aws profile --role labs-observe",
    "canopy aws credentials --help",
    "canopy aws login --profile labs --reason x",
    'grep -rn "canopy aws credentials" .',
])
def test_rail_lets_the_profile_path_and_mentions_pass(cmd):
    assert not blocks(cmd)


def test_labs_readwrite_never_asks_the_box_broker(tmp_path):
    # The broker cannot verify which agent asks, so the write role comes from canopy-web only.
    path, seen = _broker(GOOD)
    calls = []
    out = aws_cli.aws_credentials("labs-readwrite", "hal", socket_path=path, cache_dir=tmp_path,
                                  call=lambda *a: calls.append(a) or GOOD)
    assert calls == [("POST", "/api/agents/hal/aws-credentials", {"role": "labs-readwrite"})]
    assert "req" not in seen
    assert out["AccessKeyId"] == "ASIA"


def test_labs_read_falls_back_to_the_old_name_where_a_source_predates_it(tmp_path):
    asked = []

    def old_canopy_web(method, path, body):
        asked.append(body["role"])
        if body["role"] != "labs-observe":
            raise aws_cli.canopy_web.CanopyError("404 canopy does not vend an AWS role named 'labs-read'")
        return GOOD

    out = aws_cli.aws_credentials("labs-read", "hal", socket_path=str(tmp_path / "none"),
                                  cache_dir=tmp_path, call=old_canopy_web)
    assert asked == ["labs-read", "labs-observe"]
    assert out["AccessKeyId"] == "ASIA"
    assert (tmp_path / "hal--labs-read.json").exists()


def test_a_refusal_of_every_name_reports_the_new_one(tmp_path):
    def refuse(method, path, body):
        raise aws_cli.canopy_web.CanopyError(f"403 not for {body['role']}")

    with pytest.raises(click.ClickException, match="labs-read for hal"):
        aws_cli.aws_credentials("labs-read", "hal", socket_path=str(tmp_path / "none"),
                                cache_dir=tmp_path, call=refuse)


@pytest.mark.parametrize("role", ["labs-read", "labs-readwrite", "labs-config", "labs-observe"])
def test_profiles_for_both_roles_and_the_old_name(tmp_path, role):
    cfg = tmp_path / "config"
    assert aws_cli.write_profile(role, cfg) is True
    assert f"credential_process = canopy aws credentials --role {role}\nregion = us-east-1" in cfg.read_text()


def test_labs_config_never_asks_the_box_broker(tmp_path):
    path, seen = _broker(GOOD)
    calls = []
    aws_cli.aws_credentials("labs-config", "ace", socket_path=path, cache_dir=tmp_path,
                            call=lambda *a: calls.append(a) or GOOD)
    assert calls == [("POST", "/api/agents/ace/aws-credentials", {"role": "labs-config"})]
    assert "req" not in seen


def test_approval_required_says_how_to_ask(tmp_path):
    def refuse(method, path, body):
        raise aws_cli.canopy_web.CanopyError(
            'POST /api/agents/hal/aws-credentials -> 403: {"error": "approval_required", '
            '"request_with": "POST /api/agents/hal/aws-role-requests"}')

    with pytest.raises(click.ClickException) as exc:
        aws_cli.aws_credentials("labs-readwrite", "hal", socket_path=str(tmp_path / "none"),
                                cache_dir=tmp_path, call=refuse)
    msg = exc.value.message
    assert "\n" not in msg
    assert 'canopy aws request --role labs-readwrite --reason' in msg


CARD = {"id": 7, "agent": "hal", "role": "labs-readwrite", "reason": "restore a rehearsal copy", "minutes": 90,
        "status": "pending", "approval_id": 41, "approval_url": "https://canopy.dimagi.com/w/dimagi/approvals/41",
        "created_at": "2026-11-23T01:00:00Z"}


def test_request_posts_the_role_reason_and_minutes():
    calls = []
    card = aws_cli.request_role("labs-readwrite", "  restore a\nrehearsal copy ", 90, "hal",
                                call=lambda *a: calls.append(a) or CARD)
    assert calls == [("POST", "/api/agents/hal/aws-role-requests",
                      {"role": "labs-readwrite", "reason": "restore a rehearsal copy", "minutes": 90})]
    assert card["approval_url"].endswith("/approvals/41")
    with pytest.raises(click.UsageError):
        aws_cli.request_role("labs-readwrite", "   ", 60, "hal", call=_no_call)
    with pytest.raises(click.ClickException, match="no agent"):
        aws_cli.request_role("labs-readwrite", "why", 60, "", call=_no_call)


def test_request_command_prints_the_approval_page(monkeypatch):
    from click.testing import CliRunner

    monkeypatch.setattr(aws_cli.canopy_web, "call", lambda *a, **k: CARD)
    r = CliRunner().invoke(aws_cli.aws_group, ["request", "--role", "labs-readwrite", "--reason", "restore",
                                               "--minutes", "90", "--agent", "hal"])
    assert r.exit_code == 0, r.output
    assert "https://canopy.dimagi.com/w/dimagi/approvals/41" in r.output
    r = CliRunner().invoke(aws_cli.aws_group, ["request", "--role", "labs-readwrite", "--reason", "x",
                                               "--minutes", "481", "--agent", "hal"])
    assert r.exit_code != 0


def test_requests_command_lists_pending_and_live_grants(monkeypatch):
    from click.testing import CliRunner

    live = dict(CARD, id=6, status="approved", expires_at="2026-11-23T03:00:00Z", approved_by="jj@dimagi.com")
    seen = []
    monkeypatch.setattr(aws_cli.canopy_web, "call", lambda m, p, *a, **k: seen.append((m, p)) or [CARD, live])
    r = CliRunner().invoke(aws_cli.aws_group, ["requests", "--agent", "hal", "--role", "labs-readwrite"])
    assert r.exit_code == 0, r.output
    assert seen == [("GET", "/api/agents/hal/aws-role-requests?role=labs-readwrite")]
    lines = r.output.strip().splitlines()
    assert lines[0].startswith("#7 labs-readwrite pending") and "approvals/41" in lines[0]
    assert lines[1].startswith("#6 labs-readwrite approved") and "until 2026-11-23T03:00:00Z" in lines[1]


@pytest.mark.parametrize("role,hint", [("labs-read", True), ("labs-observe", True),
                                       ("labs-readwrite", True), ("labs-config", False)])
def test_profile_points_read_roles_at_the_scrubbed_log_groups(tmp_path, monkeypatch, role, hint):
    """Where the profile is made, say where labs logs live and where to start for slowness."""
    from click.testing import CliRunner
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    out = CliRunner().invoke(aws_cli.profile_cmd, ["--role", role]).output
    assert ("/labs-jj/observe/alb-requests" in out) is hint
    assert f"--profile {role}" in out
