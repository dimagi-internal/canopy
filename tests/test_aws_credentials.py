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
