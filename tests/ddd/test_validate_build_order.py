"""build_order validates against stable scene ids, not title slugs (L0).

Regression guard for the split-identity bug: build_order is GENERATED from
scene ids (build_narrative_review_request) but was VALIDATED against slugs
re-derived from scene titles. The two agreed only while no scene carried an
explicit id — so the first backfilled spec would have failed validation.
"""
from __future__ import annotations

import yaml

from scripts.ddd.validate import validate


def _spec(tmp_path, build_order):
    raw = {
        "name": "demo",
        "narrative": "The goal. The proof.",
        "base_url": "http://localhost:8000",
        "personas": {"maya": {
            "name": "Maya", "role": "PM", "color": "#4F46E5",
            "intro": "Maya runs a vitamin-A program and wants independent proof.",
        }},
        "build_order": build_order,
        "scenes": [
            {"id": "the-goal", "persona": "maya",
             "title": "A title that does not slugify to the id",
             "show": "x", "concept_claim": "The dashboard loads in under two seconds.",
             "provenance": "S1", "role": "overview"},
            {"id": "the-proof", "persona": "maya",
             "title": "Another unrelated title", "show": "y",
             "concept_claim": "Each round shows a confidence interval.",
             "provenance": "S2", "role": "overview"},
        ],
    }
    p = tmp_path / "demo.yaml"
    p.write_text(yaml.dump(raw))
    return p


def test_build_order_of_scene_ids_is_valid(tmp_path):
    _ok, problems = validate("unified_spec", _spec(tmp_path, ["the-goal", "the-proof"]))
    assert [p for p in problems if "build_order" in p] == [], problems


def test_build_order_of_title_slugs_is_now_rejected(tmp_path):
    _ok, problems = validate(
        "unified_spec",
        _spec(tmp_path, ["a-title-that-does-not-slugify-to-the-id"]),
    )
    assert [p for p in problems if "build_order" in p]


def test_duplicate_build_order_entries_still_rejected(tmp_path):
    _ok, problems = validate("unified_spec", _spec(tmp_path, ["the-goal", "the-goal"]))
    assert [p for p in problems if "duplicate" in p]
