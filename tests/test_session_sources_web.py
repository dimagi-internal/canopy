"""Tests for the `canopy-web` session source (`session_sources.collect_web_transcripts`).

WHY: an agent on the cloud runner (echo, on `cloud-ec2-1`) writes its transcripts on
that box, never in any local `~/.claude/projects`, so `agent-review echo` read a whole
local corpus and attributed zero turns. canopy-web retains those transcripts; this
adapter fetches them into a local cache and hands back JSONL paths. Every test runs
against a FAKE HTTP layer: no network, no real PAT.
"""
import datetime as dt
import json
from pathlib import Path

import pytest

from orchestrator import canopy_web
from orchestrator import session_sources as ss
from orchestrator.session_sources import (
    SessionSource,
    collect_web_transcripts,
    corpus_confidence,
    discover_local_sources,
    session_sources,
)

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_B = "bbbbbbbb-0000-4000-8000-000000000002"
SID_C = "cccccccc-0000-4000-8000-000000000003"


def _web():
    return SessionSource(name="canopy-web:test", kind="canopy-web",
                         location="https://web.test/canopy", readable=True)


def _turn(tid, *, hours_ago=1, status="done", has_transcript=True, cli="", key="",
          chat=None, hidden=False):
    when = (NOW - dt.timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
    return {"id": tid, "status": status, "has_transcript": has_transcript,
            "cli_session_id": cli, "session_key": key, "chat_session_id": chat,
            "content_hidden": hidden, "created_at": when, "started_at": when,
            "ended_at": when if status == "done" else None}


def _stream_json(sid, calls):
    """A `claude -p` stream-json transcript: `session_id`, no `timestamp`."""
    lines = []
    for i, (tool, result, is_error) in enumerate(calls):
        lines.append({"type": "assistant", "session_id": sid, "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": tool, "input": {}}]}})
        lines.append({"type": "user", "session_id": sid, "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": result,
             "is_error": is_error}]}})
    return "".join(json.dumps(l) + "\n" for l in lines)


class FakeWeb:
    """Records every request; serves turns, raw transcripts and chat sessions."""

    def __init__(self, turns, transcripts=None, sessions=None, fail_list=None,
                 fail_transcript=()):
        self.turns, self.transcripts = turns, transcripts or {}
        self.sessions = sessions or {}
        self.fail_list, self.fail_transcript = fail_list, set(fail_transcript)
        self.requests = []

    def call(self, method, path, body=None, **kw):
        self.requests.append(path)
        assert method == "GET"
        if "/turns/?limit=" in path:
            if self.fail_list:
                raise canopy_web.CanopyError(self.fail_list)
            return {"items": self.turns, "total": len(self.turns)}
        if path.startswith("/api/canopy-sessions/"):
            return self.sessions[path.split("/")[3].split("?")[0]]
        raise AssertionError(f"unexpected JSON GET {path}")

    def get_text(self, method, path, **kw):
        self.requests.append(path)
        tid = path.split("/")[4]
        if tid in self.fail_transcript:
            raise canopy_web.CanopyError(f"GET {path} -> 500: boom")
        return self.transcripts.get(tid, "")


def _collect(fake, tmp_path, local_ids=(), sources=None):
    sources = sources if sources is not None else [_web()]
    web = collect_web_transcripts(sources, "echo", 72, local_session_ids=set(local_ids),
                                  call=fake.call, get_text=fake.get_text,
                                  cache_dir=tmp_path / "cache", now=NOW)
    return web, sources


# --- discovery -------------------------------------------------------------------

def test_default_set_includes_canopy_web_when_a_pat_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_SESSION_WEB", raising=False)
    monkeypatch.setattr(canopy_web, "resolve_token", lambda t: "pat")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://web.test/canopy")
    srcs = session_sources(config_path=tmp_path / "none.json", users_root=str(tmp_path))
    web = [s for s in srcs if s.kind == "canopy-web"]
    assert [s.name for s in web] == ["canopy-web:web.test"]
    assert web[0].readable and web[0].location == "https://web.test/canopy"


def test_default_set_omits_canopy_web_without_a_pat(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_SESSION_WEB", raising=False)

    def _no_pat(t):
        raise RuntimeError("no canopy-web PAT")
    monkeypatch.setattr(canopy_web, "resolve_token", _no_pat)
    srcs = session_sources(config_path=tmp_path / "none.json", users_root=str(tmp_path))
    assert not [s for s in srcs if s.kind == "canopy-web"]


def test_env_switch_keeps_canopy_web_out(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_SESSION_WEB", "0")
    monkeypatch.setattr(canopy_web, "resolve_token", lambda t: "pat")
    srcs = session_sources(config_path=tmp_path / "none.json", users_root=str(tmp_path))
    assert not [s for s in srcs if s.kind == "canopy-web"]


def test_configured_canopy_web_without_pat_is_loud_not_dropped(tmp_path, monkeypatch):
    def _no_pat(t):
        raise RuntimeError("no canopy-web PAT — run /canopy:canopy-web-pat-mint")
    monkeypatch.setattr(canopy_web, "resolve_token", _no_pat)
    cfg = tmp_path / "session-sources.json"
    cfg.write_text(json.dumps({"sources": [{"kind": "canopy-web"}]}))
    srcs = session_sources(config_path=cfg, users_root=str(tmp_path))
    assert len(srcs) == 1 and srcs[0].kind == "canopy-web"
    assert srcs[0].readable is False and "PAT" in srcs[0].reason
    assert corpus_confidence(srcs) == "half-blind"


def test_linux_box_falls_back_to_own_home(tmp_path):
    """The cloud runner has no /Users: its own ~/.claude/projects must still count."""
    home = tmp_path / "home" / "ubuntu"
    (home / ".claude" / "projects").mkdir(parents=True)
    srcs = discover_local_sources(users_root=str(tmp_path / "no-users"), home=home)
    assert [(s.name, s.readable) for s in srcs] == [("local:ubuntu", True)]


# --- fetching --------------------------------------------------------------------

def test_fetches_window_turns_and_normalizes_to_local_shape(tmp_path):
    fake = FakeWeb([_turn("t1", cli=SID_A), _turn("old", hours_ago=100, cli=SID_B)],
                   transcripts={"t1": _stream_json(SID_A, [("Bash", "ok", False)])})
    web, srcs = _collect(fake, tmp_path)
    assert len(web.transcripts) == 1
    path = web.transcripts[0]
    assert path.stem == SID_A                         # friction_signals' session_id
    entries = [json.loads(l) for l in path.read_text().splitlines()]
    assert entries[0]["type"] == "canopy-web-provenance"
    assert all(e.get("sessionId") == SID_A for e in entries[1:])
    assert all(e.get("timestamp") for e in entries[1:])
    st = web.stats["canopy-web:test"]
    assert st["turns_in_window"] == 1 and st["fetched"] == 1
    assert srcs[0].readable is True
    assert not any("/old/" in r for r in fake.requests)   # out of window: never fetched


def test_finished_turn_is_served_from_cache_on_the_next_review(tmp_path):
    turns = [_turn("t1", cli=SID_A)]
    fake = FakeWeb(turns, transcripts={"t1": _stream_json(SID_A, [("Bash", "ok", False)])})
    _collect(fake, tmp_path)
    fake2 = FakeWeb(turns, transcripts={})            # a refetch would now come back empty
    web, _ = _collect(fake2, tmp_path)
    assert len(web.transcripts) == 1
    assert web.stats["canopy-web:test"]["cached"] == 1
    assert not any("/transcript" in r for r in fake2.requests)


def test_running_turn_is_never_cached_and_reads_as_live(tmp_path):
    turns = [_turn("t1", status="running", cli=SID_A)]
    fake = FakeWeb(turns, transcripts={"t1": _stream_json(SID_A, [("Bash", "ok", False)])})
    web, _ = _collect(fake, tmp_path)
    assert str(web.transcripts[0]) in web.running
    assert "live" in web.transcripts[0].parts
    fake2 = FakeWeb(turns, transcripts={"t1": _stream_json(SID_A, [("Bash", "ok", False)])})
    _collect(fake2, tmp_path)
    assert any("/transcript" in r for r in fake2.requests)   # refetched, still growing


def test_session_already_on_local_disk_is_not_fetched(tmp_path):
    fake = FakeWeb([_turn("t1", cli=SID_A), _turn("t2", key=SID_B)],
                   transcripts={"t1": "x", "t2": "x"})
    web, _ = _collect(fake, tmp_path, local_ids={SID_A, SID_B})
    assert web.transcripts == []
    assert web.stats["canopy-web:test"]["already_local"] == 2
    assert not any("/transcript" in r for r in fake.requests)


def test_dedup_on_the_transcripts_own_session_id(tmp_path):
    """No cli_session_id on the turn row — the transcript itself names the session."""
    fake = FakeWeb([_turn("t1")],
                   transcripts={"t1": _stream_json(SID_C, [("Bash", "ok", False)])})
    web, _ = _collect(fake, tmp_path, local_ids={SID_C})
    assert web.transcripts == []
    assert web.stats["canopy-web:test"]["already_local"] == 1


def test_chat_session_fallback_when_no_turn_transcript(tmp_path):
    """Recent cloud turns keep no turn transcript but link the chat session the runner
    drove; its parsed messages are rebuilt into JSONL the extractors can read."""
    from orchestrator.agent_review import friction_signals

    session = {"session_key": SID_A, "messages": [
        {"role": "user", "plaintext": "/echo:turn", "content": {}, "created_at": "2026-10-03T11:00:00Z"},
        {"role": "assistant", "plaintext": "Reading the procedure.", "content": {}, "created_at": "2026-10-03T11:00:01Z"},
        {"role": "tool_use", "plaintext": "", "content": {"id": "tu1", "name": "Bash", "input": {"command": "x"}}},
        {"role": "tool_result", "plaintext": "Traceback: boom", "content": {"tool_use_id": "tu1", "is_error": True}},
    ]}
    fake = FakeWeb([_turn("t1", has_transcript=False, key=SID_A, chat="chat-1")],
                   sessions={"chat-1": session})
    web, _ = _collect(fake, tmp_path)
    assert len(web.transcripts) == 1
    sig = friction_signals(web.transcripts[0])
    assert sig["session_id"] == SID_A
    assert len(sig["failures"]) == 1 and sig["failures"][0]["tool"] == "Bash"


def test_turn_with_nothing_retained_is_counted_not_hidden(tmp_path):
    fake = FakeWeb([_turn("t1", has_transcript=False)])
    web, srcs = _collect(fake, tmp_path)
    assert web.transcripts == []
    assert web.stats["canopy-web:test"]["no_transcript"] == ["t1"]


def test_listing_failure_degrades_loud(tmp_path):
    fake = FakeWeb([], fail_list="GET /api/agents/echo/turns/ -> 404: not found")
    web, srcs = _collect(fake, tmp_path)
    assert web.transcripts == []
    assert srcs[0].readable is False and "404" in srcs[0].reason
    assert corpus_confidence(srcs) == "half-blind"


def test_a_failed_transcript_fetch_makes_the_source_partial(tmp_path):
    fake = FakeWeb([_turn("t1", cli=SID_A), _turn("t2", cli=SID_B)],
                   transcripts={"t1": _stream_json(SID_A, [("Bash", "ok", False)])},
                   fail_transcript={"t2"})
    web, srcs = _collect(fake, tmp_path)
    assert len(web.transcripts) == 1                       # the good one still lands
    assert srcs[0].readable is False and srcs[0].reason.startswith("partial:")
    assert corpus_confidence(srcs) == "half-blind"


def test_permission_hidden_turns_make_the_source_partial(tmp_path):
    fake = FakeWeb([_turn("t1", cli=SID_A, hidden=True)])
    web, srcs = _collect(fake, tmp_path)
    assert web.stats["canopy-web:test"]["hidden"] == ["t1"]
    assert srcs[0].readable is False and "LOGS_READ" in srcs[0].reason


def test_unreadable_or_local_sources_are_not_queried(tmp_path):
    fake = FakeWeb([_turn("t1", cli=SID_A)])
    dead = _web()
    dead.readable, dead.reason = False, "no PAT"
    local = SessionSource(name="local:x", kind="local", location=str(tmp_path), readable=True)
    web, _ = _collect(fake, tmp_path, sources=[dead, local])
    assert fake.requests == [] and web.stats == {}


# --- wiring: agent-review ---------------------------------------------------------

def test_run_review_reads_cloud_turns_and_dedups_local_copies(tmp_path, monkeypatch):
    """The echo case: one turn ran on this laptop (local copy), two on the cloud
    runner. The review must see all three sessions exactly once."""
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    root = tmp_path / "home" / ".claude" / "projects"
    d = root / "-Users-x-emdash-repositories-echo"
    d.mkdir(parents=True)
    (d / f"{SID_A}.jsonl").write_text(json.dumps({
        "type": "assistant", "cwd": str(repo),
        "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n")

    now = dt.datetime.now(dt.timezone.utc)
    recent = (now - dt.timedelta(hours=1)).isoformat()
    turns = [
        {"id": "local1", "status": "done", "has_transcript": True, "cli_session_id": SID_A,
         "session_key": "", "created_at": recent, "ended_at": recent},
        {"id": "cloud1", "status": "done", "has_transcript": True, "cli_session_id": SID_B,
         "session_key": SID_B, "created_at": recent, "ended_at": recent},
        {"id": "cloud2", "status": "done", "has_transcript": True, "cli_session_id": SID_C,
         "session_key": SID_C, "created_at": recent, "ended_at": recent},
    ]
    fake = FakeWeb(turns, transcripts={
        "cloud1": _stream_json(SID_B, [("Bash", "ok", False)]),
        "cloud2": _stream_json(SID_C, [("Bash", "Traceback: boom", True)]),
    })
    monkeypatch.setattr(canopy_web, "call", fake.call)
    monkeypatch.setattr(canopy_web, "call_text", fake.get_text)
    monkeypatch.setattr(ss, "WEB_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("orchestrator.agent_review.session_sources", lambda *a, **k: [
        SessionSource(name="local:x", kind="local", location=str(root), readable=True),
        _web(),
    ])

    res = ar.run_review(str(repo), hours=24, use_llm=False)
    sids = sorted(s["session_id"] for s in res["signals"])
    assert sids == sorted([SID_A, SID_B, SID_C])
    assert res["corpus"]["confidence"] == "whole-corpus"
    assert "canopy-web:test" in res["corpus"]["sources"]
    assert res["corpus"]["web"]["canopy-web:test"]["already_local"] == 1
    assert not any("/local1/" in r for r in fake.requests)
    assert sum(len(s["failures"]) for s in res["signals"]) == 1
    # A finished cloud turn is finished, however recently its cache file was written.
    assert not any(s["in_progress"] for s in res["signals"] if s["session_id"] != SID_A)


def test_run_review_half_blind_when_canopy_web_is_down(tmp_path, monkeypatch):
    from orchestrator import agent_review as ar

    repo = tmp_path / "repositories" / "echo"
    (repo / "skills").mkdir(parents=True)
    root = tmp_path / "home" / ".claude" / "projects"
    root.mkdir(parents=True)

    def _down(*a, **k):
        raise canopy_web.CanopyError("GET /api/agents/echo/turns/ -> 502: bad gateway")
    monkeypatch.setattr(canopy_web, "call", _down)
    monkeypatch.setattr(ss, "WEB_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("orchestrator.agent_review.session_sources", lambda *a, **k: [
        SessionSource(name="local:x", kind="local", location=str(root), readable=True),
        _web(),
    ])
    res = ar.run_review(str(repo), hours=24, use_llm=False)
    assert res["corpus"]["confidence"] == "half-blind"
    assert res["corpus"]["unreadable"] == ["canopy-web:test"]
    assert "502" in res["corpus"]["unreadable_reasons"]["canopy-web:test"]
