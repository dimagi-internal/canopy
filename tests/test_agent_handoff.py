"""`canopy agent handoff` — moving a project between agents.

The receiver's first need is the source agent's sessions: the context the
artifacts do not carry. These pin how they are found and how the boards change.
"""
import json

import pytest
from click.testing import CliRunner

from orchestrator.agent_handoff import find_sessions, handoff_notes
from orchestrator.cli import main


def _session(root, dirname, sid, prompt, body):
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "message": {"content": prompt}},
            {"type": "assistant", "message": {"content": body}}]
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


@pytest.fixture
def transcripts(tmp_path):
    _session(tmp_path, "-Users-x-emdash-worktrees-ace-1-emdash-c-side-on-avi", "s-ace",
             "<command-name>/ace:turn</command-name> --thread T1", "wrote doc D1")
    _session(tmp_path, "-Users-x-emdash-worktrees-eva-2-emdash-c-cos", "s-eva",
             "/eva:chief-of-staff", "saw T1 in the inbox")
    _session(tmp_path, "-Users-x-emdash-worktrees-hal-3-emdash-c-other", "s-hal",
             "/hal:turn", "nothing relevant")
    # a resumed ACE session: no slash command, but it ran in an ace worktree
    _session(tmp_path, "-Users-x-emdash-worktrees-ace-1-emdash-session", "s-ace2",
             "continue", "edited D1")
    return tmp_path


def test_finds_source_sessions_first_and_skips_non_matches(transcripts):
    found = find_sessions(["T1", "D1"], "ace", root=transcripts)

    ids = [s["session_id"] for s in found]
    assert "s-hal" not in ids
    assert set(ids[:2]) == {"s-ace", "s-ace2"}      # source agent's sessions lead
    assert ids[2] == "s-eva"                         # a mere mention comes after
    assert all(s["from_agent"] for s in found[:2]) and not found[2]["from_agent"]


def test_resumed_session_still_counts_as_the_source_agents(transcripts):
    """argv does not survive a resume; the worktree directory does."""
    found = {s["session_id"]: s for s in find_sessions(["D1"], "ace", root=transcripts)}
    assert found["s-ace2"]["from_agent"] is True


def test_excludes_the_handoff_session_itself(transcripts):
    found = find_sessions(["T1"], "ace", root=transcripts, exclude_session="s-eva")
    assert "s-eva" not in [s["session_id"] for s in found]


def test_notes_say_so_when_no_source_session_was_found():
    notes = handoff_notes("ace", "eva", [], today="2026-10-02")
    assert "Handed off from ace to eva on 2026-10-02." in notes
    assert "another runner" in notes


@pytest.fixture
def fake_http(monkeypatch, transcripts):
    calls, responses = [], {}

    def transport(method, url, headers, body):
        calls.append((method, url, json.loads(body) if body else None))
        return responses.get((method, url.split("/api/")[1]), (200, "{}"))

    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    monkeypatch.setattr("orchestrator.agent_handoff.PROJECTS_ROOT", transcripts)
    return calls, responses


def test_dry_run_writes_nothing(fake_http):
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "handoff", "--from", "ace", "--to", "eva",
                                  "--name", "Avi concept note", "--ref", "T1", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert calls == []
    out = json.loads(r.output)
    assert out["sessions"][0]["session_id"] in {"s-ace", "s-ace2"}


def test_opens_on_receiver_and_reports_unreachable_source_board(fake_http):
    calls, responses = fake_http
    responses[("POST", "agents/eva/projects/")] = (200, json.dumps({"ext_id": "P2"}))
    responses[("GET", "agents/ace/projects/")] = (404, json.dumps({"detail": "agent 'ace' not found"}))

    r = CliRunner().invoke(main, [
        "agent", "handoff", "--from", "ace", "--to", "eva", "--name", "Avi concept note",
        "--ref", "T1", "--ref", "https://docs.google.com/document/d/D1/edit",
        "--from-project", "Avi concept note"])

    assert r.exit_code == 0, r.output
    method, url, body = calls[0]
    assert (method, url) == ("POST", "https://x.test/api/agents/eva/projects/")
    assert "Handed off from ace to eva" in body["notes"]
    assert "s-ace" in body["notes"]
    assert body["links"] == [{"label": "ref", "url": "https://docs.google.com/document/d/D1/edit"}]
    out = json.loads(r.output)
    assert out["project"]["ext_id"] == "P2"
    assert "project-set --slug ace" in out["source_project"]["todo"]


def test_session_matching_more_refs_outranks_a_newer_mention(tmp_path):
    import os, time
    _session(tmp_path, "-Users-x-emdash-worktrees-ace-1-emdash-c-work", "s-work",
             "/ace:turn", "T1 and D1")
    _session(tmp_path, "-Users-x-emdash-worktrees-ace-1-emdash-c-mention", "s-mention",
             "/ace:turn", "T1 only")
    old = time.time() - 3600
    os.utime(tmp_path / "-Users-x-emdash-worktrees-ace-1-emdash-c-work" / "s-work.jsonl", (old, old))
    found = find_sessions(["T1", "D1"], "ace", root=tmp_path)
    assert [s["session_id"] for s in found] == ["s-work", "s-mention"]
