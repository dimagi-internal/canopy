"""The one LLM-stdout → YAML-list parser (canopy#487).

The regression these lock down: every `claude -p` gate in the tree asks for "a YAML
list" and five separate call sites stripped the code fence ONLY when the answer began
with it. A model that introduced its block with one sentence — the most common answer
shape there is — had its completed verdict thrown away, and `agent-review` then handed
findings on stamped UNVERIFIED.
"""
from __future__ import annotations

import pytest

from orchestrator.analyzer import parse_analysis_output
from orchestrator.agent_review import parse_findings
from orchestrator.harvest import _looks_like_empty_yaml_list
from orchestrator.llm_output import parse_yaml_list, strip_code_fences
from orchestrator.proposer import parse_proposal_output
from orchestrator.verify_findings import _parse_verdict_output

# The verbatim shape from the issue: one sentence of preamble, then a fenced block.
PREAMBLE_THEN_FENCE = (
    "Confirmed via direct file reads and history checks — neither friction has been "
    "addressed in current `origin/main`.\n"
    "\n"
    "```yaml\n"
    "- index: 0\n"
    "  verdict: live\n"
    "  evidence: agent_review.py:797 still discards the verdict\n"
    "- index: 1\n"
    "  verdict: shipped\n"
    "  evidence: fixed by f87bed0\n"
    "```\n"
)


def test_preamble_before_fenced_block_still_parses():
    """The exact 2026-08-14 failure: the verify pass DID reach a verdict."""
    parsed = parse_yaml_list(PREAMBLE_THEN_FENCE)
    assert [item["verdict"] for item in parsed] == ["live", "shipped"]
    assert parsed[0]["index"] == 0


def test_bare_fenced_block_parses_as_before():
    """Legacy path is untouched — a well-formed fenced answer behaves identically."""
    parsed = parse_yaml_list("```yaml\n- title: Fix auth\n  confidence: high\n```")
    assert parsed == [{"title": "Fix auth", "confidence": "high"}]


def test_unfenced_list_after_preamble_parses():
    """Some models never fence at all; resume at the first list item."""
    parsed = parse_yaml_list(
        "Here is what I found after reading the source:\n\n"
        "- index: 0\n"
        "  verdict: live\n"
    )
    assert parsed == [{"index": 0, "verdict": "live"}]


def test_prose_with_no_list_still_returns_empty():
    """The loud 'did not parse' path must survive — we stop discarding verdicts that
    are PRESENT, we never invent one."""
    assert parse_yaml_list("I could not determine a verdict for these findings.") == []
    assert parse_yaml_list("not a list") == []
    assert parse_yaml_list("") == []
    assert parse_yaml_list("   ") == []


def test_mapping_list_wins_over_a_prose_bullet_list():
    """A bullet-point preamble parses as a list of strings; it must not shadow the
    real verdict block underneath it."""
    parsed = parse_yaml_list(
        "I checked the following:\n"
        "- the merge commit\n"
        "- the current source\n"
        "\n"
        "```yaml\n"
        "- index: 0\n"
        "  verdict: shipped\n"
        "```\n"
    )
    assert parsed == [{"index": 0, "verdict": "shipped"}]


def test_genuine_empty_list_is_preserved():
    """`harvest` distinguishes a clean audit from garbage off this — an empty list
    must stay an empty list rather than becoming 'unparseable'."""
    assert parse_yaml_list("[]") == []
    assert parse_yaml_list("```yaml\n[]\n```") == []


def test_strip_code_fences_is_position_independent():
    assert strip_code_fences("prose\n```yaml\n- a: 1\n```") == "prose\n- a: 1"
    assert strip_code_fences("```\n- a: 1\n```") == "- a: 1"


def test_looks_like_empty_yaml_list_tolerates_preamble():
    """The harvest sibling the issue flagged: same defect, same text."""
    assert _looks_like_empty_yaml_list("[]")
    assert _looks_like_empty_yaml_list("```yaml\n[]\n```")
    assert _looks_like_empty_yaml_list("No intent drift found.\n\n```yaml\n[]\n```")
    assert not _looks_like_empty_yaml_list("- finding: something real")


@pytest.mark.parametrize(
    "parser",
    [parse_findings, parse_analysis_output, parse_proposal_output, _parse_verdict_output],
)
def test_every_call_site_shares_the_tolerance(parser):
    """All five sites carried the identical bug; they must now share the identical fix."""
    parsed = parser(PREAMBLE_THEN_FENCE)
    assert parsed and parsed[0]["verdict"] == "live"
    assert parser("not a list") == []
