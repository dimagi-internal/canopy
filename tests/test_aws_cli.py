"""`canopy aws login`: the parts that fail as a wrong answer, not an error.

Picking the wrong line out of the AWS CLI's output would send the bare portal
URL with no code in it. Mis-reading the runner config would fall back silently
when it should have pushed. Reading `sent: 0` as success would leave everyone
waiting on a notification that nobody received.
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from orchestrator import aws_cli, canopy_web

AWS_OUTPUT = """Browser will not be automatically opened.
Please visit the following URL:

https://commcare-connect.awsapps.com/start/#/device

Then enter the code:

ABCD-EFGH

Alternatively, you may visit the following URL which will autofill the code upon loading:
https://commcare-connect.awsapps.com/start/#/device?user_code=ABCD-EFGH
"""


def test_the_autofill_url_is_picked_not_the_bare_portal():
    urls = [u for u in map(aws_cli.parse_device_url, AWS_OUTPUT.splitlines()) if u]
    assert urls == ["https://commcare-connect.awsapps.com/start/#/device?user_code=ABCD-EFGH"]


def test_runner_config_reads_an_inline_token(tmp_path):
    p = tmp_path / "runner.json"
    p.write_text(json.dumps({"base_url": "https://canopy.example/", "token": "cpat_x",
                             "runner_id": "r-1", "poll_seconds": 5}))
    assert aws_cli.load_runner_config(p) == {
        "base_url": "https://canopy.example", "token": "cpat_x", "runner_id": "r-1"}


def test_runner_config_follows_an_at_file_token(tmp_path):
    (tmp_path / "tok").write_text("cpat_from_file\n")
    p = tmp_path / "runner.json"
    p.write_text(json.dumps({"base_url": "https://c", "token": f"@{tmp_path / 'tok'}",
                             "runner_id": "r-1"}))
    assert aws_cli.load_runner_config(p)["token"] == "cpat_from_file"


def test_no_runner_config_is_none_not_an_error(tmp_path):
    assert aws_cli.load_runner_config(tmp_path / "missing.json") is None


def test_the_push_speaks_as_the_runner_to_its_own_route():
    seen = {}

    def call(method, path, body, **kw):
        seen.update(method=method, path=path, body=body, **kw)
        return {"sent": 2}

    cfg = {"base_url": "https://c", "token": "cpat_runner", "runner_id": "r-1"}
    sent = aws_cli.push_sign_in_request(cfg, url="https://x.awsapps.com/?user_code=A",
                                        label="labs", requested_by="hal",
                                        reason="read the 5xx logs", call=call)
    assert sent == 2
    assert seen["path"] == "/api/harness/runners/r-1/sign-in-request"
    assert seen["token"] == "cpat_runner" and seen["base_url"] == "https://c"
    assert seen["body"] == {"provider": "aws", "url": "https://x.awsapps.com/?user_code=A",
                            "label": "labs", "requested_by": "hal",
                            "reason": "read the 5xx logs"}


class _FakeProc:
    def __init__(self, output: str, rc: int = 0):
        self.stdout = iter(output.splitlines(keepends=True))
        self._rc = rc
        self.killed = False

    def wait(self, timeout=None):
        return self._rc

    def kill(self):
        self.killed = True


LOGIN = ["login", "--reason", "read the canopy-web 5xx logs"]


def _patch(monkeypatch, *, identities, proc, cfg, sent=None, push_error=None):
    ids = iter(identities)
    monkeypatch.setattr(aws_cli, "caller_identity", lambda profile: next(ids))
    monkeypatch.setattr(aws_cli.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(aws_cli, "load_runner_config", lambda: cfg)
    monkeypatch.setattr(canopy_web, "agent_context_slug", lambda: "hal")
    pushed: list[dict] = []

    def push(cfg, **kw):
        pushed.append(kw)
        if push_error:
            raise canopy_web.CanopyError(push_error)
        return sent

    monkeypatch.setattr(aws_cli, "push_sign_in_request", push)
    return pushed


def test_a_reason_is_required(monkeypatch):
    _patch(monkeypatch, identities=[""], proc=_FakeProc(AWS_OUTPUT), cfg={"runner_id": "r"}, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, ["login"])
    assert r.exit_code == 2 and "--reason" in r.output


def test_a_blank_reason_is_refused_before_anything_starts(monkeypatch):
    proc = _FakeProc(AWS_OUTPUT)
    _patch(monkeypatch, identities=[""], proc=proc, cfg={"runner_id": "r"}, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, ["login", "--reason", "   "])
    assert r.exit_code == 2 and "--reason" in r.output


def test_the_reason_and_agent_name_reach_the_alert(monkeypatch):
    pushed = _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT),
                    cfg={"runner_id": "r"}, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, ["login", "--reason", "  read the\n5xx logs "])
    assert r.exit_code == 0, r.output
    assert pushed[0]["reason"] == "read the 5xx logs"
    assert pushed[0]["requested_by"] == "Hal"


def test_a_live_session_is_left_alone(monkeypatch):
    _patch(monkeypatch, identities=["arn:aws:sts::1:assumed-role/x/jj"], proc=None, cfg=None)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "already signed in" in r.output


def test_pushed_then_approved_reports_the_identity(monkeypatch):
    _patch(monkeypatch, identities=["", "arn:aws:sts::1:assumed-role/x/jj"],
           proc=_FakeProc(AWS_OUTPUT), cfg={"runner_id": "r"}, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "pushed to the runner owner's phone (1 device)" in r.output
    assert "user_code" not in r.output, "a delivered push should not also spray the link"
    assert "signed in as arn:aws:sts::1:assumed-role/x/jj" in r.output


def test_zero_devices_falls_back_to_printing_the_link(monkeypatch):
    _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT),
           cfg={"runner_id": "r"}, sent=0)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "could not push" in r.output
    assert "?user_code=ABCD-EFGH" in r.output


def test_a_refused_push_falls_back_too(monkeypatch):
    _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT),
           cfg={"runner_id": "r"}, push_error="POST … -> 404")
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "404" in r.output and "?user_code=ABCD-EFGH" in r.output


def test_an_unapproved_login_fails_loudly(monkeypatch):
    _patch(monkeypatch, identities=["", ""], proc=_FakeProc(AWS_OUTPUT, rc=1),
           cfg={"runner_id": "r"}, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code != 0
    assert "did not complete" in r.output


def test_no_device_url_is_an_error_not_a_hang(monkeypatch):
    proc = _FakeProc("Error loading SSO Token: profile is not an SSO profile\n", rc=255)
    _patch(monkeypatch, identities=[""], proc=proc, cfg=None)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code != 0
    assert "printed no approval URL" in r.output
    assert proc.killed
