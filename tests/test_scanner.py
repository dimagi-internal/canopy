import json
from pathlib import Path
import pytest
from orchestrator.scanner import scan_transcript, scan_all_transcripts


FIXTURE = Path(__file__).parent / "fixtures" / "sample_transcript.jsonl"


class TestScanTranscript:
    def test_returns_dict(self):
        result = scan_transcript(FIXTURE)
        assert isinstance(result, dict)

    def test_has_session_id(self):
        result = scan_transcript(FIXTURE)
        assert result["session_id"] == "test-session-001"

    def test_has_file_path(self):
        result = scan_transcript(FIXTURE)
        assert result["path"] == str(FIXTURE)

    def test_has_line_count(self):
        result = scan_transcript(FIXTURE)
        assert result["lines"] > 0

    def test_has_user_message_count(self):
        result = scan_transcript(FIXTURE)
        assert result["user_msgs"] > 0

    def test_has_first_message(self):
        result = scan_transcript(FIXTURE)
        assert "maternal health" in result["first_msg"].lower()

    def test_has_mcp_tools(self):
        result = scan_transcript(FIXTURE)
        assert "connect_search" in result["mcp_servers"]

    def test_has_mcp_call_count(self):
        result = scan_transcript(FIXTURE)
        assert result["mcp_call_count"] >= 2

    def test_has_timestamps(self):
        result = scan_transcript(FIXTURE)
        assert result["first_ts"] is not None
        assert result["last_ts"] is not None

    def test_has_project_key(self):
        result = scan_transcript(FIXTURE)
        assert "project_key" in result


class TestScanAllTranscripts:
    def test_returns_list(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        result = scan_all_transcripts(projects_dir)
        assert isinstance(result, list)

    def test_finds_transcripts(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-test-project"
        proj.mkdir(parents=True)
        # Copy fixture
        import shutil
        shutil.copy(FIXTURE, proj / "abc123.jsonl")
        result = scan_all_transcripts(projects_dir)
        assert len(result) == 1

    def test_skips_non_jsonl(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-test-project"
        proj.mkdir(parents=True)
        (proj / "not-a-transcript.txt").write_text("hello")
        result = scan_all_transcripts(projects_dir)
        assert len(result) == 0

    def test_includes_repo_from_map(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-test-project"
        proj.mkdir(parents=True)
        import shutil
        shutil.copy(FIXTURE, proj / "abc123.jsonl")
        repo_map = {"-test-project": "owner/my-repo"}
        result = scan_all_transcripts(projects_dir, repo_map=repo_map)
        assert result[0]["repo"] == "owner/my-repo"


# --- summarize_prompt: the roster must distinguish turns of the SAME agent (canopy#599) ---

def test_summarize_prompt_surfaces_the_slash_command_scope():
    """The bug: truncating the raw prompt cuts INSIDE the command preamble, so the
    `<command-args>` — the only part saying what the session works on — is always the
    part discarded. Six live hal turns on six different items all rendered as
    `"<command-message>hal:turn</command-message>\\n<comma..."` on 2026-09-04.
    """
    from orchestrator.scanner import summarize_prompt

    prompt = (
        "<command-message>hal:turn</command-message>\n"
        "<command-name>/hal:turn</command-name>\n"
        "<command-args>--thread 1a043f92a7ff114d</command-args>"
    )
    assert summarize_prompt(prompt) == "/hal:turn --thread 1a043f92a7ff114d"


def test_summarize_prompt_distinguishes_two_turns_of_one_agent():
    """The only case where the roster is NEEDED is several turns of one agent — which
    is exactly the case the old rendering could not tell apart."""
    from orchestrator.scanner import summarize_prompt

    def p(args):
        return ("<command-message>hal:turn</command-message>\n"
                "<command-name>/hal:turn</command-name>\n"
                f"<command-args>{args}</command-args>")

    a = summarize_prompt(p("--thread 1a066d5b0f2cf45a"))
    b = summarize_prompt(p("--thread 1a066d69ac56e74f"))
    assert a != b, "two turns on different threads must not render identically"


def test_summarize_prompt_falls_back_to_a_plain_prompt():
    from orchestrator.scanner import summarize_prompt

    assert summarize_prompt("look into the labs worker crash loop") == \
        "look into the labs worker crash loop"


def test_summarize_prompt_collapses_whitespace_and_clips():
    from orchestrator.scanner import summarize_prompt

    out = summarize_prompt("a\n\n   b\tc")
    assert out == "a b c"
    long = summarize_prompt("x" * 200, width=20)
    assert long == "x" * 20 + "..." and len(long) == 23


def test_summarize_prompt_handles_empty_and_missing():
    from orchestrator.scanner import summarize_prompt

    assert summarize_prompt("") == ""
    assert summarize_prompt(None) == ""
