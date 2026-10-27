"""credential_guard: a tool failing on the agent's credentials ends the turn, and nothing more runs.

Evidence (2026-10-27, ACE turn 599fad3b on cloud-ec2-2): `resolve_opp_path` returned the
error below as a SUCCESSFUL result; ACE emailed the outside requester that "this runner has no
logins", marked her thread read, and the turn finished `done` with nothing alarmed.
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks"
_SPEC = importlib.util.spec_from_file_location("credential_guard", _ROOT / "credential_guard.py")
g = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(g)

SID = "082b8d26-9645-4d4a-a03e-27062aa5533f"
TURN = "599fad3b-c647-4ace-b3e2-205573aa18d0"
DRIVE = "mcp__plugin_ace_ace-gdrive__resolve_opp_path"
ACE_ERROR = ("Error: Method doesn't allow unregistered callers (callers without established "
             "identity). Please use API Key or other form of API consumer identity to call this API.")


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    caller, locks = tmp_path / "caller", tmp_path / "locks"
    monkeypatch.setattr(g, "CALLER_ROOT", str(caller))
    monkeypatch.setattr(g, "LOCK_ROOT", str(locks))
    (caller / "presence").mkdir(parents=True)
    return caller, locks


def _in_turn(caller, turn=TURN):
    (caller / "presence" / f"{SID}.json").write_text(
        json.dumps({"origin": "email", "present": False, "turn_id": turn, "updated_at": 0}))


def _run(monkeypatch, capsys, data, *argv):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": SID, **data})))
    assert g.main(list(argv)) == 0
    out = capsys.readouterr().out
    return json.loads(out) if out else None


def _post(tool=DRIVE, response=ACE_ERROR, **extra):
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_response": response, **extra}


@pytest.fixture()
def aborts(monkeypatch):
    calls = []

    def fake(turn_id, tool, reason):
        calls.append((turn_id, tool, reason))
        return True
    monkeypatch.setattr(g, "run_abort", fake)
    return calls


# ── after a tool ───────────────────────────────────────────────────────────────────────


def test_aces_drive_failure_aborts_the_turn_and_stops_claude(roots, monkeypatch, capsys, aborts):
    caller, locks = roots
    _in_turn(caller)
    out = _run(monkeypatch, capsys, _post(response=[{"type": "text", "text": ACE_ERROR}]))

    assert out["continue"] is False and "Canopy aborted this turn" in out["stopReason"]
    [(turn, tool, reason)] = aborts
    assert turn == TURN and tool == DRIVE and "unregistered callers" in reason
    assert (locks / f"{TURN}.json").exists()


def test_not_filed_still_locks_and_tells_claude_to_stop(roots, monkeypatch, capsys):
    caller, locks = roots
    _in_turn(caller)
    monkeypatch.setattr(g, "run_abort", lambda *a: False)
    out = _run(monkeypatch, capsys, _post())
    assert out["decision"] == "block" and "canopy turn abort --turn" in out["reason"]
    assert json.loads((locks / f"{TURN}.json").read_text())["filed"] is False


def test_outside_a_canopy_turn_nothing_happens(roots, monkeypatch, capsys, aborts):
    assert _run(monkeypatch, capsys, _post()) is None
    assert aborts == []


def test_the_owner_at_the_keyboard_is_not_a_turn(roots, monkeypatch, capsys, aborts):
    caller, _ = roots
    (caller / "presence" / f"{SID}.json").write_text(
        json.dumps({"origin": "keyboard", "present": True, "turn_id": "", "updated_at": 0}))
    assert _run(monkeypatch, capsys, _post()) is None and aborts == []


def test_only_once_per_turn(roots, monkeypatch, capsys, aborts):
    caller, _ = roots
    _in_turn(caller)
    _run(monkeypatch, capsys, _post())
    assert _run(monkeypatch, capsys, _post()) is None
    assert len(aborts) == 1


@pytest.mark.parametrize("error", [
    ACE_ERROR,
    "google.auth.exceptions.RefreshError: ('invalid_grant: Token has been expired or revoked.')",
    "Error: Request had invalid authentication credentials. Expected OAuth 2 access token",
    "HTTP 401 Unauthorized",
    "Error: invalid api key",
    "Error: ENOENT: no such file or directory, open '/home/x/.claude/plugins/data/ace/gws-sa-key.json'",
    "grpc: UNAUTHENTICATED",
])
def test_credential_failures(error):
    assert g.credential_failure(_post(response=error))


@pytest.mark.parametrize("error", [
    "Error: 403 Forbidden: The caller does not have permission",   # access to one resource
    "Error: File not found: 1AbC",
    "Error: 429 Too Many Requests",
    "Connection reset by peer",
])
def test_not_credential_failures(error):
    assert not g.credential_failure(_post(response=error))


def test_a_long_read_that_quotes_an_old_error_is_not_a_failure():
    """Reading ACE's own turn events (48 KB) must not abort the reader's turn."""
    events = json.dumps({"events": [{"payload": {"content": "x" * 3000}},
                                    {"payload": {"content": ACE_ERROR}}]})
    assert not g.credential_failure(_post(tool="mcp__plugin_canopy_canopy-web__read_turn_events",
                                          response=events))


def test_a_short_result_mentioning_it_late_is_not_a_failure():
    text = "Here is the incident summary you asked for. " * 12 + "It said: unregistered callers."
    assert len(text) < g.SHORT_RESULT and not g.credential_failure(_post(response=text))


def test_a_failed_mcp_call_is_judged_whatever_its_length():
    data = {"hook_event_name": "PostToolUseFailure", "tool_name": DRIVE,
            "error": "y" * 3000 + "\n" + ACE_ERROR}
    assert g.credential_failure(data)


def test_a_sign_in_tool_is_never_a_failure():
    assert not g.credential_failure(_post(tool="mcp__claude_ai_Gmail__authenticate",
                                          response="Authentication required: open this link"))


def test_a_failed_credentialed_bash_call():
    data = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "cd /w && bin/ace-email --thread-id 1 --body-file b.txt"},
            "error": "Exit code 1\ngog: oauth2: \"invalid_grant\" \"Token has been expired or revoked.\""}
    assert g.credential_failure(data)


def test_a_piped_send_that_exits_0_is_still_caught():
    data = _post(tool="Bash", tool_input={"command": "canopy email send --to x 2>&1 | tail -2"},
                 response={"stdout": "Error: oauth2: invalid_grant", "stderr": ""})
    assert g.credential_failure(data)


def test_grepping_for_the_words_is_not_a_failure():
    data = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "grep -rn invalid_grant src/ && false"},
            "error": "src/auth.py:12: if 'invalid_grant' in err:"}
    assert not g.credential_failure(data)


def test_reading_an_email_that_quotes_it_is_not_a_failure():
    data = _post(tool="Bash", tool_input={"command": "canopy email read 1a17"},
                 response={"stdout": '{"body_text": "the log said invalid_grant yesterday"}'})
    assert not g.credential_failure(data)


# ── before a tool ──────────────────────────────────────────────────────────────────────


def _lock(locks, filed=True):
    locks.mkdir(exist_ok=True)
    (locks / f"{TURN}.json").write_text(json.dumps(
        {"turn_id": TURN, "reason": f"{DRIVE} failed on the agent's credentials: {ACE_ERROR}",
         "tool": DRIVE, "filed": filed, "at": __import__("time").time()}))


def _pre(tool, **tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}


def test_a_locked_turn_sends_nothing(roots, monkeypatch, capsys):
    caller, locks = roots
    _in_turn(caller)
    _lock(locks)
    out = _run(monkeypatch, capsys, _pre("Bash", command="bin/ace-email --to sophie"), "--pre")
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "aborted" in decision["permissionDecisionReason"]
    assert "Do not tell any counterpart" in decision["permissionDecisionReason"]
    for tool, inp in (("Bash", {"command": "bin/ace-mark-read 1a17"}),
                      ("mcp__plugin_canopy_canopy-web__send_mail", {}), ("Read", {"file_path": "x"})):
        assert _run(monkeypatch, capsys, _pre(tool, **inp), "--pre")


def test_the_abort_itself_is_allowed(roots, monkeypatch, capsys):
    caller, locks = roots
    _in_turn(caller)
    _lock(locks, filed=False)
    cmd = f'cd /w && canopy turn abort --turn {TURN} --reason "a; b"'
    assert _run(monkeypatch, capsys, _pre("Bash", command=cmd), "--pre") is None
    sneaky = f"canopy turn abort --turn {TURN} --reason x; bin/ace-email --to sophie"
    assert _run(monkeypatch, capsys, _pre("Bash", command=sneaky), "--pre")


def test_the_next_turn_is_not_locked(roots, monkeypatch, capsys):
    caller, locks = roots
    _lock(locks)
    _in_turn(caller, turn="65ae5f2f-f610-4365-a45a-0923d9ba3b0f")
    assert _run(monkeypatch, capsys, _pre("Bash", command="bin/ace-email"), "--pre") is None


def test_an_old_lock_expires(roots, monkeypatch, capsys):
    caller, locks = roots
    _in_turn(caller)
    _lock(locks)
    rec = json.loads((locks / f"{TURN}.json").read_text())
    rec["at"] -= g.LOCK_MAX_AGE + 1
    (locks / f"{TURN}.json").write_text(json.dumps(rec))
    assert _run(monkeypatch, capsys, _pre("Bash", command="bin/ace-email"), "--pre") is None


def test_registered_for_every_tool_and_both_post_events():
    hooks = json.loads((_ROOT / "hooks.json").read_text())["hooks"]
    pre = [h for e in hooks["PreToolUse"] for h in e["hooks"] if "credential_guard" in h["command"]]
    assert pre and "--pre" in pre[0]["command"] and "--guard" in pre[0]["command"]
    assert [e["matcher"] for e in hooks["PreToolUse"] if "credential_guard" in
            e["hooks"][0]["command"]] == ["*"]
    for event in ("PostToolUse", "PostToolUseFailure"):
        [entry] = [e for e in hooks[event] if "credential_guard" in e["hooks"][0]["command"]]
        assert entry["matcher"] == "mcp__.*|Bash"
        assert entry["hooks"][0]["timeout"] > g.ABORT_TIMEOUT
