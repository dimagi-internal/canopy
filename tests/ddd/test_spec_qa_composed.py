"""spec_qa / validate run against a composed recipe + lock (L1).

`spec_qa` delegates loading to `validate`, which read the path itself — so a
`<slug>.recipe.yaml` was validated ALONE and failed with "name: Field required;
personas: Field required; ...". Every QA gate would have failed the instant a
spec was migrated.
"""
from __future__ import annotations

import json
import pathlib
import shutil

import pytest
import yaml

from scripts.ddd.spec_qa import spec_qa
from scripts.ddd.split_spec import split
from scripts.ddd.validate import validate

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "live_specs"
LIVE_WHY_BRIEFS = pathlib.Path(
    "/Users/acedimagi/emdash/repositories/connect-labs/docs/walkthroughs"
)


def _minimal_pair(tmp_path):
    (tmp_path / "demo.recipe.yaml").write_text(yaml.dump({
        "base_url": "http://localhost:8000",
        "scenes": [{
            "id": "the-goal", "show": "Open the dashboard.", "role": "overview",
            "concept_claim": "The dashboard loads in under two seconds.",
        }],
    }))
    (tmp_path / "demo.narrative.lock.json").write_text(json.dumps({
        "slug": "demo", "version": 1, "fetched_at": "2026-07-26T10:00:00Z",
        "name": "demo", "narrative": "The goal.",
        "personas": {"maya": {"name": "Maya", "role": "PM", "color": "#4F46E5",
                              "intro": "Maya runs a vitamin-A program."}},
        "build_order": ["the-goal"],
        "scenes": [{"id": "the-goal", "title": "The goal", "persona": "maya",
                    "provenance": "S1", "narrative": "The goal.", "features": []}],
    }, indent=2, sort_keys=True) + "\n")
    return tmp_path / "demo.recipe.yaml"


def test_spec_qa_accepts_a_recipe_path(tmp_path):
    verdict = spec_qa(str(_minimal_pair(tmp_path)))
    reason = (verdict.blocking_reason or "").lower()
    assert "field required" not in reason, reason
    assert verdict.verdict == "pass", reason


def test_validate_accepts_a_recipe_path(tmp_path):
    ok, problems = validate("unified_spec", _minimal_pair(tmp_path))
    assert ok, problems


def test_validate_still_loads_a_why_brief_plainly(tmp_path):
    """Only unified_spec is two-file — other kinds must load unchanged."""
    wb = tmp_path / "b.why_brief.yaml"
    wb.write_text(yaml.dump({"narrative_slug": "demo", "problem": "", "spine": [], "gaps": []}))
    ok, problems = validate("why_brief", wb)
    assert "Failed to load file" not in " ".join(problems)


@pytest.mark.skipif(not LIVE_WHY_BRIEFS.exists(), reason="connect-labs checkout absent")
@pytest.mark.parametrize("slug", ["verified-monitoring", "microplans-study-groups"])
def test_a_migrated_live_spec_passes_qa(slug, tmp_path):
    """End to end on real data: legacy spec → split → spec_qa passes.

    The legacy spec FAILS QA only because it has no scene ids; the split mints
    them. So migration is what makes these specs clean, not what breaks them.
    """
    shutil.copy(FIXTURES / f"{slug}.yaml", tmp_path / f"{slug}.yaml")
    wb = LIVE_WHY_BRIEFS / f"{slug}.why_brief.yaml"
    if wb.exists():
        shutil.copy(wb, tmp_path / wb.name)

    before = spec_qa(str(tmp_path / f"{slug}.yaml"))
    assert "scene id" in (before.blocking_reason or "").lower()

    split(tmp_path / f"{slug}.yaml")
    (tmp_path / f"{slug}.yaml").unlink()

    after = spec_qa(str(tmp_path / f"{slug}.recipe.yaml"))
    assert after.verdict == "pass", after.blocking_reason
