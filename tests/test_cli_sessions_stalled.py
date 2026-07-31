import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from orchestrator.cli import main
from orchestrator.session_stall import StallVerdict
from orchestrator.stall_backtest import BacktestCase
from orchestrator.stall_judge import AWAITING_CONTINUE, GATE_OUTBOUND

# Timestamps must be RELATIVE to now, not hardcoded calendar dates: `sessions
# stalled` drops anything older than --hours (24h default), so fixed past
# dates silently age out of the window and every assertion sees an empty
# list with no code regression at all. Follows
# tests/test_cli_sessions_project_filter.py's approach.
_NOW = datetime.now(timezone.utc)
_FIRST_TS = (_NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
_LAST_TS = (_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_transcript(path: Path, stop_reason: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "message": {"content": "do the thing"},
         "timestamp": _FIRST_TS, "sessionId": path.stem, "cwd": "/tmp/proj"},
        {"type": "assistant",
         "message": {"stop_reason": stop_reason,
                     "content": [{"type": "text", "text": text}]},
         "timestamp": _LAST_TS, "sessionId": path.stem, "cwd": "/tmp/proj"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _fake_classify(verdicts):
    def fake(records_by_id, *, runner=None, model="haiku", batch_size=30,
              retries=2, stats=None):
        return {sid: verdicts[sid] for sid, _ in records_by_id}
    return fake


def test_stalled_reports_a_waiting_session(tmp_path, monkeypatch):
    _write_transcript(tmp_path / ".claude" / "projects" / "-tmp-proj" / "sess-a.jsonl",
                      "end_turn", "Done seeding. Next I'll re-render.")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "orchestrator.session_stall.classify_sessions",
        _fake_classify({"sess-a": StallVerdict(
            "stalled", AWAITING_CONTINUE, 0.8, "stated next step", "end_turn")}))

    res = CliRunner().invoke(main, ["sessions", "stalled", "--json-output"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)
    assert len(rows) == 1
    assert rows[0]["class"] == AWAITING_CONTINUE
    assert rows[0]["auto_send"] is True
    assert rows[0]["reason"] == "stated next step"


def test_stalled_omits_a_working_session(tmp_path, monkeypatch):
    _write_transcript(tmp_path / ".claude" / "projects" / "-tmp-proj" / "sess-b.jsonl",
                      "tool_use", "checking now")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "orchestrator.session_stall.classify_sessions",
        _fake_classify({"sess-b": StallVerdict("working", "working", 1.0, "", "tool_use")}))

    res = CliRunner().invoke(main, ["sessions", "stalled", "--json-output"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == []


def test_gate_outbound_is_reported_but_not_auto_send(tmp_path, monkeypatch):
    _write_transcript(tmp_path / ".claude" / "projects" / "-tmp-proj" / "sess-c.jsonl",
                      "end_turn", "Approve and I'll send it to the partner.")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "orchestrator.session_stall.classify_sessions",
        _fake_classify({"sess-c": StallVerdict(
            "stalled", GATE_OUTBOUND, 0.9, "wants send approval", "end_turn")}))

    res = CliRunner().invoke(main, ["sessions", "stalled", "--json-output"])
    rows = json.loads(res.output)
    assert rows[0]["class"] == GATE_OUTBOUND
    assert rows[0]["auto_send"] is False


def test_multiple_stalled_sessions_are_batched_and_correctly_keyed(tmp_path, monkeypatch):
    """Guards two regressions byte-identical output on a single-session fixture
    can't catch: (1) looping and calling `classify_sessions` once per session
    instead of once for the whole batch, and (2) a verdict-keying bug that
    always reads index [0] instead of indexing by session id.

    Two sessions, two DIFFERENT classes, a spy fake that records call count
    and the exact `records_by_id` it received.
    """
    _write_transcript(tmp_path / ".claude" / "projects" / "-tmp-proj" / "sess-d.jsonl",
                      "end_turn", "Done seeding. Next I'll re-render.")
    _write_transcript(tmp_path / ".claude" / "projects" / "-tmp-proj" / "sess-e.jsonl",
                      "end_turn", "Approve and I'll send it to the partner.")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    calls = []

    def spy(records_by_id, *, runner=None, model="haiku", batch_size=30,
            retries=2, stats=None):
        calls.append(records_by_id)
        verdicts = {
            "sess-d": StallVerdict("stalled", AWAITING_CONTINUE, 0.8, "stated next step", "end_turn"),
            "sess-e": StallVerdict("stalled", GATE_OUTBOUND, 0.9, "wants send approval", "end_turn"),
        }
        return {sid: verdicts[sid] for sid, _ in records_by_id}

    monkeypatch.setattr("orchestrator.session_stall.classify_sessions", spy)

    res = CliRunner().invoke(main, ["sessions", "stalled", "--json-output"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)

    # Batching: exactly one call, and that one call carried both session ids.
    assert len(calls) == 1, "classify_sessions must be called exactly once for the whole batch"
    called_ids = {sid for sid, _ in calls[0]}
    assert called_ids == {"sess-d", "sess-e"}

    # Keying: each row's class/reason lands on the correct session_id, not
    # index [0] for everything.
    by_id = {r["session_id"]: r for r in rows}
    assert len(rows) == 2
    assert by_id["sess-d"]["class"] == AWAITING_CONTINUE
    assert by_id["sess-d"]["reason"] == "stated next step"
    assert by_id["sess-d"]["auto_send"] is True
    assert by_id["sess-e"]["class"] == GATE_OUTBOUND
    assert by_id["sess-e"]["reason"] == "wants send approval"
    assert by_id["sess-e"]["auto_send"] is False


def test_backtest_scores_transcripts(tmp_path, monkeypatch):
    # NOTE: fixtures live under `.claude/projects`, matching scan_all_transcripts'
    # actual discovery root (Path.home() / ".claude" / "projects") and every
    # other fixture in this file — the brief's version of this test omitted
    # `.claude`, which would leave `n == 0` regardless of implementation.
    projects = tmp_path / ".claude" / "projects" / "-tmp-proj"
    projects.mkdir(parents=True)
    rows = [
        {"type": "assistant", "sessionId": "s1", "cwd": "/tmp/proj",
         "timestamp": "2026-07-30T10:00:00Z",
         "message": {"stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "Done. Next I'll re-render."}]}},
        {"type": "user", "sessionId": "s1", "cwd": "/tmp/proj",
         "timestamp": "2026-07-30T10:01:00Z", "message": {"content": "keep going"}},
    ]
    (projects / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "orchestrator.stall_backtest.grade",
        lambda hbs, **kw: [BacktestCase(AWAITING_CONTINUE, True, True,
                                        hbs[0].tail, hbs[0].reply)])

    res = CliRunner().invoke(main, ["sessions", "stall-backtest", "--json-output"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["overall"]["n"] == 1
    assert out["overall"]["tp"] == 1
    assert out["overall"]["precision"] == 1.0


def test_backtest_with_no_handbacks_exits_clean(tmp_path, monkeypatch):
    projects = tmp_path / ".claude" / "projects" / "-tmp-proj"
    projects.mkdir(parents=True)
    (projects / "s2.jsonl").write_text(json.dumps(
        {"type": "assistant", "sessionId": "s2", "cwd": "/tmp/proj",
         "timestamp": "2026-07-30T10:00:00Z",
         "message": {"stop_reason": "tool_use",
                     "content": [{"type": "text", "text": "working"}]}}) + "\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    res = CliRunner().invoke(main, ["sessions", "stall-backtest", "--json-output"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["overall"]["n"] == 0


def test_backtest_limit_keeps_the_time_newest_handback_not_alphabetical(tmp_path, monkeypatch):
    # Finding 1 regression guard: scan_all_transcripts orders sessions
    # alphabetically by project-dir then session-file name (session files
    # are named by UUID, unrelated to time). Filenames here are chosen so
    # alphabetical order is the OPPOSITE of time order -- --limit must keep
    # the time-newest handback, not whichever the alphabetical scan visited
    # last.
    projects = tmp_path / ".claude" / "projects" / "-tmp-proj"
    projects.mkdir(parents=True)

    older_ts = (_NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    newer_ts = (_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _handback_rows(session_id, tail_text, ts):
        return [
            {"type": "assistant", "sessionId": session_id, "cwd": "/tmp/proj",
             "timestamp": ts,
             "message": {"stop_reason": "end_turn",
                         "content": [{"type": "text", "text": tail_text}]}},
            {"type": "user", "sessionId": session_id, "cwd": "/tmp/proj",
             "timestamp": ts, "message": {"content": "keep going"}},
        ]

    # "sess-aaa" sorts FIRST alphabetically but is the time-NEWEST.
    (projects / "sess-aaa.jsonl").write_text(
        "\n".join(json.dumps(r) for r in _handback_rows("sess-aaa", "AAA_TAIL", newer_ts)) + "\n")
    # "sess-zzz" sorts LAST alphabetically but is the time-OLDEST.
    (projects / "sess-zzz.jsonl").write_text(
        "\n".join(json.dumps(r) for r in _handback_rows("sess-zzz", "ZZZ_TAIL", older_ts)) + "\n")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    captured = {}

    def spy_grade(hbs, **kw):
        captured["hbs"] = hbs
        return [BacktestCase(AWAITING_CONTINUE, True, True, h.tail, h.reply) for h in hbs]

    monkeypatch.setattr("orchestrator.stall_backtest.grade", spy_grade)

    res = CliRunner().invoke(main, ["sessions", "stall-backtest", "--limit", "1", "--json-output"])
    assert res.exit_code == 0, res.output

    assert len(captured["hbs"]) == 1
    assert captured["hbs"][0].tail == "AAA_TAIL"  # time-newest, despite sorting last alphabetically

    out = json.loads(res.output)
    assert out["handbacks_found"] == 2   # TRUE pre-limit total (Finding 2)
    assert out["limit_applied"] is True
    assert out["limit"] == 1
    assert out["graded"] == 1


def test_backtest_limit_prints_a_loud_note_when_it_truncates(tmp_path, monkeypatch):
    # Finding 2 regression guard: a capped run must say, in the
    # human-readable output itself (not just --json-output), how much
    # history existed before the cap -- mirroring the chunk-skip WARNING.
    projects = tmp_path / ".claude" / "projects" / "-tmp-proj"
    projects.mkdir(parents=True)
    rows = [
        {"type": "assistant", "sessionId": "s1", "cwd": "/tmp/proj",
         "timestamp": "2026-07-30T10:00:00Z",
         "message": {"stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "Done. Next I'll re-render."}]}},
        {"type": "user", "sessionId": "s1", "cwd": "/tmp/proj",
         "timestamp": "2026-07-30T10:01:00Z", "message": {"content": "keep going"}},
        {"type": "assistant", "sessionId": "s1", "cwd": "/tmp/proj",
         "timestamp": "2026-07-30T10:02:00Z",
         "message": {"stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "Second thing done."}]}},
        {"type": "user", "sessionId": "s1", "cwd": "/tmp/proj",
         "timestamp": "2026-07-30T10:03:00Z", "message": {"content": "yes go ahead"}},
    ]
    (projects / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "orchestrator.stall_backtest.grade",
        lambda hbs, **kw: [BacktestCase(AWAITING_CONTINUE, True, True, h.tail, h.reply)
                            for h in hbs])

    res = CliRunner().invoke(main, ["sessions", "stall-backtest", "--limit", "1"])
    assert res.exit_code == 0, res.output
    assert "NOTE: --limit 1 kept the newest 1 of 2 handbacks found" in res.output


def test_backtest_no_limit_note_when_limit_not_truncating(tmp_path, monkeypatch):
    projects = tmp_path / ".claude" / "projects" / "-tmp-proj"
    projects.mkdir(parents=True)
    rows = [
        {"type": "assistant", "sessionId": "s1", "cwd": "/tmp/proj",
         "timestamp": "2026-07-30T10:00:00Z",
         "message": {"stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "Done. Next I'll re-render."}]}},
        {"type": "user", "sessionId": "s1", "cwd": "/tmp/proj",
         "timestamp": "2026-07-30T10:01:00Z", "message": {"content": "keep going"}},
    ]
    (projects / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "orchestrator.stall_backtest.grade",
        lambda hbs, **kw: [BacktestCase(AWAITING_CONTINUE, True, True, h.tail, h.reply)
                            for h in hbs])

    # --limit 5 with only 1 handback available never truncates.
    res = CliRunner().invoke(main, ["sessions", "stall-backtest", "--limit", "5"])
    assert res.exit_code == 0, res.output
    assert "NOTE:" not in res.output

    out_res = CliRunner().invoke(main, ["sessions", "stall-backtest", "--limit", "5", "--json-output"])
    out = json.loads(out_res.output)
    assert out["limit_applied"] is False
    assert out["limit"] == 5
