"""provenance: every canopy-web request names its client and, when knowable, the
turn/session that started it — without ever failing the request."""
import importlib.util
import io
import json
import time
from pathlib import Path

import pytest

from orchestrator import canopy_web, provenance as pv

TURN = "3f2b8c1e-0000-4000-8000-000000000001"
SESSION = "5a5a5a5a-0000-4000-8000-000000000002"
CLAUDE = "0e1f0e1f-0000-4000-8000-000000000003"
TASK = "c-please-deploy"
CWD = f"/Users/a/emdash/worktrees/hal-0ceb29c5/emdash-{TASK}-x7o1w"

_ENV_VARS = ("CANOPY_TURN_ID", "CANOPY_SESSION_ID", "CANOPY_EMDASH_TASK", "CANOPY_CALLER",
             "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


@pytest.fixture()
def root(tmp_path, monkeypatch):
    for k in _ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    r = tmp_path / "caller"
    (r / "by-task").mkdir(parents=True)
    monkeypatch.setattr(pv, "CALLER_ROOT", r)
    return r


def test_nothing_known_sends_only_the_client_headers(root, tmp_path):
    h = pv.provenance_headers(cwd=str(tmp_path))
    assert h[pv.HEADER_CLIENT].startswith("canopy-cli/")
    assert h["User-Agent"].startswith("canopy-cli/")
    for header in (pv.HEADER_TURN, pv.HEADER_SESSION, pv.HEADER_TASK, pv.HEADER_CLAUDE):
        assert header not in h


def test_env_wins(root, monkeypatch):
    (root / "by-task" / f"{TASK}.json").write_text(json.dumps(
        {"turn_id": "11111111-0000-4000-8000-000000000000", "session_id": SESSION}))
    monkeypatch.setenv("CANOPY_TURN_ID", TURN)
    monkeypatch.setenv("CANOPY_SESSION_ID", SESSION)
    monkeypatch.setenv("CANOPY_EMDASH_TASK", "cloud-task")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)
    h = pv.provenance_headers(cwd=CWD)
    assert h[pv.HEADER_TURN] == TURN
    assert h[pv.HEADER_SESSION] == SESSION
    assert h[pv.HEADER_TASK] == "cloud-task"
    assert h[pv.HEADER_CLAUDE] == CLAUDE


def test_caller_envelope_fills_the_parent(root, monkeypatch, tmp_path):
    env = tmp_path / "env.json"
    env.write_text(json.dumps({"turn_id": TURN, "conversation": {"session_id": SESSION}}))
    monkeypatch.setenv("CANOPY_CALLER", str(env))
    p = pv.resolve_parent(cwd=str(tmp_path))
    assert (p["turn_id"], p["session_id"]) == (TURN, SESSION)


def test_by_task_record_is_found_from_the_worktree_path(root):
    (root / "by-task" / f"{TASK}.json").write_text(json.dumps(
        {"turn_id": TURN, "session_id": SESSION, "claude_session_id": CLAUDE}))
    h = pv.provenance_headers(cwd=CWD + "/src/sub")
    assert h[pv.HEADER_TURN] == TURN
    assert h[pv.HEADER_SESSION] == SESSION
    assert h[pv.HEADER_CLAUDE] == CLAUDE
    assert h[pv.HEADER_TASK] == TASK


def test_unsafe_values_are_dropped_not_sent(root, monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_TURN_ID", "not a uuid\r\nX-Evil: 1")
    assert pv.HEADER_TURN not in pv.provenance_headers(cwd=str(tmp_path))


def test_a_broken_resolver_never_fails_the_request(root, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(pv, "resolve_parent", boom)
    h = pv.provenance_headers()
    assert pv.HEADER_CLIENT in h and pv.HEADER_TURN not in h


def test_canopy_web_call_attaches_provenance_and_the_bearer_wins(root, monkeypatch):
    monkeypatch.setenv("CANOPY_TURN_ID", TURN)
    seen = {}

    def fake(method, url, headers, data):
        seen.update(headers)
        return 200, "{}"
    canopy_web.call("GET", "/api/x", base_url="https://x.test", token="t", transport=fake,
                    headers={"Authorization": "Bearer spoof"})
    assert seen[pv.HEADER_TURN] == TURN
    assert seen[pv.HEADER_CLIENT].startswith("canopy-cli/")
    assert seen["Authorization"] == "Bearer t"


def test_with_provenance_keeps_the_callers_own_keys(root):
    h = pv.with_provenance({"User-Agent": "mine", "Authorization": "Bearer t"})
    assert h["User-Agent"] == "mine" and pv.HEADER_CLIENT in h


# ── the laptop half: the UserPromptSubmit hook leaves the by-task record ────────
_HOOK = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks/caller_context.py"
_SPEC = importlib.util.spec_from_file_location("caller_context_pv", _HOOK)
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)


def test_hook_claim_writes_a_record_the_cli_then_reads(root, monkeypatch, capsys):
    (root / "pending").mkdir()
    envelope = root / f"{TURN}.json"
    envelope.write_text(json.dumps({"version": 2, "turn_id": TURN, "relationship": "owner",
                                    "conversation": {"session_id": SESSION}}))
    (root / "pending" / f"{TASK}.json").write_text(json.dumps(
        {"version": 1, "turn_id": TURN, "task": TASK, "envelope": str(envelope),
         "written_at": time.time()}))
    monkeypatch.setattr(cc, "CALLER_ROOT", str(root))
    tp = f"/Users/a/.claude/projects/-Users-a-emdash-worktrees-hal-0ceb29c5-emdash-{TASK}-x7o1w/s.jsonl"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"hook_event_name": "UserPromptSubmit", "transcript_path": tp, "cwd": CWD,
         "session_id": CLAUDE, "prompt": "go"})))
    assert cc.main() == 0
    assert "additionalContext" in capsys.readouterr().out
    rec_path = root / "by-task" / f"{TASK}.json"
    assert oct(rec_path.stat().st_mode & 0o777) == "0o600"
    rec = json.loads(rec_path.read_text())
    assert (rec["turn_id"], rec["session_id"], rec["claude_session_id"]) == (TURN, SESSION, CLAUDE)
    p = pv.resolve_parent(cwd=CWD)
    assert (p["turn_id"], p["session_id"], p["claude_session_id"], p["task"]) == (
        TURN, SESSION, CLAUDE, TASK)
