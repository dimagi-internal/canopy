import json
from pathlib import Path
import pytest
from orchestrator.transcripts import (
    read_transcript,
    extract_user_messages,
    extract_tool_calls,
    extract_assistant_text,
    get_session_id,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transcript.jsonl"


class TestReadTranscript:
    def test_returns_list_of_dicts(self):
        entries = read_transcript(FIXTURE)
        assert isinstance(entries, list)
        assert all(isinstance(e, dict) for e in entries)

    def test_all_entries_have_type(self):
        entries = read_transcript(FIXTURE)
        assert all("type" in e for e in entries)

    def test_missing_file_returns_empty(self):
        entries = read_transcript(Path("/nonexistent/transcript.jsonl"))
        assert entries == []

    def test_filters_file_history_snapshots(self):
        entries = read_transcript(FIXTURE)
        types = [e["type"] for e in entries]
        assert "file-history-snapshot" not in types


class TestExtractUserMessages:
    def test_returns_strings(self):
        entries = read_transcript(FIXTURE)
        messages = extract_user_messages(entries)
        assert all(isinstance(m, str) for m in messages)

    def test_finds_user_text(self):
        entries = read_transcript(FIXTURE)
        messages = extract_user_messages(entries)
        assert any("maternal health" in m.lower() for m in messages)

    def test_excludes_tool_results(self):
        entries = read_transcript(FIXTURE)
        messages = extract_user_messages(entries)
        assert all("tool_result" not in m for m in messages)


class TestExtractToolCalls:
    def test_returns_list_of_dicts(self):
        entries = read_transcript(FIXTURE)
        calls = extract_tool_calls(entries)
        assert isinstance(calls, list)
        assert all(isinstance(c, dict) for c in calls)

    def test_each_call_has_name_and_input(self):
        entries = read_transcript(FIXTURE)
        calls = extract_tool_calls(entries)
        for call in calls:
            assert "name" in call
            assert "input" in call

    def test_finds_mcp_calls(self):
        entries = read_transcript(FIXTURE)
        calls = extract_tool_calls(entries)
        names = [c["name"] for c in calls]
        assert "mcp__connect_search__search_documents" in names
        assert "mcp__commcare_hq__get_app_structure" in names

    def test_includes_tool_result(self):
        entries = read_transcript(FIXTURE)
        calls = extract_tool_calls(entries)
        for call in calls:
            assert "result" in call


class TestExtractAssistantText:
    def test_returns_strings(self):
        entries = read_transcript(FIXTURE)
        texts = extract_assistant_text(entries)
        assert all(isinstance(t, str) for t in texts)

    def test_finds_assistant_reasoning(self):
        entries = read_transcript(FIXTURE)
        texts = extract_assistant_text(entries)
        combined = " ".join(texts)
        assert "training" in combined.lower()


class TestGetSessionId:
    def test_returns_session_id(self):
        entries = read_transcript(FIXTURE)
        assert get_session_id(entries) == "test-session-001"

    def test_returns_none_if_no_last_prompt(self):
        assert get_session_id([]) is None


def test_extract_tool_calls_keeps_the_harness_error_flag():
    """`is_error` is the recorded outcome; dropping it forces consumers to re-derive
    failure from the result prose. None when absent, so "unknown" stays distinct
    from "succeeded". See dimagi-internal/canopy#416."""
    from orchestrator.transcripts import extract_tool_calls

    def _pair(tid, name, result, **extra):
        return [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": name, "input": {}}]}},
            {"type": "user", "message": {"content": [
                dict({"type": "tool_result", "tool_use_id": tid, "content": result}, **extra)]}},
        ]

    entries = (_pair("a", "Bash", "boom", is_error=True)
               + _pair("b", "Bash", "fine", is_error=False)
               + _pair("c", "Bash", "legacy"))
    flags = [c.get("is_error") for c in extract_tool_calls(entries)]
    assert flags == [True, False, None]
