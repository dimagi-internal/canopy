"""Tests for the STATUS sentinel lines emitted by `canopy analyze`.

A session-review run can fan out many parallel background `canopy analyze`
calls, and some may produce 0-byte output files. Without the
STARTED/DONE/FAILED sentinels, "0 bytes" is indistinguishable from
"command was never invoked," "venv contention killed the launch," and
"command ran but found no observations." These tests pin the contract.
"""
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from orchestrator.cli import main


@patch("orchestrator.cli.ensure_canopy_dir")
@patch("orchestrator.analyzer.analyze_transcript")
class TestAnalyzeStatusSentinels:
    def _setup_mocks(self, ensure, analyze, tmp_path):
        ensure.return_value = tmp_path

    def test_started_line_emitted_first(
        self, mock_analyze, mock_ensure, tmp_path
    ):
        self._setup_mocks(mock_ensure, mock_analyze, tmp_path)
        mock_analyze.return_value = []
        transcript = tmp_path / "fake.jsonl"
        transcript.write_text("{}\n")

        runner = CliRunner()
        result = runner.invoke(main, ["analyze", str(transcript)])
        assert result.exit_code == 0
        # STARTED is the very first line so a 0-byte output file is unambiguously
        # "the command never wrote anything" rather than "ran with no findings".
        assert result.output.startswith("STATUS: STARTED analyze "), result.output
        assert str(transcript) in result.output.splitlines()[0]

    def test_done_zero_observations_no_propose(
        self, mock_analyze, mock_ensure, tmp_path
    ):
        self._setup_mocks(mock_ensure, mock_analyze, tmp_path)
        mock_analyze.return_value = []
        transcript = tmp_path / "fake.jsonl"
        transcript.write_text("{}\n")

        runner = CliRunner()
        result = runner.invoke(main, ["analyze", str(transcript)])
        assert result.exit_code == 0
        assert "STATUS: DONE 0-observations" in result.output
        assert "No observations found." in result.output

    def test_done_observations_no_propose(
        self, mock_analyze, mock_ensure, tmp_path
    ):
        self._setup_mocks(mock_ensure, mock_analyze, tmp_path)
        mock_analyze.return_value = [
            {"type": "friction", "description": "x", "severity": "low", "related_servers": []},
            {"type": "gap", "description": "y", "severity": "medium", "related_servers": []},
        ]
        transcript = tmp_path / "fake.jsonl"
        transcript.write_text("{}\n")
        (tmp_path / "observations").mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["analyze", str(transcript)])
        assert result.exit_code == 0
        assert "STATUS: DONE 2-observations no-propose" in result.output

    def test_failed_when_analyze_raises(
        self, mock_analyze, mock_ensure, tmp_path
    ):
        self._setup_mocks(mock_ensure, mock_analyze, tmp_path)
        mock_analyze.side_effect = RuntimeError("simulated rate-limit")
        transcript = tmp_path / "fake.jsonl"
        transcript.write_text("{}\n")

        runner = CliRunner()
        result = runner.invoke(main, ["analyze", str(transcript)])
        assert result.exit_code != 0
        # STARTED still emitted (so 0-byte case still distinguishable from this case)
        assert "STATUS: STARTED" in result.output
        assert "STATUS: FAILED analyze-raised RuntimeError" in result.output
        assert "simulated rate-limit" in result.output

    def test_status_started_includes_transcript_path(
        self, mock_analyze, mock_ensure, tmp_path
    ):
        # The transcript path on the STARTED line is what lets a parallel
        # caller correlate output files back to specific sessions.
        self._setup_mocks(mock_ensure, mock_analyze, tmp_path)
        mock_analyze.return_value = []
        transcript = tmp_path / "session-abc-123.jsonl"
        transcript.write_text("{}\n")

        runner = CliRunner()
        result = runner.invoke(main, ["analyze", str(transcript)])
        assert "STATUS: STARTED analyze " in result.output
        assert "session-abc-123.jsonl" in result.output
