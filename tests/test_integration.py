"""Integration test — create, save and reload a multi-server corpus entry."""

from pathlib import Path

import pytest

from orchestrator.corpus import create_corpus_entry, save_corpus_entry, load_corpus_entry

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.integration
def test_full_flow(tmp_path):
    """Test: create a multi-server corpus entry, save it, load it back."""
    # 1. Create corpus entry
    corpus_dir = tmp_path / "corpus"
    entry = create_corpus_entry(
        entry_id="test-solicitation",
        domain="connect",
        goal="Create a test solicitation",
        initial_prompt="Create a solicitation for test program",
        expected_servers=["commcare-hq", "solicitations"],
        expected_tool_sequence=[
            {"server": "commcare-hq", "tool": "get_app_structure", "mock_response": "{}"},
            {"server": "solicitations", "tool": "create_solicitation", "is_write": True, "mock_response": "{}"},
        ],
        tags=["test"],
    )

    path = save_corpus_entry(entry, corpus_dir)
    assert path.exists()

    loaded = load_corpus_entry(path)
    assert loaded["id"] == "test-solicitation"
    assert loaded["complexity"] == "multi-server"
