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


# What AWS CLI 2.34 prints: the code rides inside `request_uri`, there is no
# `user_code=`, and both URLs are the same link.
SIGNIN_URL = ("https://us-east-1.signin.aws/platform/authorize?request_uri=urn%3Aietf%3Aparams"
              "%3Aoauth%3Arequest_uri%3AFAKEREQ123&identitystore_id=d-0000000000")
AWS_OUTPUT_SIGNIN = f"""Browser will not be automatically opened.
Please visit the following URL:
{SIGNIN_URL}
Then enter the code:
WXYZ-ABCD
Alternatively, you may visit the following URL which will autofill the code upon loading:
{SIGNIN_URL}
"""


def test_the_signin_aws_request_uri_url_is_picked():
    urls = [u for u in map(aws_cli.parse_device_url, AWS_OUTPUT_SIGNIN.splitlines()) if u]
    assert urls and set(urls) == {SIGNIN_URL}


def test_a_signin_aws_login_pushes_its_url(monkeypatch):
    _, _ = _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT_SIGNIN),
                  cfg=CFG, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "signed in as arn:x" in r.output


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


# The laptop runner signs in with its device key and keeps runner.json's token
# EMPTY. Sending "" let canopy_web.call swap in the session's own PAT (an agent's,
# which owns no runner): 404 "runner not found" for an online runner, 2026-10-29.
PAIRED = {"base_url": "https://c", "token": "", "runner_id": "r-1"}


def test_the_live_runners_session_token_is_used_first():
    cfg, why = aws_cli.runner_credential({**PAIRED, "token": "cpat_legacy"},
                                         session_token=lambda base: "crt_session",
                                         key_runner_id=lambda: "")
    assert why == ""
    assert cfg == {"base_url": "https://c", "token": "crt_session", "runner_id": "r-1",
                   "source": "runner-session"}


def test_the_legacy_token_stands_in_only_without_a_session_token():
    cfg, _ = aws_cli.runner_credential({**PAIRED, "token": "cpat_legacy"},
                                       session_token=lambda base: "", key_runner_id=lambda: "")
    assert cfg["token"] == "cpat_legacy" and cfg["source"] == "runner.json"


def test_an_empty_legacy_token_is_never_sent():
    cfg, why = aws_cli.runner_credential(PAIRED, session_token=lambda base: "",
                                         key_runner_id=lambda: "")
    assert cfg is None
    assert "r-1" in why and "device key" in why


def test_a_cloud_box_finds_its_runner_through_its_device_key(monkeypatch):
    monkeypatch.setattr(canopy_web, "resolve_base_url", lambda b: "https://cloud")
    seen = []
    cfg, _ = aws_cli.runner_credential({}, session_token=lambda base: seen.append(base) or "crt",
                                       key_runner_id=lambda: "r-cloud")
    assert cfg["runner_id"] == "r-cloud" and cfg["base_url"] == "https://cloud"
    assert seen == ["https://cloud"]


def test_no_runner_at_all_says_so():
    cfg, why = aws_cli.runner_credential({}, session_token=lambda base: "x",
                                         key_runner_id=lambda: "")
    assert cfg is None and "no canopy runner" in why


def test_an_empty_token_in_runner_json_still_reads():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "runner.json"
        p.write_text(json.dumps({"base_url": "https://c/", "token": "", "runner_id": "r-1"}))
        assert aws_cli.load_runner_config(p) == PAIRED


def test_the_push_speaks_as_the_runner_to_its_own_route():
    seen = {}

    def call(method, path, body, **kw):
        seen.update(method=method, path=path, body=body, **kw)
        return {"sent": 2, "id": "req-1"}

    cfg = {"base_url": "https://c", "token": "cpat_runner", "runner_id": "r-1"}
    sent = aws_cli.push_sign_in_request(cfg, url="https://x.awsapps.com/?user_code=A",
                                        label="labs", requested_by="hal",
                                        reason="read the 5xx logs", call=call)
    assert sent == (2, "req-1")
    assert seen["path"] == "/api/harness/runners/r-1/sign-in-request"
    assert seen["token"] == "cpat_runner" and seen["base_url"] == "https://c"
    assert seen["body"] == {"provider": "aws", "url": "https://x.awsapps.com/?user_code=A",
                            "label": "labs", "requested_by": "hal",
                            "reason": "read the 5xx logs"}


def test_an_empty_token_is_refused_before_it_reaches_canopy_web():
    import pytest

    def call(*a, **k):
        raise AssertionError("must not be sent")

    with pytest.raises(canopy_web.CanopyError, match="no runner token"):
        aws_cli.push_sign_in_request(PAIRED, url="u", label="l", requested_by="h",
                                     reason="r", call=call)


class _FakeProc:
    def __init__(self, output: str, rc: int = 0, running: bool = False):
        self.stdout = iter(output.splitlines(keepends=True))
        self._rc = rc
        self._running = running
        self.killed = False

    def wait(self, timeout=None):
        return self._rc

    def poll(self):
        return None if self._running else self._rc

    def kill(self):
        self.killed = True


LOGIN = ["login", "--reason", "read the canopy-web 5xx logs"]
CFG = {"base_url": "https://c", "token": "t", "runner_id": "r"}


def test_a_canopy_web_that_stores_nothing_reads_as_no_id():
    sent = aws_cli.push_sign_in_request({"base_url": "https://c", "token": "t", "runner_id": "r"},
                                        url="u", label="l", requested_by="h", reason="r",
                                        call=lambda *a, **k: {"sent": 1})
    assert sent == (1, "")


def _patch(monkeypatch, *, identities, proc, cfg, sent=None, push_error=None, request_id="req-1"):
    ids = iter(identities)
    monkeypatch.setattr(aws_cli, "caller_identity", lambda profile: next(ids))
    monkeypatch.setattr(aws_cli.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(aws_cli, "runner_credential",
                        lambda: (cfg, "" if cfg else "this box is no canopy runner"))
    monkeypatch.setattr(canopy_web, "agent_context_slug", lambda: "hal")
    pushed: list[dict] = []

    def push(cfg, **kw):
        pushed.append(kw)
        if push_error:
            raise canopy_web.CanopyError(push_error)
        return sent, request_id

    monkeypatch.setattr(aws_cli, "push_sign_in_request", push)
    reported: list[tuple] = []

    def call(method, path, body, **kw):
        reported.append((path, body["outcome"]))
        return {}

    monkeypatch.setattr(canopy_web, "call", call)
    return pushed, reported


def test_a_reason_is_required(monkeypatch):
    _patch(monkeypatch, identities=[""], proc=_FakeProc(AWS_OUTPUT), cfg=CFG, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, ["login"])
    assert r.exit_code == 2 and "--reason" in r.output


def test_a_blank_reason_is_refused_before_anything_starts(monkeypatch):
    proc = _FakeProc(AWS_OUTPUT)
    _patch(monkeypatch, identities=[""], proc=proc, cfg=CFG, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, ["login", "--reason", "   "])
    assert r.exit_code == 2 and "--reason" in r.output


def test_the_reason_and_agent_name_reach_the_alert(monkeypatch):
    pushed, _ = _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT),
                    cfg=CFG, sent=1)
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
           proc=_FakeProc(AWS_OUTPUT), cfg=CFG, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "pushed to the runner owner's phone (1 device)" in r.output
    assert "user_code" not in r.output, "a delivered push should not also spray the link"
    assert "signed in as arn:aws:sts::1:assumed-role/x/jj" in r.output


def test_zero_devices_falls_back_to_printing_the_link(monkeypatch):
    _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT),
           cfg=CFG, sent=0)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "could not push" in r.output
    assert "?user_code=ABCD-EFGH" in r.output


def test_a_runner_not_found_404_says_what_to_do(monkeypatch):
    _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT),
           cfg={**CFG, "source": "runner.json"},
           push_error='POST /api/harness/runners/r/sign-in-request -> 404: {"detail": "runner not found"}')
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "does not belong to the runner's owner" in r.output
    assert "runner.json credential" in r.output
    assert "?user_code=ABCD-EFGH" in r.output, "the link is still printed to deliver by hand"


def test_no_runner_credential_prints_why_and_the_link(monkeypatch):
    _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT), cfg=None)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "no canopy runner" in r.output and "?user_code=ABCD-EFGH" in r.output


def test_a_refused_push_falls_back_too(monkeypatch):
    _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT),
           cfg=CFG, push_error="POST … -> 404")
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "404" in r.output and "?user_code=ABCD-EFGH" in r.output


def test_an_unapproved_login_fails_loudly(monkeypatch):
    _patch(monkeypatch, identities=["", ""], proc=_FakeProc(AWS_OUTPUT, rc=1),
           cfg=CFG, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code != 0
    assert "did not complete" in r.output


def test_no_device_url_is_an_error_not_a_hang(monkeypatch):
    proc = _FakeProc("Error loading SSO Token: profile is not an SSO profile\n", rc=255)
    _patch(monkeypatch, identities=[""], proc=proc, cfg=None)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code != 0
    assert "without printing an approval URL" in r.output
    assert "exited 255" in r.output
    # The CLI's own words are the diagnosis; a guessed cause alone hides it.
    assert "profile is not an SSO profile" in r.output
    assert proc.killed


def test_a_login_that_prints_no_url_and_keeps_running_says_so(monkeypatch):
    proc = _FakeProc("", running=True)
    _patch(monkeypatch, identities=[""], proc=proc, cfg=None)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code != 0
    assert "still running after 60s" in r.output
    assert "(it printed nothing)" in r.output
    assert proc.killed


def test_only_the_tail_of_a_long_failure_is_quoted():
    proc = _FakeProc("".join(f"line {i}\n" for i in range(40)))
    found, box, seen = aws_cli.threading.Event(), [], []
    aws_cli._read_device_url(proc, found, box, seen)
    assert seen == [f"line {i}" for i in range(40 - aws_cli._SEEN_LINES, 40)]
    assert found.is_set() and box == []


def test_each_outcome_is_reported_so_the_feed_stops_offering_the_link(monkeypatch):
    _, reported = _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT),
                         cfg=CFG, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "listed on their supervisor feed" in r.output
    assert reported == [("/api/harness/runners/r/sign-in-requests/req-1/result", "approved")]


def test_a_failed_login_is_reported_as_failed(monkeypatch):
    _, reported = _patch(monkeypatch, identities=["", ""], proc=_FakeProc(AWS_OUTPUT, rc=1),
                         cfg=CFG, sent=1)
    CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert [o for _, o in reported] == ["failed"]


def test_a_timed_out_login_is_reported_as_expired(monkeypatch):
    import subprocess

    class Slow(_FakeProc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("aws", timeout)

    _, reported = _patch(monkeypatch, identities=[""], proc=Slow(AWS_OUTPUT), cfg=CFG, sent=1)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert "has expired" in r.output
    assert [o for _, o in reported] == ["expired"]


def test_an_unreachable_canopy_web_does_not_fail_a_good_login(monkeypatch):
    _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT), cfg=CFG, sent=1)

    def down(*a, **k):
        raise canopy_web.CanopyError("POST … -> 503")

    monkeypatch.setattr(canopy_web, "call", down)
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "signed in as arn:x" in r.output


def test_no_stored_request_reports_nothing(monkeypatch):
    _, reported = _patch(monkeypatch, identities=["", "arn:x"], proc=_FakeProc(AWS_OUTPUT),
                         cfg=CFG, sent=1, request_id="")
    r = CliRunner().invoke(aws_cli.aws_group, LOGIN)
    assert r.exit_code == 0, r.output
    assert "supervisor feed" not in r.output
    assert reported == []
