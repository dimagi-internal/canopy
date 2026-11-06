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

import click
import pytest
from click.testing import CliRunner

from orchestrator import secret_cli, sign_in_cli

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
    monkeypatch.setattr(sign_in_cli, "SPAWN_WATCHER", False)  # the watcher is tested in-process
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


class Secrets:
    """The chat's secrets, as `canopy secret` would list and open them."""

    def __init__(self, monkeypatch):
        self.values = {}
        self.used = set()
        monkeypatch.setattr(secret_cli, "this_chat", lambda: "chk_test")
        monkeypatch.setattr(secret_cli, "list_secrets",
                            lambda key, **k: [{"name": n, "last_used_at": n in self.used}
                                              for n in self.values])
        monkeypatch.setattr(secret_cli, "fetch_value", lambda key, name, **k: self.values[name])


@pytest.fixture
def secrets(box, monkeypatch):
    return Secrets(monkeypatch)


def run(*args, stdin=None):
    return CliRunner().invoke(sign_in_cli.sign_in_group, list(args), input=stdin)


def creds(home):
    return json.loads((home / ".claude" / ".credentials.json").read_text())


def hook(home, session=SESS):
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"session_id": session}),
                          text=True, env={**os.environ, "HOME": str(home)}, check=False)


def test_flow_start_code_stop(box, secrets):
    r = run("start", "design")
    assert r.exit_code == 0, r.output
    assert "https://claude.test/authorize?x=1" in r.output
    meta = json.loads(next((box / ".canopy" / "sign-in").glob("*.json")).read_text())
    assert meta["provider"] == "design" and meta["phase"] == "waiting"

    secrets.values["CLAUDE_DESIGN_CODE"] = CODE
    r = run("code", "design")
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


def test_the_process_does_not_inherit_the_session(box, secrets):
    # The fake refuses to run if any CLAUDE*/CANOPY* variable reaches it.
    r = run("start", "design")
    assert r.exit_code == 0, r.output
    assert "inside a session" not in r.output


def test_a_malformed_code_never_reaches_the_process(box, secrets):
    run("start", "design")
    secrets.values["CLAUDE_DESIGN_CODE"] = 'not a code\n{"cmd": 1}'
    r = run("code", "design")
    assert r.exit_code != 0 and "does not look like a sign-in code" in r.output
    assert "designOauth" not in creds(box)


def test_no_secret_yet_says_what_to_add(box, secrets):
    run("start", "design")
    r = run("code", "design")
    assert r.exit_code != 0 and "CLAUDE_DESIGN_CODE" in r.output and "Secrets" in r.output


def test_the_code_is_never_taken_as_text(box, secrets):
    run("start", "design")
    r = run("code", "design", CODE)
    assert r.exit_code != 0  # no argument form: it comes from the chat's secrets only
    r = run("code", "design", stdin=CODE)
    assert r.exit_code != 0 and "CLAUDE_DESIGN_CODE" in r.output


def test_wait_picks_up_a_secret_added_while_waiting(box, secrets, monkeypatch):
    run("start", "design")
    calls = {"n": 0}

    def later(_s):
        calls["n"] += 1
        if calls["n"] == 2:
            secrets.values["CLAUDE_DESIGN_CODE"] = CODE

    monkeypatch.setattr(sign_in_cli, "SLEEP", later)
    r = run("code", "design", "--wait")
    assert r.exit_code == 0, r.output
    assert creds(box)["designOauth"]["accessToken"] == TOKEN and CODE not in r.output


def test_a_refused_code_leaves_nothing_behind(box, secrets):
    run("start", "design")
    wrong = "x" * 24 + "#" + "y" * 24
    secrets.values["CLAUDE_DESIGN_CODE"] = wrong
    r = run("code", "design")
    assert r.exit_code != 0 and "refused" in r.output and wrong not in r.output
    assert "designOauth" not in creds(box)
    left = sorted(p.suffix for p in (box / ".canopy" / "sign-in").iterdir())
    assert left == [".json"]  # only the short record of why; no fifo, no output, no process
    st = json.loads(run("status", "design", "--json-output").output)
    assert st["status"] == "failed" and "refused" in st["error"] and wrong not in json.dumps(st)
    assert run("start", "design").exit_code == 0  # a new link replaces the record


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


def test_a_turn_ending_does_not_end_a_live_sign_in(box, secrets):
    """In a chat every turn is a new Claude process, so SessionEnd fires between the turn that
    starts a sign-in and the one that uses it; the hook must leave a live one alone."""
    secrets.values["CLAUDE_DESIGN_CODE"] = CODE
    run("start", "design")
    assert hook(box, SESS).returncode == 0  # while it waits for the code
    assert json.loads(run("status", "design", "--json-output").output)["status"] == "waiting"
    run("code", "design")
    assert hook(box, SESS).returncode == 0  # and once signed in
    assert "designOauth" in creds(box)
    assert json.loads(run("status", "design", "--json-output").output)["status"] == "signed_in"


def test_expiry_sweep_removes_it_and_cli_and_hook_agree(box, secrets, monkeypatch):
    secrets.values["CLAUDE_DESIGN_CODE"] = CODE
    run("start", "design")
    run("code", "design")
    meta_path = next((box / ".canopy" / "sign-in").glob("*.json"))
    meta = json.loads(meta_path.read_text())
    meta["expires_at"] = time.time() - 1
    meta_path.write_text(json.dumps(meta))
    assert hook(box, "unrelated").returncode == 0
    assert "designOauth" not in creds(box)
    # The CLI's own sweep does the same to an expired one.
    secrets.values["CLAUDE_DESIGN_CODE"] = CODE
    run("start", "design")
    run("code", "design")
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


def test_the_watcher_hands_the_code_over_by_itself(box, secrets, monkeypatch):
    """`start` leaves a watcher that signs in the moment the secret appears: no turn waits."""
    run("start", "design")
    calls = {"n": 0}

    def later(_s):
        calls["n"] += 1
        if calls["n"] == 3:
            secrets.values["CLAUDE_DESIGN_CODE"] = CODE

    monkeypatch.setattr(sign_in_cli, "SLEEP", later)
    r = run("watch", "design")
    assert r.exit_code == 0, r.output
    assert creds(box)["designOauth"]["accessToken"] == TOKEN
    assert json.loads(run("status", "design", "--json-output").output)["status"] == "signed_in"
    assert CODE not in r.output


def test_the_watcher_gives_up_quietly_when_the_link_lapses(box, secrets, monkeypatch):
    run("start", "design")
    t = {"now": time.time()}
    monkeypatch.setattr(sign_in_cli, "NOW", lambda: t["now"])
    monkeypatch.setattr(sign_in_cli, "SLEEP", lambda s: t.__setitem__("now", t["now"] + 120))
    r = run("watch", "design")
    assert r.exit_code == 0
    assert "designOauth" not in creds(box)


def test_the_watcher_outliving_its_turn_login_leaves_the_sign_in_waiting(box, secrets, monkeypatch):
    """2026-11-06, investigation 34: the turn that ran `start` ended, its canopy-web login was
    revoked, and the watcher's next read got a 401. That must not fail the sign-in (nor leave a
    401 for `status` to print, which credential_guard aborts the next turn on): the next turn's
    `code`, with its own login, hands the code over."""
    run("start", "design")

    def refused(key, **k):
        raise click.ClickException("GET /api/session-secrets/key -> 401: Authentication required")

    monkeypatch.setattr(secret_cli, "list_secrets", refused)
    r = run("watch", "design")
    assert r.exit_code == 0, r.output
    status = run("status", "design").output
    assert "401" not in status and "failed" not in status, status
    assert json.loads(run("status", "design", "--json-output").output)["status"] == "waiting"

    secrets.values["CLAUDE_DESIGN_CODE"] = CODE
    monkeypatch.setattr(secret_cli, "list_secrets",
                        lambda key, **k: [{"name": "CLAUDE_DESIGN_CODE", "last_used_at": None}])
    r = run("code", "design")
    assert r.exit_code == 0, r.output
    assert creds(box)["designOauth"]["accessToken"] == TOKEN


def test_a_used_secret_is_not_taken_again(box, secrets):
    """The code is single-use: an earlier attempt's secret must not fail this sign-in."""
    secrets.values["CLAUDE_DESIGN_CODE"] = CODE
    secrets.used.add("CLAUDE_DESIGN_CODE")
    run("start", "design")
    r = run("code", "design")
    assert r.exit_code != 0 and "not in this chat yet" in r.output
    assert "designOauth" not in creds(box)
