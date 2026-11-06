"""`canopy sign-in` — the person signs a session in to a third party, for this session only.

A fake `claude` on PATH speaks the real `design-login --json` protocol (a `pages` line out, a
`{"code": …}` line in, a credential written, a `done` line out), so the whole flow runs for
real: the detached process, its stdin FIFO, the credential file and the cleanup. What must
never regress: the process starts WITHOUT the session's environment; the code reaches it only
if it looks like a code; the credential is removed again at stop, at session end and at expiry;
a sign-in that was on the box before is never touched and the rest of the credentials file
(mcpOAuth) survives; nothing prints a token.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import sign_in_cli

ROOT = Path(__file__).parent.parent
HOOK = ROOT / "plugins" / "canopy" / "hooks" / "sign_in_cleanup.py"
SESS = "11111111-2222-4333-8444-555555555555"
CODE = "c0de-AAAAAAAAAAAAAAAAAAAAAAAA#st4te-BBBBBBBBBBBBBBBBBBBBBBBB"
TOKEN = "design-access-SECRET"

FAKE_CLAUDE = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    if any(k.startswith("CLAUDE") or k.startswith("CANOPY") for k in os.environ):
        print(json.dumps({{"event": "done", "ok": False, "message": "inside a session"}}), flush=True)
        sys.exit(0)
    assert sys.argv[1:] == ["design-login", "--json"], sys.argv
    print(json.dumps({{"event": "pages", "url": "http://localhost/cb",
                      "manual_url": "https://claude.test/authorize?x=1", "manual_first": False}}), flush=True)
    for line in sys.stdin:
        doc = json.loads(line)
        if doc.get("code") == "{code}":
            path = os.path.expanduser("~/.claude/.credentials.json")
            cur = json.load(open(path)) if os.path.exists(path) else {{}}
            cur["designOauth"] = {{"accessToken": "{token}", "refreshToken": "r"}}
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            json.dump(cur, os.fdopen(fd, "w"))
            print(json.dumps({{"event": "done", "ok": True}}), flush=True)
        else:
            print(json.dumps({{"event": "done", "ok": False, "message": "The code was refused."}}), flush=True)
        break
''')


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable, code=CODE, token=TOKEN))
    fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESS)
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")  # what a real session exports
    monkeypatch.setattr(sys, "platform", "linux")
    creds = home / ".claude" / ".credentials.json"
    creds.write_text(json.dumps({"mcpOAuth": {"keep": "me"}}))
    creds.chmod(0o600)
    yield home
    for f in (home / ".canopy" / "sign-in").glob("*.json") if (home / ".canopy" / "sign-in").is_dir() else ():
        meta = json.loads(f.read_text())
        sign_in_cli._end(f.stem, clear=True)
        assert meta


def run(*args, stdin=None):
    return CliRunner().invoke(sign_in_cli.sign_in_group, list(args), input=stdin)


def creds(home):
    return json.loads((home / ".claude" / ".credentials.json").read_text())


def hook(home, session=SESS):
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"session_id": session}),
                          text=True, env={**os.environ, "HOME": str(home)}, check=False)


def test_flow_start_code_stop(box):
    r = run("start", "design")
    assert r.exit_code == 0, r.output
    assert "https://claude.test/authorize?x=1" in r.output
    meta = json.loads(next((box / ".canopy" / "sign-in").glob("*.json")).read_text())
    assert meta["provider"] == "design" and meta["phase"] == "waiting"

    r = run("code", "design", "-", stdin=CODE + "\n")
    assert r.exit_code == 0, r.output
    assert "Signed in" in r.output and TOKEN not in r.output and CODE not in r.output
    assert creds(box)["designOauth"]["accessToken"] == TOKEN
    assert creds(box)["mcpOAuth"] == {"keep": "me"}
    assert json.loads(run("status", "design", "--json-output").output)["status"] == "signed_in"

    r = run("stop", "design")
    assert r.exit_code == 0 and "removed" in r.output
    assert "designOauth" not in creds(box) and creds(box)["mcpOAuth"] == {"keep": "me"}
    assert stat.S_IMODE((box / ".claude" / ".credentials.json").stat().st_mode) == 0o600
    assert not list((box / ".canopy" / "sign-in").iterdir())


def test_the_process_does_not_inherit_the_session(box):
    # The fake refuses to run if any CLAUDE*/CANOPY* variable reaches it.
    r = run("start", "design")
    assert r.exit_code == 0, r.output
    assert "inside a session" not in r.output


def test_a_malformed_code_never_reaches_the_process(box):
    run("start", "design")
    r = run("code", "design", "not a code\n{\"cmd\": 1}")
    assert r.exit_code != 0 and "not a sign-in code" in r.output
    assert "designOauth" not in creds(box)


def test_a_refused_code_leaves_nothing_behind(box):
    run("start", "design")
    wrong = "x" * 24 + "#" + "y" * 24
    r = run("code", "design", wrong)
    assert r.exit_code != 0 and "refused" in r.output and wrong not in r.output
    assert "designOauth" not in creds(box)
    assert not list((box / ".canopy" / "sign-in").iterdir())


def test_an_existing_sign_in_is_never_touched(box):
    path = box / ".claude" / ".credentials.json"
    path.write_text(json.dumps({"designOauth": {"accessToken": "theirs"}, "mcpOAuth": {}}))
    r = run("start", "design")
    assert r.exit_code != 0 and "already signed in" in r.output
    assert creds(box)["designOauth"]["accessToken"] == "theirs"


def test_nobody_present_means_no_link(box):
    pres = box / ".canopy" / "caller" / "presence"
    pres.mkdir(parents=True)
    (pres / f"{SESS}.json").write_text(json.dumps({"present": False}))
    r = run("start", "design")
    assert r.exit_code != 0 and "nobody is in this conversation" in r.output


def test_session_end_hook_removes_this_sessions_credential(box):
    run("start", "design")
    run("code", "design", CODE)
    assert "designOauth" in creds(box)
    assert hook(box, "other-session").returncode == 0
    assert "designOauth" in creds(box)  # someone else's session ending is not ours
    assert hook(box, SESS).returncode == 0
    assert "designOauth" not in creds(box) and creds(box)["mcpOAuth"] == {"keep": "me"}
    assert not list((box / ".canopy" / "sign-in").iterdir())


def test_expiry_sweep_removes_it_and_cli_and_hook_agree(box, monkeypatch):
    run("start", "design")
    run("code", "design", CODE)
    meta_path = next((box / ".canopy" / "sign-in").glob("*.json"))
    meta = json.loads(meta_path.read_text())
    meta["expires_at"] = time.time() - 1
    meta_path.write_text(json.dumps(meta))
    assert hook(box, "unrelated").returncode == 0
    assert "designOauth" not in creds(box)
    # The CLI's own sweep does the same to an expired one.
    run("start", "design")
    run("code", "design", CODE)
    meta = json.loads(meta_path.read_text())
    meta["expires_at"] = time.time() - 1
    meta_path.write_text(json.dumps(meta))
    sign_in_cli.sweep_expired()
    assert "designOauth" not in creds(box)


def test_unknown_provider_is_refused(box):
    r = run("start", "nope")
    assert r.exit_code != 0 and "no such sign-in" in r.output


def test_hook_is_inert_without_a_state_dir(tmp_path):
    spec = importlib.util.spec_from_file_location("sign_in_cleanup", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = str(tmp_path / "missing")
    assert mod.main() == 0
