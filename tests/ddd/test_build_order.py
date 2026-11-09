"""Tests for build_order — tackle sequence over chunks, independent of video order.

Covers:
  1. UnifiedSpec.build_order field (models.py)
  2. validate() semantic checks for build_order (validate.py)
  3. build_narrative_review_request — emits build_order in ReviewRequest (narrative.py)
  4. apply_narrative_edits — reads + persists build_order from response_json (narrative.py)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts.ddd.schemas.models import Feature, NarrationItem, Persona, ReviewRequest, Scene, UnifiedSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _make_spec(build_order: list[str] | None = None) -> UnifiedSpec:
    kwargs = {}
    if build_order is not None:
        kwargs["build_order"] = build_order
    return UnifiedSpec(
        name="rooftop-surveys",
        narrative="Rooftop surveys ride Connect microplanning.",
        base_url="https://labs.connect.dimagi.com",
        personas={
            "alice": Persona(
                name="Alice",
                role="Program Manager",
                color="#3B82F6",
                intro="Alice manages rooftop survey programs.",
            ),
            "bob": Persona(
                name="Bob",
                role="Field Supervisor",
                color="#10B981",
                intro="Bob assigns work to field teams.",
            ),
        },
        scenes=[
            Scene(
                persona="alice",
                title="Area Selection",
                show="Navigate to /areas.",
                concept_claim="Users draw a boundary to select the survey area.",
                provenance="S1",
            ),
            Scene(
                persona="alice",
                title="Sample Generation",
                show="Click Generate Sample.",
                concept_claim="System generates a proportional building sample.",
                provenance="S2",
            ),
            Scene(
                persona="bob",
                title="Field Assignment",
                show="Assign buildings to field workers.",
                concept_claim="Supervisors assign buildings to FLWs with a single tap.",
                provenance="S3",
            ),
        ],
        **kwargs,
    )


def _write_spec(tmp_path: Path, spec: UnifiedSpec) -> Path:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        yaml.dump(spec.model_dump(), default_flow_style=False, allow_unicode=True)
    )
    return spec_path


# ---------------------------------------------------------------------------
# 1. UnifiedSpec.build_order field
# ---------------------------------------------------------------------------


class TestUnifiedSpecBuildOrder:
    def test_build_order_defaults_to_empty_list(self):
        spec = _make_spec()
        assert spec.build_order == []

    def test_build_order_accepts_list_of_strings(self):
        spec = _make_spec(build_order=["field-assignment", "area-selection", "sample-generation"])
        assert spec.build_order == ["field-assignment", "area-selection", "sample-generation"]

    def test_build_order_round_trips_through_model_dump(self):
        order = ["sample-generation", "field-assignment", "area-selection"]
        spec = _make_spec(build_order=order)
        dumped = spec.model_dump()
        assert "build_order" in dumped
        assert dumped["build_order"] == order

    def test_build_order_round_trips_through_yaml(self, tmp_path):
        order = ["field-assignment", "area-selection"]
        spec = _make_spec(build_order=order)
        spec_path = _write_spec(tmp_path, spec)
        raw = yaml.safe_load(spec_path.read_text())
        reloaded = UnifiedSpec.model_validate(raw)
        assert reloaded.build_order == order

    def test_empty_build_order_round_trips_through_yaml(self, tmp_path):
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        raw = yaml.safe_load(spec_path.read_text())
        reloaded = UnifiedSpec.model_validate(raw)
        assert reloaded.build_order == []

    def test_spec_without_build_order_key_loads_with_default(self, tmp_path):
        """A YAML spec that omits build_order entirely must default to []."""
        spec = _make_spec()
        raw = spec.model_dump()
        raw.pop("build_order", None)  # ensure key is absent
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(yaml.dump(raw))
        reloaded = UnifiedSpec.model_validate(yaml.safe_load(spec_path.read_text()))
        assert reloaded.build_order == []


# ---------------------------------------------------------------------------
# 2. validate() semantic checks for build_order
# ---------------------------------------------------------------------------


def _valid_spec_dict(build_order: list[str] | None = None) -> dict:
    d: dict = {
        "name": "Test Spec",
        "narrative": "A test walkthrough",
        "base_url": "http://localhost:8000",
        "personas": {
            "alice": {"name": "Alice", "role": "PM", "color": "#3B82F6", "intro": "Test persona."}
        },
        "scenes": [
            {
                "persona": "alice",
                "title": "Area Selection",
                "show": "navigate to /areas",
                "concept_claim": "Users draw a boundary.",
                "provenance": "S1",
            },
            {
                "persona": "alice",
                "title": "Sample Generation",
                "show": "click generate",
                "concept_claim": "System samples buildings.",
                "provenance": "S2",
            },
        ],
    }
    if build_order is not None:
        d["build_order"] = build_order
    return d


class TestValidateBuildOrder:
    def test_no_build_order_passes(self, tmp_path):
        from scripts.ddd.validate import validate

        spec = _valid_spec_dict()
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(spec))
        ok, problems = validate("unified_spec", p)
        assert ok is True, f"Expected pass, got: {problems}"

    def test_valid_partial_build_order_passes(self, tmp_path):
        """A partial list (only some scenes listed) is allowed."""
        from scripts.ddd.validate import validate

        spec = _valid_spec_dict(build_order=["sample-generation"])
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(spec))
        ok, problems = validate("unified_spec", p)
        assert ok is True, f"Expected pass with partial build_order, got: {problems}"

    def test_full_build_order_passes(self, tmp_path):
        from scripts.ddd.validate import validate

        spec = _valid_spec_dict(build_order=["sample-generation", "area-selection"])
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(spec))
        ok, problems = validate("unified_spec", p)
        assert ok is True, f"Expected pass with full build_order, got: {problems}"

    def test_build_order_referencing_missing_slug_fails(self, tmp_path):
        """A slug that doesn't match any scene title fails validation."""
        from scripts.ddd.validate import validate

        spec = _valid_spec_dict(build_order=["nonexistent-scene"])
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(spec))
        ok, problems = validate("unified_spec", p)
        assert ok is False
        assert any("nonexistent-scene" in prob for prob in problems), \
            f"Expected 'nonexistent-scene' in problems, got: {problems}"

    def test_build_order_with_one_bad_one_good_slug_fails(self, tmp_path):
        """One bad slug among valid ones still fails."""
        from scripts.ddd.validate import validate

        spec = _valid_spec_dict(build_order=["area-selection", "bogus-slug"])
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(spec))
        ok, problems = validate("unified_spec", p)
        assert ok is False
        assert any("bogus-slug" in prob for prob in problems)

    def test_build_order_with_duplicate_slug_fails(self, tmp_path):
        """Duplicate slugs in build_order fail validation."""
        from scripts.ddd.validate import validate

        spec = _valid_spec_dict(build_order=["area-selection", "area-selection"])
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(spec))
        ok, problems = validate("unified_spec", p)
        assert ok is False
        assert any(
            "duplicate" in prob.lower() and "area-selection" in prob
            for prob in problems
        ), f"Expected duplicate problem for area-selection, got: {problems}"

    def test_build_order_empty_list_passes(self, tmp_path):
        """Empty build_order is valid (treated as unset)."""
        from scripts.ddd.validate import validate

        spec = _valid_spec_dict(build_order=[])
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(spec))
        ok, problems = validate("unified_spec", p)
        assert ok is True, f"Expected pass with empty build_order, got: {problems}"

    def test_build_order_problem_message_is_clear(self, tmp_path):
        """Problem message includes context enough for the user to fix it."""
        from scripts.ddd.validate import validate

        spec = _valid_spec_dict(build_order=["area-selection", "missing-scene"])
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(spec))
        ok, problems = validate("unified_spec", p)
        assert ok is False
        # At least one message should mention build_order
        assert any("build_order" in prob for prob in problems), \
            f"Expected 'build_order' in problem messages, got: {problems}"


# ---------------------------------------------------------------------------
# 3. build_narrative_review_request — emits build_order
# ---------------------------------------------------------------------------


class TestBuildNarrativeReviewRequestBuildOrder:
    def test_review_request_has_build_order_field(self):
        from scripts.ddd.narrative import build_narrative_review_request

        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert hasattr(result, "build_order"), "ReviewRequest must have a build_order attribute"

    def test_build_order_defaults_to_scene_order_when_spec_has_none(self):
        """When spec.build_order is [], review_request.build_order = narration-item ids in scene order."""
        from scripts.ddd.narrative import build_narrative_review_request

        spec = _make_spec()  # build_order=[]
        result = build_narrative_review_request(spec, "run-001")
        expected = [_slug(scene.title) for scene in spec.scenes]
        assert result.build_order == expected

    def test_build_order_uses_spec_build_order_when_set(self):
        """When spec.build_order is set, review_request.build_order matches it."""
        from scripts.ddd.narrative import build_narrative_review_request

        order = ["field-assignment", "area-selection", "sample-generation"]
        spec = _make_spec(build_order=order)
        result = build_narrative_review_request(spec, "run-001")
        assert result.build_order == order

    def test_build_order_partial_spec_passes_through(self):
        """A partial spec.build_order (fewer slugs than scenes) passes through unchanged."""
        from scripts.ddd.narrative import build_narrative_review_request

        order = ["sample-generation"]
        spec = _make_spec(build_order=order)
        result = build_narrative_review_request(spec, "run-001")
        assert result.build_order == order

    def test_build_order_type_is_list(self):
        from scripts.ddd.narrative import build_narrative_review_request

        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert isinstance(result.build_order, list)

    def test_build_order_slugs_match_title_slugs_in_default(self):
        """Default build_order slugs must exactly match _title_slug applied to each scene title."""
        from scripts.ddd.narrative import build_narrative_review_request, _title_slug

        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        expected = [_title_slug(s.title) for s in spec.scenes]
        assert result.build_order == expected


# ---------------------------------------------------------------------------
# 3b. build_narrative_review_request — derives built|new status per beat
# ---------------------------------------------------------------------------


def _why_brief(*, s1_status="grounded", s2_status="grounded", s3_status="grounded", gaps=None):
    """A why-brief whose spine ids (S1/S2/S3) carry the given grounded/gap status.

    ``gaps`` is a list of spine ids that an open why-brief gap references.
    """
    def _spine(sid, status):
        return {
            "id": sid,
            "claim": sid,
            "status": status,
            "evidence": [{"kind": "implemented", "ref": f"EV-{sid}"}],
        }

    return {
        "feature": "rooftop-surveys",
        "problem": "x",
        "spine": [_spine("S1", s1_status), _spine("S2", s2_status), _spine("S3", s3_status)],
        "gaps": [{"id": f"G{i}", "type": "CAPABILITY", "claim_ref": ref} for i, ref in enumerate(gaps or [])],
    }


class TestNarrationBuildStatus:
    def test_grounded_ungapped_beat_is_built(self):
        """A grounded spine item with no open gap is 'built' — even with implemented evidence on a gap peer."""
        from scripts.ddd.narrative import build_narrative_review_request

        wb = _why_brief(s1_status="grounded", s2_status="gap", s3_status="grounded")
        result = build_narrative_review_request(_make_spec(), "run-001", why_brief=wb)
        by_prov = {n.provenance: n.status for n in result.narration}
        assert by_prov["S1"] == "built"  # grounded, no gap
        assert by_prov["S2"] == "new"  # status=gap -> frontier
        assert by_prov["S3"] == "built"

    def test_grounded_claim_with_open_capability_gap_reads_new(self):
        """A grounded spine item that a CAPABILITY gap references is still to-build -> 'new'."""
        from scripts.ddd.narrative import build_narrative_review_request

        wb = _why_brief(s1_status="grounded", s2_status="grounded", s3_status="grounded", gaps=["S2"])
        result = build_narrative_review_request(_make_spec(), "run-001", why_brief=wb)
        by_prov = {n.provenance: n.status for n in result.narration}
        assert by_prov["S1"] == "built"
        assert by_prov["S2"] == "new"  # grounded but a gap references it -> frontier
        assert by_prov["S3"] == "built"

    def test_no_why_brief_defaults_all_new(self):
        """With no why-brief, nothing is known to be built — every beat is 'new'."""
        from scripts.ddd.narrative import build_narrative_review_request

        result = build_narrative_review_request(_make_spec(), "run-001")
        assert all(n.status == "new" for n in result.narration)

    def test_unknown_provenance_is_new(self):
        """A scene whose provenance isn't in the spine falls back to 'new'."""
        from scripts.ddd.narrative import build_narrative_review_request

        wb = _why_brief()
        wb["spine"] = [wb["spine"][0]]  # drop S2/S3 from the spine entirely
        result = build_narrative_review_request(_make_spec(), "run-001", why_brief=wb)
        by_prov = {n.provenance: n.status for n in result.narration}
        assert by_prov["S1"] == "built"
        assert by_prov["S2"] == "new"  # not in spine -> new
        assert by_prov["S3"] == "new"


# ---------------------------------------------------------------------------
# 4. apply_narrative_edits — persists build_order from response_json
# ---------------------------------------------------------------------------


class TestApplyNarrativeEditsBuildOrder:
    def test_apply_persists_reordered_build_order(self, tmp_path):
        """A reordered build_order in the response is written back to the spec YAML."""
        from scripts.ddd.narrative import apply_narrative_edits

        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        new_order = ["field-assignment", "area-selection", "sample-generation"]
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
            "build_order": new_order,
        }
        result = apply_narrative_edits(str(spec_path), response)

        # Result dict includes build_order
        assert "build_order" in result, f"Result missing build_order key: {result}"
        assert result["build_order"] == new_order

        # Spec on disk is updated
        on_disk = yaml.safe_load(spec_path.read_text())
        assert on_disk.get("build_order") == new_order

    def test_apply_persists_partial_build_order(self, tmp_path):
        """A partial build_order (fewer slugs than scenes) is written back as-is."""
        from scripts.ddd.narrative import apply_narrative_edits

        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        partial_order = ["sample-generation"]
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
            "build_order": partial_order,
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["build_order"] == partial_order

        on_disk = yaml.safe_load(spec_path.read_text())
        assert on_disk.get("build_order") == partial_order

    def test_apply_drops_deleted_scene_slug_from_build_order(self, tmp_path):
        """After deleting a scene, its slug is removed from build_order."""
        from scripts.ddd.narrative import apply_narrative_edits

        # Start with a spec that has a custom build_order
        spec = _make_spec(
            build_order=["field-assignment", "area-selection", "sample-generation"]
        )
        spec_path = _write_spec(tmp_path, spec)

        # Delete "area-selection" scene
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": "",
                    "deleted": True,
                    "features": [],
                }
            ],
            "build_order": ["field-assignment", "area-selection", "sample-generation"],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert "area-selection" not in result["build_order"], (
            f"Deleted scene slug must be dropped from build_order: {result['build_order']}"
        )
        # Other slugs remain
        assert "field-assignment" in result["build_order"]
        assert "sample-generation" in result["build_order"]

        # Verified on disk too
        on_disk = yaml.safe_load(spec_path.read_text())
        assert "area-selection" not in on_disk.get("build_order", [])

    def test_apply_appends_new_scene_slug_to_build_order(self, tmp_path):
        """A newly added scene's slug is appended to the build_order."""
        from scripts.ddd.narrative import apply_narrative_edits

        spec = _make_spec(build_order=["area-selection", "sample-generation"])
        spec_path = _write_spec(tmp_path, spec)

        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "new-1",
                    "title": "Data Export",
                    "narration": "Program managers export data as CSV.",
                    "deleted": False,
                    "features": [],
                }
            ],
            "build_order": ["area-selection", "sample-generation"],
        }
        result = apply_narrative_edits(str(spec_path), response)
        # The new scene's slug must be appended
        assert "data-export" in result["build_order"], (
            f"New scene slug 'data-export' must be appended to build_order: {result['build_order']}"
        )
        # Original slugs preserved (field-assignment is NOT in spec, only area/sample)
        assert result["build_order"].index("data-export") > result["build_order"].index("area-selection")

    def test_apply_no_build_order_in_response_preserves_spec_value(self, tmp_path):
        """If response_json has no build_order key, spec's existing build_order is preserved."""
        from scripts.ddd.narrative import apply_narrative_edits

        existing_order = ["sample-generation", "area-selection", "field-assignment"]
        spec = _make_spec(build_order=existing_order)
        spec_path = _write_spec(tmp_path, spec)

        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
            # No build_order key
        }
        result = apply_narrative_edits(str(spec_path), response)
        # The existing spec order must be preserved in result
        assert result["build_order"] == existing_order

        on_disk = yaml.safe_load(spec_path.read_text())
        assert on_disk.get("build_order") == existing_order

    def test_apply_build_order_in_summary_returned(self, tmp_path):
        """The returned summary dict always includes a build_order key."""
        from scripts.ddd.narrative import apply_narrative_edits

        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
            "build_order": ["area-selection", "field-assignment", "sample-generation"],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert "build_order" in result

    def test_apply_build_order_invalid_slugs_dropped_not_raised(self, tmp_path):
        """build_order slugs that don't map to surviving scenes are silently dropped."""
        from scripts.ddd.narrative import apply_narrative_edits

        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
            "build_order": ["area-selection", "bogus-slug", "field-assignment"],
        }
        # Must NOT raise
        result = apply_narrative_edits(str(spec_path), response)
        assert "bogus-slug" not in result["build_order"]
        assert "area-selection" in result["build_order"]
        assert "field-assignment" in result["build_order"]

    def test_apply_build_order_legacy_shape_returns_build_order_key(self, tmp_path):
        """Even with the legacy narration_edits shape, result includes build_order."""
        from scripts.ddd.narrative import apply_narrative_edits

        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        response = {
            "decisions": {"narrative-verdict": "approve"},
            "narration_edits": {},
            "build_order": ["sample-generation", "area-selection", "field-assignment"],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert "build_order" in result


# ---------------------------------------------------------------------------
# 5. ReviewRequest model has build_order field
# ---------------------------------------------------------------------------


class TestReviewRequestBuildOrderField:
    def test_review_request_build_order_defaults_to_empty(self):
        rr = ReviewRequest(
            run_id="r1",
            gate="concept_change",
            video={},
            decisions=[],
            narration=[],
        )
        assert rr.build_order == []

    def test_review_request_accepts_build_order(self):
        order = ["scene-a", "scene-b"]
        rr = ReviewRequest(
            run_id="r1",
            gate="concept_change",
            video={},
            decisions=[],
            narration=[],
            build_order=order,
        )
        assert rr.build_order == order

    def test_review_request_build_order_in_model_dump(self):
        order = ["scene-a", "scene-b"]
        rr = ReviewRequest(
            run_id="r1",
            gate="concept_change",
            video={},
            decisions=[],
            narration=[],
            build_order=order,
        )
        dumped = rr.model_dump()
        assert "build_order" in dumped
        assert dumped["build_order"] == order
