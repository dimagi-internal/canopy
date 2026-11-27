"""`canopy aws credentials` / `canopy aws profile`: one hour of a named read-only role.

Pinned: canopy-web is the only source (the box broker is retired: it could not tell which
agent asked, so it could not honour an approval); keys are cached 0600 and reused
until 5 minutes before expiry; only credential_process keys are printed; the profile is
added once and an existing one is never edited; and the fleet rail blocks a raw
`canopy aws credentials` statement (it would print keys into a transcript) while the
profile path, `--help` and grep pass.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import click
import pytest

from orchestrator import aws_cli

GOOD = {"Version": 1, "AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "t",
        "Expiration": "2099-01-01T00:00:00Z", "RoleArn": "arn:aws:iam::1:role/labs-jj-observe-readonly"}


def _no_call(*a, **k):
    raise AssertionError("canopy-web must not be called")


def _unsealed(call):
    """`call` behind a canopy-web with nothing sealed for the role: the sealed route answers
    404 `no_sealed_source`, so `canopy aws credentials` goes on to canopy-web's own route."""
    def wrapped(method, path, body):
        if path.endswith("/aws-credentials/sealed"):
            raise aws_cli.canopy_web.CanopyError(f'{method} {path} -> 404: {{"error": "no_sealed_source"}}')
        return call(method, path, body)
    return wrapped


def test_canopy_web_as_the_agent_keeps_only_process_keys(tmp_path):
    calls = []

    def call(method, path, body):
        calls.append((method, path, body))
        return GOOD

    out = aws_cli.aws_credentials("labs-observe", "hal",
                                  cache_dir=tmp_path, call=_unsealed(call))
    assert calls == [("POST", "/api/agents/hal/aws-credentials", {"role": "labs-observe"})]
    assert set(out) == {"Version", "AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"}
    assert stat.S_IMODE((tmp_path / "hal--labs-observe.json").stat().st_mode) == 0o600


def test_no_agent_says_so(tmp_path):
    with pytest.raises(click.ClickException, match="no agent"):
        aws_cli.aws_credentials("labs-observe", "",
                                cache_dir=tmp_path, call=_no_call)


def test_cached_keys_are_reused_until_near_expiry(tmp_path):
    (tmp_path / "hal--labs-observe.json").write_text(json.dumps(GOOD))
    out = aws_cli.aws_credentials("labs-observe", "hal",
                                  cache_dir=tmp_path, call=_no_call)
    assert out["AccessKeyId"] == "ASIA"
    stale = dict(GOOD, Expiration="2000-01-01T00:00:00Z")
    (tmp_path / "hal--labs-observe.json").write_text(json.dumps(stale))
    fresh = dict(GOOD, AccessKeyId="ASIANEW")
    out = aws_cli.aws_credentials("labs-observe", "hal",
                                  cache_dir=tmp_path, call=_unsealed(lambda *a: fresh))
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


def test_labs_read_falls_back_to_the_old_name_where_a_source_predates_it(tmp_path):
    asked = []

    def old_canopy_web(method, path, body):
        asked.append(body["role"])
        if body["role"] != "labs-observe":
            raise aws_cli.canopy_web.CanopyError("404 canopy does not vend an AWS role named 'labs-read'")
        return GOOD

    out = aws_cli.aws_credentials("labs-read", "hal",
                                  cache_dir=tmp_path, call=_unsealed(old_canopy_web))
    assert asked == ["labs-read", "labs-observe"]
    assert out["AccessKeyId"] == "ASIA"
    assert (tmp_path / "hal--labs-read.json").exists()


def test_a_refusal_of_every_name_reports_the_new_one(tmp_path):
    def refuse(method, path, body):
        raise aws_cli.canopy_web.CanopyError(f"403 not for {body['role']}")

    with pytest.raises(click.ClickException, match="labs-read for hal"):
        aws_cli.aws_credentials("labs-read", "hal",
                                cache_dir=tmp_path, call=_unsealed(refuse))


@pytest.mark.parametrize("role", ["labs-read", "labs-readwrite", "labs-config", "labs-observe"])
def test_profiles_for_both_roles_and_the_old_name(tmp_path, role):
    cfg = tmp_path / "config"
    assert aws_cli.write_profile(role, cfg) is True
    assert f"credential_process = canopy aws credentials --role {role}\nregion = us-east-1" in cfg.read_text()


def test_approval_required_says_how_to_ask(tmp_path):
    def refuse(method, path, body):
        raise aws_cli.canopy_web.CanopyError(
            'POST /api/agents/hal/aws-credentials -> 403: {"error": "approval_required", '
            '"request_with": "POST /api/agents/hal/aws-role-requests"}')

    with pytest.raises(click.ClickException) as exc:
        aws_cli.aws_credentials("labs-readwrite", "hal",
                                cache_dir=tmp_path, call=_unsealed(refuse))
    msg = exc.value.message
    assert "\n" not in msg
    assert 'canopy aws request --role labs-readwrite --reason' in msg


CARD = {"id": 7, "agent": "hal", "role": "labs-readwrite", "reason": "restore a rehearsal copy", "minutes": 90,
        "status": "pending", "approval_id": 41, "approval_url": "https://canopy.dimagi.com/w/dimagi/approvals/41",
        "created_at": "2026-11-23T01:00:00Z"}


def test_request_posts_the_role_reason_and_minutes():
    calls = []
    card = aws_cli.request_role("labs-readwrite", "  restore a\nrehearsal copy ", 90, "hal",
                                call=lambda *a: calls.append(a) or CARD, delivery="custodial")
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


def test_no_role_asks_a_box_broker():
    # Retired: the broker could not tell which agent asked, so it could not honour an approval.
    assert not hasattr(aws_cli, "from_broker")
    assert "socket_path" not in aws_cli.aws_credentials.__code__.co_varnames
