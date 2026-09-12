"""Tests for the two promoted privates: `templates()` and `gating_config()`.

fleet_align (in the `canopy` orchestrator repo) reaches across the package boundary for
the canonical stamp-table baseline — it diffs real agent repos against "what a
factory-stamped agent looks like." Before this promotion it did `getattr(agent_factory,
"_TEMPLATES", {})`, whose silent-empty default means a rename or a refactor could make
the ground-truth baseline quietly become `{}` with no error — fleet_align would then
report every agent as missing every artifact, or worse, nothing at all. This is the test
that would have caught that class of bug: it pins the count and proves the returned
mapping cannot be mutated by a caller.
"""
from __future__ import annotations

import pytest

from canopy_agent_factory import gating_config, templates


def test_templates_is_non_empty_with_expected_count():
    t = templates()
    # 20 entries verified at extraction time (EXTRACTION-BRIEF.md) — a change to this
    # number should be a deliberate template addition/removal, not a silent regression.
    assert len(t) == 20
    assert "CLAUDE.md" in t
    assert "config/gating.json" in t


def test_templates_result_is_read_only():
    t = templates()
    with pytest.raises(TypeError):
        t["CLAUDE.md"] = "tampered"


def test_templates_returns_a_fresh_copy_each_call():
    """Mutating what one call returns must never leak into the next call."""
    a = templates()
    b = templates()
    assert a == b
    assert a is not b


def test_gating_config_is_present_and_non_empty():
    cfg = gating_config()
    assert isinstance(cfg, str)
    assert cfg.strip()
    assert "deny" in cfg
