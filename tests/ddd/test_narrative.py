"""Tests for scripts/ddd/narrative.py — narrative-agreement gate.

All functions under test are pure (no network, no disk-for-build, only
disk-for-apply).  The CLI's network-touching 'post' path is not tested here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts.ddd.schemas.models import Persona, ReviewRequest, Scene, UnifiedSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(scenes: list[Scene] | None = None) -> UnifiedSpec:
    if scenes is None:
        scenes = [
            Scene(
                persona="alice",
                title="Area Selection",
                show="Navigate to /areas and draw a boundary on the map.",
                concept_claim="Users can draw a custom boundary to select the survey area within 30 seconds.",
                provenance="S1",
            ),
            Scene(
                persona="alice",
                title="Sample Generation",
                show="Click 'Generate Sample' and review the building list.",
                concept_claim="The system generates a proportional building sample and displays it on the map.",
                provenance="S2",
            ),
            Scene(
                persona="bob",
                title="Field Assignment",
                show="Assign buildings to field workers from the team dashboard.",
                concept_claim="Supervisors can assign sampled buildings to field workers with a single tap.",
                provenance="S3",
            ),
        ]
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
        scenes=scenes,
    )


def _write_spec(tmp_path: Path, spec: UnifiedSpec) -> Path:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        yaml.dump(spec.model_dump(), default_flow_style=False, allow_unicode=True)
    )
    return spec_path


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------


from scripts.ddd.narrative import (
    _scene_id,
    apply_narrative_edits,
    build_narrative_review_request,
    is_narrative_locked,
    set_narrative_lock,
    web_narrative_to_spec_parts,
)


# ---------------------------------------------------------------------------
# build_narrative_review_request — pure function tests
# ---------------------------------------------------------------------------


class TestBuildNarrativeReviewRequest:
    def test_returns_review_request_instance(self):
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert isinstance(result, ReviewRequest)

    def test_gate_is_concept_change(self):
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert result.gate == "concept_change"

    def test_run_id_preserved(self):
        spec = _make_spec()
        result = build_narrative_review_request(spec, "my-run-42")
        assert result.run_id == "my-run-42"

    def test_narrative_slug_defaults_to_run_id_slug(self):
        """With no explicit narrative_slug, it's the run_id with the date stamp
        stripped — matching canopy-web's narrative_slug_from_run_id."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "verified-monitoring-2026-06-04-001")
        assert result.narrative_slug == "verified-monitoring"

    def test_explicit_narrative_slug_overrides_run_id_slug(self):
        """The explicit narrative_slug wins — this is what survives a slug
        rename, so the review files under the renamed narrative, not the old
        run_id slug."""
        spec = _make_spec()
        result = build_narrative_review_request(
            spec, "did-monitoring-2026-06-01-001", narrative_slug="verified-monitoring"
        )
        assert result.narrative_slug == "verified-monitoring"

    def test_narrative_slug_serialised_into_payload(self):
        """The cross-repo contract: canopy-web reads request_json.narrative_slug."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001", narrative_slug="my-narrative")
        assert result.model_dump(by_alias=True)["narrative_slug"] == "my-narrative"

    def test_video_is_empty_dict(self):
        """No cut yet — this is a pre-render narrative review."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert result.video == {}

    def test_autonomous_audit_is_empty(self):
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert result.autonomous_audit == []

    def test_narration_has_one_item_per_scene(self):
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert len(result.narration) == len(spec.scenes)

    def test_narration_items_have_required_keys(self):
        """Each narration item (NarrationItem) must have scene, id, and text."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        for item in result.narration:
            assert hasattr(item, "scene"), f"narration item missing 'scene': {item}"
            assert hasattr(item, "id"), f"narration item missing 'id': {item}"
            assert hasattr(item, "text"), f"narration item missing 'text': {item}"

    def test_narration_scene_indices_are_one_based(self):
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        for i, item in enumerate(result.narration, start=1):
            assert item.scene == i, (
                f"scene index mismatch at position {i}: expected {i}, got {item.scene}"
            )

    def test_narration_carries_title_and_persona(self):
        """v3: each narration item carries the scene's story-beat title and persona
        so the review surface can render the cohesive multi-persona narrative."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        for item, scene in zip(result.narration, spec.scenes):
            assert item.title == scene.title
            assert item.persona == scene.persona

    def test_request_carries_narrative_and_personas(self):
        """v3: the request carries the cohesive narrative + persona dict for the
        review surface header."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert result.narrative == spec.narrative
        assert set(result.personas.keys()) == set(spec.personas.keys())
        for key, persona in spec.personas.items():
            assert result.personas[key]["name"] == persona.name

    def test_narration_text_falls_back_to_concept_claim_when_sentences_mismatch(self):
        """When the narrative paragraph's sentence count doesn't match the scene
        count (here: 1 narrative sentence + 3 scenes), each narration item
        falls back to its scene.concept_claim — the read-side default for
        multi-sentence scenes or under-drafted paragraphs."""
        spec = _make_spec()
        assert len(spec.scenes) == 3
        result = build_narrative_review_request(spec, "run-001")
        for item, scene in zip(result.narration, spec.scenes):
            assert item.text == scene.concept_claim, (
                f"narration item text must fall back to the scene's concept_claim "
                f"when sentence count != scene count; got {item.text!r}, "
                f"expected {scene.concept_claim!r}"
            )

    def test_narration_text_is_literal_narrative_sentence_in_one_to_one_mode(self):
        """When the narrative paragraph has exactly one sentence per scene,
        each narration item's text is the LITERAL sentence at that position —
        so the reviewer reads the same prose top-to-bottom in the paragraph
        and in each scene card.

        This is the fix for the "concept_claim paraphrases the narrative
        sentence" drift the user caught on the microplans-10-wards spec.
        """
        scenes = [
            Scene(
                persona="alice",
                title="Beat 1",
                show="step 1",
                concept_claim="A paraphrased claim about beat 1.",
                provenance="S1",
            ),
            Scene(
                persona="alice",
                title="Beat 2",
                show="step 2",
                concept_claim="A paraphrased claim about beat 2.",
                provenance="S2",
            ),
        ]
        spec = _make_spec(scenes=scenes)
        # Override narrative to have exactly 2 sentences (one per scene).
        spec.narrative = (
            "Alice opens the workspace and reviews the area list. "
            "She then generates the sample for the chosen ward."
        )
        result = build_narrative_review_request(spec, "run-one-to-one")
        assert result.narration[0].text == "Alice opens the workspace and reviews the area list."
        assert result.narration[1].text == "She then generates the sample for the chosen ward."
        # concept_claim is NOT what's shown to the reviewer in this mode.
        for item, scene in zip(result.narration, spec.scenes):
            assert item.text != scene.concept_claim

    def test_narration_id_is_title_slug(self):
        """id must be the scene title lowercased with spaces replaced by hyphens."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        for item, scene in zip(result.narration, spec.scenes):
            expected_slug = re.sub(r"[^a-z0-9]+", "-", scene.title.lower()).strip("-")
            assert item.id == expected_slug, (
                f"narration item id must be a slug of the scene title; "
                f"got {item.id!r}, expected {expected_slug!r}"
            )

    def test_decisions_contains_one_narrative_verdict(self):
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert decision.id == "narrative-verdict"

    def test_decision_prompt_is_narrative_focused(self):
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        decision = result.decisions[0]
        # Must mention both narrative agreement and building/rendering
        assert "narrative" in decision.prompt.lower() or "story" in decision.prompt.lower()

    def test_decision_options_are_approve_redraft(self):
        """v3: options must be {approve, redraft} — the approve/redraft shape."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        decision = result.decisions[0]
        assert set(decision.options) == {"approve", "redraft"}, (
            f"decision options must be {{approve, redraft}}; got {decision.options}"
        )

    def test_decision_recommended_is_approve(self):
        """v3: recommended must be 'approve' (was 'agree' in v2)."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        decision = result.decisions[0]
        assert decision.recommended == "approve"

    def test_decision_class_is_concept_change(self):
        """Decision must use class_='concept_change' (stored under alias 'class')."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        decision = result.decisions[0]
        assert decision.class_ == "concept_change"

    def test_decision_model_dump_emits_class_alias(self):
        """model_dump(by_alias=True) must emit 'class', not 'class_'."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        decision = result.decisions[0]
        dumped = decision.model_dump(by_alias=True)
        assert "class" in dumped, f"alias 'class' not in dump; keys: {list(dumped)}"
        assert "class_" not in dumped, f"raw field 'class_' leaked into dump: {dumped}"
        assert dumped["class"] == "concept_change"

    def test_single_scene_spec(self):
        """Works correctly with a single scene."""
        spec = _make_spec(
            scenes=[
                Scene(
                    persona="alice",
                    title="Only Scene",
                    show="Navigate to home.",
                    concept_claim="Users can see the dashboard summary on first load.",
                    provenance="S1",
                )
            ]
        )
        # 1 scene + 1 narrative sentence triggers sentence-mode, so the
        # narration item's text is the LITERAL narrative sentence, not the
        # paraphrased concept_claim. The default _make_spec narrative is
        # "Rooftop surveys ride Connect microplanning."
        result = build_narrative_review_request(spec, "run-single")
        assert len(result.narration) == 1
        assert result.narration[0].scene == 1
        assert result.narration[0].text == "Rooftop surveys ride Connect microplanning."

    # -----------------------------------------------------------------------
    # v3: narration items carry per-scene features
    # -----------------------------------------------------------------------

    def test_narration_items_carry_features_from_scenes(self):
        """Each narration item must include the features declared on the matching scene."""
        from scripts.ddd.schemas.models import Feature

        spec = _make_spec(
            scenes=[
                Scene(
                    persona="alice",
                    title="Area Selection",
                    show="Navigate to /areas and draw a boundary.",
                    concept_claim="Users draw a boundary to select the survey area.",
                    provenance="S1",
                    features=[
                        Feature(
                            id="boundary-draw",
                            description="Polygon drawing widget on /areas map",
                            verify="Playwright: draw polygon, assert POST /areas returns 201",
                        ),
                        Feature(
                            id="area-persist",
                            description="Drawn area persists across page reloads",
                            verify="Reload /areas, assert polygon still visible in DOM",
                        ),
                    ],
                ),
                Scene(
                    persona="alice",
                    title="Sample Generation",
                    show="Click Generate Sample.",
                    concept_claim="System generates a proportional building sample.",
                    provenance="S2",
                    features=[
                        Feature(
                            id="sample-algo",
                            description="Proportional sampling algorithm",
                            verify="pytest: sampling returns floor-weighted buildings",
                        )
                    ],
                ),
            ]
        )
        result = build_narrative_review_request(spec, "run-features")
        # Scene 0 must carry its 2 features
        item0 = result.narration[0]
        assert hasattr(item0, "features") or isinstance(item0, dict), (
            "narration item must be a NarrationItem or dict with 'features' key"
        )
        feats0 = item0.features if hasattr(item0, "features") else item0.get("features", [])
        assert len(feats0) == 2, f"expected 2 features on scene 0, got {len(feats0)}"
        feat_ids = [f.id if hasattr(f, "id") else f["id"] for f in feats0]
        assert "boundary-draw" in feat_ids
        assert "area-persist" in feat_ids

        # Scene 1 must carry its 1 feature
        item1 = result.narration[1]
        feats1 = item1.features if hasattr(item1, "features") else item1.get("features", [])
        assert len(feats1) == 1

    def test_narration_items_have_empty_features_when_scene_has_none(self):
        """Narration items for scenes with no features must carry an empty features list."""
        spec = _make_spec()  # default scenes have no features
        result = build_narrative_review_request(spec, "run-no-features")
        for item in result.narration:
            feats = item.features if hasattr(item, "features") else item.get("features", [])
            assert feats == [], (
                f"narration item for scene without features must carry empty list; got {feats}"
            )

    # -----------------------------------------------------------------------
    # v3: actionability passthrough
    # -----------------------------------------------------------------------

    def test_actionability_defaults_to_none(self):
        """Without an actionability param, ReviewRequest.actionability is None."""
        spec = _make_spec()
        result = build_narrative_review_request(spec, "run-001")
        assert result.actionability is None

    def test_actionability_param_passes_through(self):
        """When actionability= is provided, it is set on the returned ReviewRequest."""
        spec = _make_spec()
        actionability = {
            "overall_score": 4.0,
            "per_scene": {"area-selection": {"score": 4.0, "missed": []}},
        }
        result = build_narrative_review_request(spec, "run-001", actionability=actionability)
        assert result.actionability is not None
        assert result.actionability["overall_score"] == 4.0
        assert "area-selection" in result.actionability["per_scene"]


# ---------------------------------------------------------------------------
# apply_narrative_edits — disk-touching pure transform
# ---------------------------------------------------------------------------


class TestApplyNarrativeEdits:
    def test_returns_dict_with_decision_and_edited(self, tmp_path):
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "agree"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert "decision" in result
        assert "edited" in result

    def test_approve_round_trips(self, tmp_path):
        """v3: 'approve' is the canonical go-forward decision."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "approve"

    def test_redraft_round_trips_directly(self, tmp_path):
        """v3: 'redraft' is the canonical loop-back decision."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "redraft"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "redraft"

    def test_returns_dict_with_decision_and_edited_on_unknown(self, tmp_path):
        """An unrecognised decision value passes through unchanged (unknown future values)."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "some-future-value"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert "decision" in result
        assert "edited" in result

    def test_no_edits_returns_zero_edited_count(self, tmp_path):
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "agree"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["edited"] == 0

    def test_applies_narration_edit_to_concept_claim(self, tmp_path):
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        # Build the slug for scene 0 ("Area Selection")
        scene0_slug = re.sub(r"[^a-z0-9]+", "-", "Area Selection".lower()).strip("-")
        new_claim = "Users draw a precise boundary in under 30 seconds using satellite imagery."

        response = {
            "decisions": {"narrative-verdict": "edit"},
            "narration_edits": {scene0_slug: new_claim},
        }
        apply_narrative_edits(str(spec_path), response)

        # Reload and verify
        updated = yaml.safe_load(spec_path.read_text())
        assert updated["scenes"][0]["concept_claim"] == new_claim

    def test_applies_edit_to_correct_scene_by_slug(self, tmp_path):
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        # Edit scene 2 ("Field Assignment") only
        scene2_slug = re.sub(r"[^a-z0-9]+", "-", "Field Assignment".lower()).strip("-")
        new_claim = "Supervisors assign buildings to FLWs with drag-and-drop, confirmed in one tap."

        response = {
            "decisions": {"narrative-verdict": "edit"},
            "narration_edits": {scene2_slug: new_claim},
        }
        apply_narrative_edits(str(spec_path), response)

        updated = yaml.safe_load(spec_path.read_text())
        # Scene 0 and 1 unchanged
        assert updated["scenes"][0]["concept_claim"] == spec.scenes[0].concept_claim
        assert updated["scenes"][1]["concept_claim"] == spec.scenes[1].concept_claim
        # Scene 2 updated
        assert updated["scenes"][2]["concept_claim"] == new_claim

    def test_edited_count_matches_changed_scenes(self, tmp_path):
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        scene0_slug = re.sub(r"[^a-z0-9]+", "-", "Area Selection".lower()).strip("-")
        scene1_slug = re.sub(r"[^a-z0-9]+", "-", "Sample Generation".lower()).strip("-")

        response = {
            "decisions": {"narrative-verdict": "edit"},
            "narration_edits": {
                scene0_slug: "New claim for scene 0.",
                scene1_slug: "New claim for scene 1.",
            },
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["edited"] == 2

    def test_unmatched_narration_id_is_silently_skipped(self, tmp_path):
        """Unknown keys in narration_edits must not raise errors."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        response = {
            "decisions": {"narrative-verdict": "agree"},
            "narration_edits": {
                "this-slug-does-not-exist": "Some text that should be ignored.",
                "another-bogus-slug": "More ignored text.",
            },
        }
        # Must NOT raise
        result = apply_narrative_edits(str(spec_path), response)
        assert result["edited"] == 0

        # Spec on disk must be unchanged
        updated = yaml.safe_load(spec_path.read_text())
        for i, scene in enumerate(spec.scenes):
            assert updated["scenes"][i]["concept_claim"] == scene.concept_claim

    def test_accepts_path_object(self, tmp_path):
        """apply_narrative_edits accepts a pathlib.Path, not only str."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        response = {"decisions": {"narrative-verdict": "approve"}, "narration_edits": {}}
        # Must not raise
        result = apply_narrative_edits(spec_path, response)
        assert result["decision"] == "approve"

    def test_missing_decisions_key_defaults_to_approve(self, tmp_path):
        """Robust default: if 'decisions' key absent, decision defaults to 'approve'."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {"narration_edits": {}}
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "approve"

    def test_missing_narration_edits_key_is_handled(self, tmp_path):
        """Robust default: if 'narration_edits' absent, no changes are applied."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {"decisions": {"narrative-verdict": "redraft"}}
        result = apply_narrative_edits(str(spec_path), response)
        assert result["edited"] == 0
        assert result["decision"] == "redraft"

    def test_writes_spec_back_to_disk(self, tmp_path):
        """After apply, spec file on disk must be valid YAML with updated content."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)

        scene0_slug = re.sub(r"[^a-z0-9]+", "-", "Area Selection".lower()).strip("-")
        response = {
            "decisions": {"narrative-verdict": "edit"},
            "narration_edits": {scene0_slug: "Updated claim."},
        }
        apply_narrative_edits(str(spec_path), response)

        # Must still be valid YAML
        updated = yaml.safe_load(spec_path.read_text())
        assert isinstance(updated, dict)
        assert "scenes" in updated

    # -----------------------------------------------------------------------
    # v3: approve / redraft vocabulary
    # -----------------------------------------------------------------------

    def test_approve_decision_round_trips(self, tmp_path):
        """v3: 'approve' is the canonical approval decision."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "approve"

    def test_redraft_decision_round_trips(self, tmp_path):
        """v3: 'redraft' triggers looping back to /ddd-spec."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "redraft"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "redraft"

    # -----------------------------------------------------------------------
    # v3: legacy compatibility (agree/edit → approve, rethink → redraft)
    # -----------------------------------------------------------------------

    def test_legacy_agree_treated_as_approve(self, tmp_path):
        """Legacy 'agree' decision must be coerced to 'approve' for safety."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "agree"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "approve", (
            f"legacy 'agree' must map to 'approve'; got {result['decision']!r}"
        )

    def test_legacy_edit_treated_as_approve(self, tmp_path):
        """Legacy 'edit' decision must be coerced to 'approve' for safety."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "edit"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "approve", (
            f"legacy 'edit' must map to 'approve'; got {result['decision']!r}"
        )

    def test_legacy_rethink_treated_as_redraft(self, tmp_path):
        """Legacy 'rethink' decision must be coerced to 'redraft' for safety."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "rethink"},
            "narration_edits": {},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "redraft", (
            f"legacy 'rethink' must map to 'redraft'; got {result['decision']!r}"
        )


# ---------------------------------------------------------------------------
# apply_narrative_edits — new edited_scenes shape
# ---------------------------------------------------------------------------


def _make_spec_with_features() -> UnifiedSpec:
    """Make a spec whose scenes carry concrete features for editing tests."""
    from scripts.ddd.schemas.models import Feature

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
                show="Navigate to /areas and draw a boundary on the map.",
                concept_claim="Users can draw a custom boundary to select the survey area within 30 seconds.",
                provenance="S1",
                features=[
                    Feature(
                        id="boundary-draw",
                        description="Polygon drawing widget on /areas map",
                        verify="Playwright: draw polygon, assert POST /areas returns 201",
                    ),
                    Feature(
                        id="area-persist",
                        description="Drawn area persists across page reloads",
                        verify="Reload /areas, assert polygon still visible in DOM",
                    ),
                ],
            ),
            Scene(
                persona="alice",
                title="Sample Generation",
                show="Click 'Generate Sample' and review the building list.",
                concept_claim="The system generates a proportional building sample and displays it on the map.",
                provenance="S2",
                features=[
                    Feature(
                        id="sample-algo",
                        description="Proportional sampling algorithm",
                        verify="pytest: sampling returns floor-weighted buildings",
                    ),
                ],
            ),
            Scene(
                persona="bob",
                title="Field Assignment",
                show="Assign buildings to field workers from the team dashboard.",
                concept_claim="Supervisors can assign sampled buildings to field workers with a single tap.",
                provenance="S3",
                features=[],
            ),
        ],
    )


class TestApplyNarrativeEditsNewShape:
    """Tests for the new ``edited_scenes`` payload shape."""

    # ------------------------------------------------------------------
    # Return-shape basics
    # ------------------------------------------------------------------

    def test_returns_decision_and_applied_and_needs_grounding_and_feedback(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert "decision" in result
        assert "applied" in result
        assert "needs_grounding" in result
        assert "feedback" in result
        applied = result["applied"]
        for key in ("updated", "added", "deleted", "features_changed"):
            assert key in applied, f"applied dict missing key {key!r}"

    def test_approve_decision_round_trips(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {"decisions": {"narrative-verdict": "approve"}, "edited_scenes": []}
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "approve"

    def test_redraft_decision_round_trips(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {"decisions": {"narrative-verdict": "redraft"}, "edited_scenes": []}
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "redraft"

    def test_legacy_agree_coerced_in_new_shape(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {"decisions": {"narrative-verdict": "agree"}, "edited_scenes": []}
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "approve"

    def test_legacy_rethink_coerced_in_new_shape(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {"decisions": {"narrative-verdict": "rethink"}, "edited_scenes": []}
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "redraft"

    # ------------------------------------------------------------------
    # Edit existing scene narration
    # ------------------------------------------------------------------

    def test_edit_existing_scene_narration_writes_to_scene_narrative_v2(self, tmp_path):
        """v2 (gap-flexible-scene-length): narration edits write to scene.narrative
        (the canonical per-scene field), not to concept_claim. concept_claim stays
        as a separate testable claim. spec.narrative is rebuilt from the per-scene
        narratives so the top paragraph stays consistent."""
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        new_claim = "Users draw a precise boundary in under 30 seconds using satellite imagery."
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": new_claim,
                    "deleted": False,
                    "features": [
                        {"id": "boundary-draw", "description": "Polygon drawing widget on /areas map",
                         "verify": "Playwright: draw polygon, assert POST /areas returns 201"},
                        {"id": "area-persist", "description": "Drawn area persists across page reloads",
                         "verify": "Reload /areas, assert polygon still visible in DOM"},
                    ],
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["updated"] == 1

        updated = yaml.safe_load(spec_path.read_text())
        # v2: writes to scene.narrative, not concept_claim.
        assert updated["scenes"][0]["narrative"] == new_claim
        # concept_claim left alone — it's a separate testable claim now.
        assert updated["scenes"][0]["concept_claim"] == spec.scenes[0].concept_claim
        # spec.narrative rebuilt to include the new per-scene text.
        assert new_claim in updated["narrative"]

    def test_edit_existing_scene_leaves_others_unchanged(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": "Updated narration for area selection.",
                    "deleted": False,
                    "features": [
                        {"id": "boundary-draw", "description": "Polygon drawing widget on /areas map",
                         "verify": "Playwright: draw polygon, assert POST /areas returns 201"},
                    ],
                }
            ],
        }
        apply_narrative_edits(str(spec_path), response)
        updated = yaml.safe_load(spec_path.read_text())
        # Scenes 1 and 2 untouched
        assert updated["scenes"][1]["concept_claim"] == spec.scenes[1].concept_claim
        assert updated["scenes"][2]["concept_claim"] == spec.scenes[2].concept_claim

    # ------------------------------------------------------------------
    # Feature reconciliation — edit verify on existing feature
    # ------------------------------------------------------------------

    def test_edit_feature_verify_updates_it(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        new_verify = "Playwright: draw polygon, assert POST /areas returns 201 and body has area_id"
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": spec.scenes[0].concept_claim,
                    "deleted": False,
                    "features": [
                        {"id": "boundary-draw", "description": "Polygon drawing widget on /areas map",
                         "verify": new_verify},
                        {"id": "area-persist", "description": "Drawn area persists across page reloads",
                         "verify": "Reload /areas, assert polygon still visible in DOM"},
                    ],
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["features_changed"] >= 1

        updated = yaml.safe_load(spec_path.read_text())
        feat_map = {f["id"]: f for f in updated["scenes"][0]["features"]}
        assert feat_map["boundary-draw"]["verify"] == new_verify

    # ------------------------------------------------------------------
    # Feature reconciliation — delete feature (absent from payload)
    # ------------------------------------------------------------------

    def test_feature_absent_from_payload_is_removed(self, tmp_path):
        """A feature present in the spec but NOT in the payload list is removed."""
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        # Send only boundary-draw; omit area-persist
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": spec.scenes[0].concept_claim,
                    "deleted": False,
                    "features": [
                        {"id": "boundary-draw", "description": "Polygon drawing widget on /areas map",
                         "verify": "Playwright: draw polygon, assert POST /areas returns 201"},
                    ],
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["features_changed"] >= 1

        updated = yaml.safe_load(spec_path.read_text())
        feat_ids = [f["id"] for f in updated["scenes"][0].get("features", [])]
        assert "boundary-draw" in feat_ids
        assert "area-persist" not in feat_ids, "area-persist should have been removed"

    # ------------------------------------------------------------------
    # Feature reconciliation — add new-* feature
    # ------------------------------------------------------------------

    def test_new_feature_id_appended_with_stable_id(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": spec.scenes[0].concept_claim,
                    "deleted": False,
                    "features": [
                        {"id": "boundary-draw", "description": "Polygon drawing widget on /areas map",
                         "verify": "Playwright: draw polygon, assert POST /areas returns 201"},
                        {"id": "area-persist", "description": "Drawn area persists across page reloads",
                         "verify": "Reload /areas, assert polygon still visible in DOM"},
                        {"id": "new-1", "description": "Undo last drawn polygon",
                         "verify": "Click undo, assert last polygon removed from map"},
                    ],
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["features_changed"] >= 1

        updated = yaml.safe_load(spec_path.read_text())
        feat_ids = [f["id"] for f in updated["scenes"][0].get("features", [])]
        assert "new-1" not in feat_ids, "new-* ids should be replaced with stable ids"
        # Should have 3 features now (2 original + 1 new)
        assert len(feat_ids) == 3

    # ------------------------------------------------------------------
    # Add new scene (new-* id)
    # ------------------------------------------------------------------

    def test_add_new_scene_appends_scene(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "new-1",
                    "title": "Data Export",
                    "narration": "Program managers can export survey data as CSV in one click.",
                    "deleted": False,
                    "features": [
                        {"id": "new-1", "description": "CSV export button on dashboard",
                         "verify": "Click export, assert CSV downloaded with correct headers"},
                    ],
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["added"] == 1
        assert "Data Export" in result["needs_grounding"]

        updated = yaml.safe_load(spec_path.read_text())
        titles = [s["title"] for s in updated["scenes"]]
        assert "Data Export" in titles

    def test_new_scene_has_empty_provenance(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "new-1",
                    "title": "Data Export",
                    "narration": "Program managers can export survey data as CSV.",
                    "deleted": False,
                    "features": [],
                }
            ],
        }
        apply_narrative_edits(str(spec_path), response)
        updated = yaml.safe_load(spec_path.read_text())
        new_scene = next(s for s in updated["scenes"] if s["title"] == "Data Export")
        assert new_scene["provenance"] == "", (
            f"new scene should have empty provenance, got {new_scene['provenance']!r}"
        )

    def test_new_scene_appears_in_needs_grounding(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "new-1",
                    "title": "Data Export",
                    "narration": "Program managers can export survey data as CSV.",
                    "deleted": False,
                    "features": [],
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert "Data Export" in result["needs_grounding"]

    def test_new_scene_features_get_stable_ids(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "new-1",
                    "title": "Data Export",
                    "narration": "Program managers can export survey data as CSV.",
                    "deleted": False,
                    "features": [
                        {"id": "new-1", "description": "CSV export button on dashboard",
                         "verify": "Click export, assert CSV downloaded"},
                        {"id": "new-2", "description": "Export includes all survey fields",
                         "verify": "Assert CSV headers match survey schema"},
                    ],
                }
            ],
        }
        apply_narrative_edits(str(spec_path), response)
        updated = yaml.safe_load(spec_path.read_text())
        new_scene = next(s for s in updated["scenes"] if s["title"] == "Data Export")
        feat_ids = [f["id"] for f in new_scene.get("features", [])]
        assert len(feat_ids) == 2
        for fid in feat_ids:
            assert not fid.startswith("new-"), f"new-* id not replaced: {fid!r}"

    # ------------------------------------------------------------------
    # Delete scene
    # ------------------------------------------------------------------

    def test_delete_scene_removes_it(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "field-assignment",
                    "title": "Field Assignment",
                    "narration": "",
                    "deleted": True,
                    "features": [],
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["deleted"] == 1

        updated = yaml.safe_load(spec_path.read_text())
        titles = [s["title"] for s in updated["scenes"]]
        assert "Field Assignment" not in titles
        # Other scenes still present
        assert "Area Selection" in titles
        assert "Sample Generation" in titles

    def test_delete_scene_count_is_correct(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {"id": "area-selection", "title": "Area Selection", "narration": "",
                 "deleted": True, "features": []},
                {"id": "sample-generation", "title": "Sample Generation", "narration": "",
                 "deleted": True, "features": []},
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["deleted"] == 2

        updated = yaml.safe_load(spec_path.read_text())
        assert len(updated["scenes"]) == 1
        assert updated["scenes"][0]["title"] == "Field Assignment"

    # ------------------------------------------------------------------
    # Feedback collection
    # ------------------------------------------------------------------

    def test_per_feature_feedback_collected(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": spec.scenes[0].concept_claim,
                    "deleted": False,
                    "features": [
                        {"id": "boundary-draw",
                         "description": "Polygon drawing widget on /areas map",
                         "verify": "Playwright: draw polygon, assert POST /areas returns 201",
                         "feedback": "The verify step should also check the response body."},
                        {"id": "area-persist",
                         "description": "Drawn area persists across page reloads",
                         "verify": "Reload /areas, assert polygon still visible in DOM"},
                    ],
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        feature_feedbacks = [f for f in result["feedback"] if f["scope"] == "feature"]
        assert len(feature_feedbacks) == 1
        fb = feature_feedbacks[0]
        assert fb["ref"] == "boundary-draw"
        assert "verify" in fb["text"].lower() or "body" in fb["text"].lower()

    def test_per_scene_feedback_collected(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "sample-generation",
                    "title": "Sample Generation",
                    "narration": spec.scenes[1].concept_claim,
                    "deleted": False,
                    "features": [
                        {"id": "sample-algo",
                         "description": "Proportional sampling algorithm",
                         "verify": "pytest: sampling returns floor-weighted buildings"},
                    ],
                    "feedback": "Consider showing the sampling ratio to the user.",
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        scene_feedbacks = [f for f in result["feedback"] if f["scope"] == "scene"]
        assert len(scene_feedbacks) == 1
        assert scene_feedbacks[0]["ref"] == "sample-generation"
        assert "sampling" in scene_feedbacks[0]["text"].lower()

    def test_overall_feedback_collected(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
            "overall_feedback": "Overall the narrative flows well but needs more FLW perspective.",
        }
        result = apply_narrative_edits(str(spec_path), response)
        overall_feedbacks = [f for f in result["feedback"] if f["scope"] == "overall"]
        assert len(overall_feedbacks) == 1
        assert "flw" in overall_feedbacks[0]["text"].lower() or "narrative" in overall_feedbacks[0]["text"].lower()

    def test_feedback_scope_and_ref_are_correct(self, tmp_path):
        """feature/scene/overall feedback each have correct scope and ref."""
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": spec.scenes[0].concept_claim,
                    "deleted": False,
                    "features": [
                        {"id": "boundary-draw",
                         "description": "Polygon drawing widget on /areas map",
                         "verify": "Playwright: draw polygon, assert POST /areas returns 201",
                         "feedback": "feature-level feedback"},
                    ],
                    "feedback": "scene-level feedback",
                }
            ],
            "overall_feedback": "overall feedback",
        }
        result = apply_narrative_edits(str(spec_path), response)
        fb_by_scope = {f["scope"]: f for f in result["feedback"]}
        assert "feature" in fb_by_scope
        assert "scene" in fb_by_scope
        assert "overall" in fb_by_scope
        assert fb_by_scope["feature"]["ref"] == "boundary-draw"
        assert fb_by_scope["scene"]["ref"] == "area-selection"
        assert fb_by_scope["overall"]["ref"] == ""

    def test_no_feedback_returns_empty_list(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": spec.scenes[0].concept_claim,
                    "deleted": False,
                    "features": [
                        {"id": "boundary-draw", "description": "Polygon drawing widget on /areas map",
                         "verify": "Playwright: draw polygon, assert POST /areas returns 201"},
                    ],
                }
            ],
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["feedback"] == []

    # ------------------------------------------------------------------
    # Legacy narration_edits still works
    # ------------------------------------------------------------------

    def test_legacy_narration_edits_still_works(self, tmp_path):
        """The old narration_edits dict shape must still apply edits correctly."""
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        slug = re.sub(r"[^a-z0-9]+", "-", "Area Selection".lower()).strip("-")
        new_claim = "Drawn boundary persists and is shareable."
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "narration_edits": {slug: new_claim},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "approve"
        # backward-compat key
        assert "edited" in result
        assert result["edited"] == 1

        updated = yaml.safe_load(spec_path.read_text())
        assert updated["scenes"][0]["concept_claim"] == new_claim

    # ------------------------------------------------------------------
    # redraft round-trips through new shape
    # ------------------------------------------------------------------

    def test_redraft_new_shape_round_trips(self, tmp_path):
        spec = _make_spec_with_features()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "redraft"},
            "edited_scenes": [
                {
                    "id": "area-selection",
                    "title": "Area Selection",
                    "narration": "Revised: users can draw a custom boundary in 15 seconds.",
                    "deleted": False,
                    "features": [
                        {"id": "boundary-draw",
                         "description": "Polygon drawing widget on /areas map",
                         "verify": "Playwright: draw polygon, assert POST /areas returns 201"},
                    ],
                    "feedback": "Too slow — target 15s not 30s.",
                }
            ],
            "overall_feedback": "Needs tighter time targets throughout.",
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["decision"] == "redraft"
        assert result["applied"]["updated"] == 1
        feedbacks = result["feedback"]
        scopes = {f["scope"] for f in feedbacks}
        assert "scene" in scopes
        assert "overall" in scopes


# ---------------------------------------------------------------------------
# v3: persona org field + why-brief carry + editable personas / why-brief
# ---------------------------------------------------------------------------


class TestPersonasAndWhyBrief:
    def test_persona_org_round_trips_into_request(self):
        from scripts.ddd.schemas.models import Feature, Persona, UnifiedSpec

        spec = UnifiedSpec(
            name="t",
            narrative="A one-beat demo.",
            base_url="https://x",
            personas={
                "maya": Persona(
                    name="Maya",
                    role="Program lead",
                    color="#3B82F6",
                    intro="Maya designs the plan.",
                    org="Dimagi",
                )
            },
            scenes=[
                Scene(
                    persona="maya",
                    title="Maya designs the plan",
                    show="open the map",
                    concept_claim="Maya designs the plan from real boundaries.",
                    provenance="S1",
                    features=[Feature(id="f1", description="d", verify="pytest: ok")],
                )
            ],
        )
        req = build_narrative_review_request(spec, "run-1")
        assert req.personas["maya"]["org"] == "Dimagi"

    def test_build_carries_why_brief(self):
        spec = _make_spec()
        wb = {"problem": "P", "spine": [{"id": "S1", "claim": "c"}], "gaps": []}
        req = build_narrative_review_request(spec, "run-1", why_brief=wb)
        assert req.why_brief == wb
        # default is empty dict when not provided
        assert build_narrative_review_request(spec, "run-1").why_brief == {}

    def test_apply_persona_edits_persists_to_spec(self, tmp_path):
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
            "edited_personas": {
                "alice": {"name": "Alice Q", "org": "Dimagi", "role": "M&E Lead"}
            },
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["personas_changed"] == 3
        reloaded = yaml.safe_load(spec_path.read_text())
        assert reloaded["personas"]["alice"]["name"] == "Alice Q"
        assert reloaded["personas"]["alice"]["org"] == "Dimagi"
        assert reloaded["personas"]["alice"]["role"] == "M&E Lead"
        # untouched persona unchanged
        assert reloaded["personas"]["bob"]["name"] == "Bob"

    def test_apply_persona_edits_ignores_unknown_key(self, tmp_path):
        spec = _make_spec()
        spec_path = _write_spec(tmp_path, spec)
        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
            "edited_personas": {"ghost": {"name": "Nobody"}},
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["personas_changed"] == 0

    def test_apply_why_brief_edits_persists_to_why_brief_file(self, tmp_path):
        # why-brief lives next to the spec; spec.why_brief points at it
        wb = {
            "schema_version": 1,
            "feature": "t",
            "problem": "old problem",
            "spine": [
                {"id": "S1", "claim": "old claim", "rationale": "old rat", "status": "grounded",
                 "evidence": [{"kind": "documented", "ref": "doc"}]},
            ],
            "gaps": [
                {"id": "g1", "type": "CAPABILITY", "claim_ref": "S1",
                 "detail": "old detail", "proposed_action": "old action"},
            ],
        }
        wb_path = tmp_path / "why-brief.yaml"
        wb_path.write_text(yaml.dump(wb))

        spec = _make_spec()
        raw = spec.model_dump()
        raw["why_brief"] = "why-brief.yaml"
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True))

        response = {
            "decisions": {"narrative-verdict": "approve"},
            "edited_scenes": [],
            "edited_why_brief": {
                "problem": "new problem",
                "spine": {"S1": {"claim": "new claim"}},
                "gaps": {"g1": {"proposed_action": "new action"}},
            },
        }
        result = apply_narrative_edits(str(spec_path), response)
        assert result["applied"]["why_brief_changed"] == 3
        reloaded = yaml.safe_load(wb_path.read_text())
        assert reloaded["problem"] == "new problem"
        assert reloaded["spine"][0]["claim"] == "new claim"
        assert reloaded["spine"][0]["rationale"] == "old rat"  # untouched
        assert reloaded["gaps"][0]["proposed_action"] == "new action"
        assert reloaded["gaps"][0]["detail"] == "old detail"  # untouched


# ---------------------------------------------------------------------------
# Narrative lock — approve makes the narrative durable input; redraft clears it
# ---------------------------------------------------------------------------


class TestNarrativeLock:
    def _approve(self, decision="approve"):
        return {"decisions": {"narrative-verdict": decision}, "edited_scenes": []}

    def test_approve_locks_the_spec(self, tmp_path):
        spec_path = _write_spec(tmp_path, _make_spec())
        assert is_narrative_locked(spec_path) is False
        result = apply_narrative_edits(str(spec_path), self._approve("approve"))
        assert result["narrative_locked"] is True
        reloaded = yaml.safe_load(spec_path.read_text())
        assert reloaded["narrative_locked"] is True
        assert reloaded.get("narrative_locked_at")  # timestamp stamped
        assert is_narrative_locked(spec_path) is True

    def test_redraft_clears_the_lock(self, tmp_path):
        spec_path = _write_spec(tmp_path, _make_spec())
        apply_narrative_edits(str(spec_path), self._approve("approve"))
        assert is_narrative_locked(spec_path) is True
        result = apply_narrative_edits(str(spec_path), self._approve("redraft"))
        assert result["narrative_locked"] is False
        assert is_narrative_locked(spec_path) is False
        reloaded = yaml.safe_load(spec_path.read_text())
        assert "narrative_locked_at" not in reloaded

    def test_approve_persists_lock_even_with_no_edits_legacy_shape(self, tmp_path):
        # Legacy shape (no edited_scenes, no narration_edits) must still write the lock.
        spec_path = _write_spec(tmp_path, _make_spec())
        result = apply_narrative_edits(
            str(spec_path), {"decisions": {"narrative-verdict": "agree"}}
        )
        assert result["decision"] == "approve"  # 'agree' normalises to 'approve'
        assert result["narrative_locked"] is True
        assert is_narrative_locked(spec_path) is True

    def test_lock_preserves_full_spec_scenes_verbatim(self, tmp_path):
        # The whole spec (scenes incl. show/design_intent) survives a lock round-trip.
        spec = _make_spec()
        spec.scenes[0].design_intent = "A dense KPI strip with monospace numerals."
        spec_path = _write_spec(tmp_path, spec)
        apply_narrative_edits(str(spec_path), self._approve("approve"))
        reloaded = UnifiedSpec.model_validate(yaml.safe_load(spec_path.read_text()))
        assert reloaded.narrative_locked is True
        assert reloaded.scenes[0].design_intent == "A dense KPI strip with monospace numerals."
        assert reloaded.scenes[0].show == spec.scenes[0].show
        assert [s.title for s in reloaded.scenes] == [s.title for s in spec.scenes]

    def test_is_narrative_locked_missing_file(self, tmp_path):
        assert is_narrative_locked(tmp_path / "nope.yaml") is False

    def test_set_narrative_lock_explicit(self, tmp_path):
        spec_path = _write_spec(tmp_path, _make_spec())
        r1 = set_narrative_lock(spec_path, True)
        assert r1 == {"narrative_locked": True, "changed": True}
        assert is_narrative_locked(spec_path) is True
        # idempotent: locking again is a no-op
        r2 = set_narrative_lock(spec_path, True)
        assert r2 == {"narrative_locked": True, "changed": False}
        r3 = set_narrative_lock(spec_path, False)
        assert r3 == {"narrative_locked": False, "changed": True}
        assert is_narrative_locked(spec_path) is False


# ---------------------------------------------------------------------------
# Deterministic run_state stamping — replaces the old hand-run snippet
# ---------------------------------------------------------------------------

class TestStampRunState:
    def test_tokenized_url_appends_share_token(self):
        from scripts.ddd.narrative import _tokenized_review_url

        out = _tokenized_review_url(
            {"url": "https://c/review/abc/", "share_token": "tok9"}
        )
        assert out == "https://c/review/abc/?t=tok9"

    def test_tokenized_url_keeps_already_tokenized(self):
        from scripts.ddd.narrative import _tokenized_review_url

        out = _tokenized_review_url(
            {"url": "https://c/review/abc/?t=existing", "share_token": "tok9"}
        )
        assert out == "https://c/review/abc/?t=existing"

    def test_internal_url_strips_token_and_is_absolute(self):
        from scripts.ddd.narrative import _internal_review_url

        # server returns a RELATIVE, token-bearing url — internal must drop the
        # token and absolutize against base, so it opens with the left rail.
        out = _internal_review_url(
            {"id": "abc-123", "url": "/review/abc-123/?t=TOK", "share_token": "TOK"},
            "https://c",
        )
        assert out == "https://c/review/abc-123/"
        assert "?t=" not in out

    def test_internal_url_falls_back_to_url_path_without_id(self):
        from scripts.ddd.narrative import _internal_review_url

        out = _internal_review_url(
            {"id": "", "url": "https://c/review/zzz/?t=Q", "share_token": "Q"},
            "https://c",
        )
        assert out == "https://c/review/zzz/"

    def test_internal_url_differs_from_share_url(self):
        from scripts.ddd.narrative import _internal_review_url, _tokenized_review_url

        result = {"id": "d-9", "url": "/review/d-9/?t=Z", "share_token": "Z"}
        internal = _internal_review_url(result, "https://c")
        share = _tokenized_review_url(result)
        assert "?t=" not in internal
        assert "?t=" in share

    def test_narrative_slug_from_run_id(self):
        from scripts.ddd.narrative import _narrative_slug_from_run_id

        assert _narrative_slug_from_run_id("verified-monitoring-2026-06-04-001") == "verified-monitoring"
        assert _narrative_slug_from_run_id("nostampslug") == "nostampslug"

    def test_stamp_writes_id_and_url(self, tmp_path, monkeypatch):
        import scripts.ddd.runstate as rs
        from scripts.ddd.narrative import _stamp_run_state
        from scripts.ddd.schemas.models import RunState

        monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: tmp_path)
        run_id = "verified-monitoring-2026-06-04-001"
        rs.save(RunState(run_id=run_id, narrative_slug="verified-monitoring", phase="converged"))

        _stamp_run_state(
            run_id,
            {"id": "rev-uuid-1", "url": "https://c/review/rev-uuid-1/", "share_token": "t0"},
        )

        reloaded = rs.load(run_id)
        assert reloaded.narrative_review_id == "rev-uuid-1"
        assert reloaded.narrative_review_url == "https://c/review/rev-uuid-1/?t=t0"

    def test_stamp_missing_run_state_warns_not_raises(self, tmp_path, monkeypatch, capsys):
        import scripts.ddd.runstate as rs
        from scripts.ddd.narrative import _stamp_run_state

        monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: tmp_path)
        # No run_state on disk — must warn, not crash (the post already succeeded).
        _stamp_run_state("ghost-2026-01-01-001", {"id": "r", "url": "https://c/review/r/"})
        assert "WARNING" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Narrative sync (hydrate web → disk) — pull decision + merge
# ---------------------------------------------------------------------------

class _FakeReview:
    """A fake `scripts.ddd.review` module for auto-version tests.

    Records `post_review_request` calls and serves `get_narrative` with a
    `current_version.version` that increments on each post — mirroring
    canopy-web assigning the next monotonic version AT POST TIME and treating
    the latest-posted version as current (independent of pending/resolved).
    `web_version_override` simulates web having advanced underneath the run.
    """

    def __init__(self, start_version: int = 0, web_version_override: int | None = None):
        self._version = start_version
        self._web_version_override = web_version_override
        self.posts: list = []

    # Network-touching API used by post_narrative_version / _stamp_spec_sync.
    def post_review_request(self, request, **kwargs):
        self._version += 1
        self.posts.append(request)
        rid = f"rev-{self._version}"
        return {"id": rid, "url": f"https://c/review/{rid}/", "share_token": "t"}

    def get_narrative(self, slug, **kwargs):
        v = self._web_version_override if self._web_version_override is not None else self._version
        if v <= 0:
            return None
        return {"current_version": {"version": v}}

    def _resolve_base_url(self, base):
        return "https://c"


def _seed_run_state(tmp_path, monkeypatch, run_id: str, narrative_slug: str):
    import scripts.ddd.runstate as rs
    from scripts.ddd.schemas.models import RunState

    monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: tmp_path)
    rs.save(RunState(run_id=run_id, narrative_slug=narrative_slug, phase="converged"))
    return rs


class _SyncFakeReview(_FakeReview):
    """`_FakeReview` plus a `get_review` that serves a RESOLVED review carrying
    inline edits, so `sync` can fold web edits and then auto-version them."""

    def __init__(self, *, response_json, status="resolved", **kw):
        super().__init__(**kw)
        self._response_json = response_json
        self._status = status

    def get_review(self, review_id, **kwargs):
        return {
            "request_json": {},
            "response_json": self._response_json,
            "status": self._status,
            "is_owner": True,
        }


def _seed_run_state_with_review(tmp_path, monkeypatch, run_id, slug, review_id):
    import scripts.ddd.runstate as rs
    from scripts.ddd.schemas.models import RunState

    monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: tmp_path)
    rs.save(
        RunState(
            run_id=run_id,
            narrative_slug=slug,
            phase="converged",
            narrative_review_id=review_id,
        )
    )
    return rs


def test_scene_id_prefers_explicit_id_over_title():
    scene = Scene(
        id="the-goal",
        persona="alice",
        title="A title that will be reworded later",
        show="Open the dashboard.",
        concept_claim="The dashboard loads in under two seconds.",
        provenance="S1",
    )
    assert _scene_id(scene) == "the-goal"


def test_scene_id_falls_back_to_title_slug_when_unset():
    scene = Scene(
        persona="alice",
        title="Area Selection",
        show="Draw a boundary.",
        concept_claim="A boundary can be drawn in under 30 seconds.",
        provenance="S1",
    )
    assert _scene_id(scene) == "area-selection"


def test_scene_id_accepts_a_raw_dict():
    assert _scene_id({"id": "the-goal", "title": "Anything"}) == "the-goal"
    assert _scene_id({"title": "Area Selection"}) == "area-selection"


def test_scene_id_treats_whitespace_only_id_as_absent():
    assert _scene_id({"id": "   ", "title": "Area Selection"}) == "area-selection"


def test_review_request_narration_id_is_stable_across_a_title_reword():
    def spec_with_title(title: str) -> UnifiedSpec:
        return _make_spec([
            Scene(
                id="the-goal",
                persona="alice",
                title=title,
                show="Open the dashboard.",
                concept_claim="The dashboard loads in under two seconds.",
                provenance="S1",
            )
        ])

    before = build_narrative_review_request(
        spec_with_title("Original wording"), "demo-2026-07-26-001"
    )
    after = build_narrative_review_request(
        spec_with_title("Completely different wording"), "demo-2026-07-26-002"
    )

    assert before.narration[0].id == "the-goal"
    assert after.narration[0].id == "the-goal"


def test_build_order_defaults_to_explicit_scene_ids():
    spec = _make_spec([
        Scene(id="the-goal", persona="alice", title="Some Title", show="x",
              concept_claim="A claim with at least five words.", provenance="S1"),
        Scene(id="the-proof", persona="alice", title="Another Title", show="y",
              concept_claim="Another claim with at least five words.", provenance="S2"),
    ])
    req = build_narrative_review_request(spec, "demo-2026-07-26-001")
    assert req.build_order == ["the-goal", "the-proof"]


def test_apply_edits_matches_on_scene_id_not_title(tmp_path):
    spec_path = tmp_path / "demo.yaml"
    spec_path.write_text(yaml.dump({
        "name": "demo",
        "narrative": "The old spoken line.",
        "base_url": "http://localhost:8000",
        "personas": {"alice": {"name": "Alice", "role": "PM"}},
        "scenes": [{
            "id": "the-goal",
            "persona": "alice",
            "title": "A title nobody edits",
            "show": "css:text=/^Dashboard$/",
            "concept_claim": "The dashboard loads in under two seconds.",
            "provenance": "S1",
            "narrative": "The old spoken line.",
        }],
    }))

    result = apply_narrative_edits(spec_path, {
        "decisions": {"narrative-verdict": "approve"},
        "edited_scenes": [{"id": "the-goal", "narration": "The new spoken line."}],
    })

    assert result["applied"]["updated"] == 1
    raw = yaml.safe_load(spec_path.read_text())
    assert raw["scenes"][0]["narrative"] == "The new spoken line."
    assert raw["scenes"][0]["show"] == "css:text=/^Dashboard$/"


def test_apply_edits_writes_an_explicit_id_on_a_newly_added_scene(tmp_path):
    spec_path = tmp_path / "demo.yaml"
    spec_path.write_text(yaml.dump({
        "name": "demo",
        "narrative": "The only line.",
        "base_url": "http://localhost:8000",
        "personas": {"alice": {"name": "Alice", "role": "PM"}},
        "scenes": [{
            "id": "the-goal", "persona": "alice", "title": "The goal",
            "show": "x", "concept_claim": "A claim with at least five words.",
            "provenance": "S1", "narrative": "The only line.",
        }],
    }))

    apply_narrative_edits(spec_path, {
        "decisions": {"narrative-verdict": "approve"},
        "edited_scenes": [{
            "id": "new-1", "title": "The proof", "narration": "A brand new beat.",
        }],
    })

    raw = yaml.safe_load(spec_path.read_text())
    added = raw["scenes"][-1]
    assert added["id"] == "the-proof"
    assert added["title"] == "The proof"


def test_pull_maps_web_narration_text_to_scene_narrative_not_concept_claim():
    parts = web_narrative_to_spec_parts({
        "narrative_slug": "demo",
        "narrative": "The whole story.",
        "personas": {},
        "build_order": [],
        "narration": [{
            "id": "the-goal",
            "title": "The goal",
            "persona": "alice",
            "provenance": "S1",
            "text": "The line the reviewer approved.",
            "features": [],
        }],
    })

    scene = parts["scenes"][0]
    assert scene["id"] == "the-goal"
    assert scene["narrative"] == "The line the reviewer approved."
    assert "concept_claim" not in scene


def test_pull_falls_back_to_the_title_slug_for_a_legacy_id_less_narration():
    parts = web_narrative_to_spec_parts({
        "narrative_slug": "demo",
        "narrative": "The whole story.",
        "personas": {},
        "build_order": [],
        "narration": [{"title": "Area Selection", "text": "A line.", "features": []}],
    })
    assert parts["scenes"][0]["id"] == "area-selection"


