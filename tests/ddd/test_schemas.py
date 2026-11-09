"""Tests for DDD schema models (SP0.1 + SP0.2)."""
import pytest
import pydantic


# ---------------------------------------------------------------------------
# SP0.1 — WhyBrief schema
# ---------------------------------------------------------------------------

def test_why_brief_round_trip():
    from scripts.ddd.schemas.models import WhyBrief, SpineItem, Gap

    wb = WhyBrief(
        narrative_slug="Rooftop Survey Sampling",
        problem="We lack a systematic way to sample rooftops.",
        spine=[
            SpineItem(id="S1", claim="Sampling is needed", rationale="without it we miss units"),
        ],
        gaps=[
            Gap(
                id="G1",
                type="RESEARCH",
                claim_ref="S1",
                detail="No baseline data",
                proposed_action="commission survey",
            )
        ],
    )
    d = wb.model_dump()
    assert d["schema_version"] == 1
    assert d["narrative_slug"] == "Rooftop Survey Sampling"
    assert d["spine"][0]["id"] == "S1"
    assert d["gaps"][0]["type"] == "RESEARCH"


def test_gap_invalid_type_raises():
    from scripts.ddd.schemas.models import Gap

    with pytest.raises(pydantic.ValidationError):
        Gap(
            id="G1",
            type="BOGUS",
            claim_ref="S1",
            detail="x",
            proposed_action="y",
        )


# ---------------------------------------------------------------------------
# SP0.2 — Remaining models
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Feature model (v3 — actionable narratives)
# ---------------------------------------------------------------------------

def test_feature_round_trip():
    from scripts.ddd.schemas.models import Feature

    f = Feature(
        id="F1",
        description="Display a filterable list of tasks sorted by due date",
        verify="GET /api/tasks?status=open returns tasks in ascending due_date order",
    )
    d = f.model_dump()
    assert d["id"] == "F1"
    assert d["description"] == "Display a filterable list of tasks sorted by due date"
    assert d["verify"] == "GET /api/tasks?status=open returns tasks in ascending due_date order"


def test_feature_missing_id_raises():
    from scripts.ddd.schemas.models import Feature

    with pytest.raises(pydantic.ValidationError):
        Feature(
            description="Some description",
            verify="Run pytest tests/test_foo.py",
        )


def test_feature_missing_description_raises():
    from scripts.ddd.schemas.models import Feature

    with pytest.raises(pydantic.ValidationError):
        Feature(
            id="F1",
            verify="Run pytest tests/test_foo.py",
        )


def test_feature_missing_verify_raises():
    from scripts.ddd.schemas.models import Feature

    with pytest.raises(pydantic.ValidationError):
        Feature(
            id="F1",
            description="Some description",
        )


def test_scene_round_trips_with_features():
    from scripts.ddd.schemas.models import Scene, Feature

    scene = Scene(
        persona="alice",
        title="Filter task list",
        show="navigate to /tasks, click Status filter, select Open",
        concept_claim="Users can filter the task list by status and see only open tasks without a page reload",
        provenance="S1",
        features=[
            Feature(
                id="task-filter-ui",
                description="Status dropdown filter on the task list page",
                verify="Selenium: select 'Open' in the Status dropdown, assert only open tasks visible",
            ),
            Feature(
                id="task-filter-api",
                description="GET /tasks?status= endpoint filters tasks by status",
                verify="pytest: GET /tasks?status=open returns 200 with tasks all having status=open",
            ),
        ],
    )
    d = scene.model_dump()
    assert len(d["features"]) == 2
    assert d["features"][0]["id"] == "task-filter-ui"
    assert d["features"][1]["id"] == "task-filter-api"


def test_scene_features_defaults_to_empty_list():
    from scripts.ddd.schemas.models import Scene

    scene = Scene(
        persona="alice",
        title="Submit Form",
        show="navigate to /form, fill fields, click Submit",
        concept_claim="Users can submit the form and see a confirmation message",
        provenance="S1",
    )
    assert scene.features == []


def test_scene_missing_required_fields_raises():
    from scripts.ddd.schemas.models import Scene

    with pytest.raises(pydantic.ValidationError):
        # concept_claim and provenance are both missing
        Scene(persona="p1", title="My Scene", show="navigate to /home")


def test_verdict_invalid_verdict_raises():
    from scripts.ddd.schemas.models import Verdict, Dimension

    with pytest.raises(pydantic.ValidationError):
        Verdict(
            dimensions={"clarity": Dimension(score=7.0, weight=1.0)},
            overall_score=7.0,
            verdict="bogus",
        )


def test_verdict_valid_pass():
    from scripts.ddd.schemas.models import Verdict, Dimension

    v = Verdict(
        dimensions={"clarity": Dimension(score=8.0, weight=1.0)},
        overall_score=8.0,
        verdict="pass",
    )
    assert v.verdict == "pass"
    assert v.schema_version == 1
    assert v.blocking_reason is None
    assert v.fix_recommendation is None


def test_decision_serializes_class_as_alias():
    from scripts.ddd.schemas.models import Decision

    d = Decision(
        id="D1",
        prompt="Which approach?",
        options=["A", "B"],
        recommended="A",
        class_="SCOPE",
    )
    dumped = d.model_dump(by_alias=True)
    assert "class" in dumped
    assert dumped["class"] == "SCOPE"
    # Should not have the Python name "class_" in aliased output
    assert "class_" not in dumped


def test_decision_default_dump_emits_class_not_class_underscore():
    """Regression: plain model_dump() / model_dump_json() must emit 'class', not 'class_'."""
    from scripts.ddd.schemas.models import Decision, ReviewRequest

    d = Decision(
        id="D1",
        prompt="Which approach?",
        options=["A", "B"],
        recommended="A",
        class_="SCOPE",
    )
    # Default model_dump() (no by_alias argument) must emit "class"
    plain_dump = d.model_dump()
    assert "class" in plain_dump, "model_dump() must emit 'class' key by default"
    assert "class_" not in plain_dump, "model_dump() must NOT emit 'class_' by default"

    # model_dump_json() must also emit "class"
    json_str = d.model_dump_json()
    assert '"class"' in json_str, "model_dump_json() must emit '\"class\"' by default"
    assert '"class_"' not in json_str, "model_dump_json() must NOT emit '\"class_\"' by default"

    # And through ReviewRequest (the real consumer path)
    rr = ReviewRequest(
        run_id="run-001",
        gate="phase1",
        video={"url": "http://example.com/v.mp4"},
        decisions=[d],
        narration=[{"text": "hello"}],
    )
    rr_json = rr.model_dump_json()
    assert '"class"' in rr_json, "ReviewRequest.model_dump_json() must emit '\"class\"'"
    assert '"class_"' not in rr_json, "ReviewRequest.model_dump_json() must NOT emit '\"class_\"'"


def test_decision_loads_from_class_key_and_class_kwarg():
    """Decision must accept both {'class': ...} dict input and class_=... kwarg."""
    from scripts.ddd.schemas.models import Decision

    # Load from dict with "class" key (JSON/YAML deserialization path)
    d1 = Decision.model_validate({
        "id": "D1",
        "prompt": "p",
        "options": ["A"],
        "recommended": "A",
        "class": "SCOPE",
    })
    assert d1.class_ == "SCOPE"

    # Load using Python kwarg class_=
    d2 = Decision(
        id="D2",
        prompt="p",
        options=["A"],
        recommended="A",
        class_="SCOPE",
    )
    assert d2.class_ == "SCOPE"


# ---------------------------------------------------------------------------
# NarrationItem model (v3 — carries per-scene features)
# ---------------------------------------------------------------------------

def test_narration_item_round_trip():
    from scripts.ddd.schemas.models import NarrationItem, Feature

    item = NarrationItem(
        scene=0,
        id="area-selection",
        text="Users draw a boundary and generate a sample in under 30 seconds.",
        features=[
            Feature(
                id="boundary-draw",
                description="Map widget lets user draw a polygon boundary",
                verify="Playwright: draw polygon on /areas, assert polygon saved to POST /areas",
            )
        ],
    )
    d = item.model_dump()
    assert d["scene"] == 0
    assert d["id"] == "area-selection"
    assert d["text"] == "Users draw a boundary and generate a sample in under 30 seconds."
    assert len(d["features"]) == 1
    assert d["features"][0]["id"] == "boundary-draw"


def test_narration_item_features_defaults_to_empty():
    from scripts.ddd.schemas.models import NarrationItem

    item = NarrationItem(scene=1, id="sample-gen", text="Generates a proportional sample.")
    assert item.features == []


def test_narration_item_missing_id_raises():
    import pydantic
    from scripts.ddd.schemas.models import NarrationItem

    with pytest.raises(pydantic.ValidationError):
        NarrationItem(scene=0, text="some text")


def test_narration_item_missing_text_raises():
    import pydantic
    from scripts.ddd.schemas.models import NarrationItem

    with pytest.raises(pydantic.ValidationError):
        NarrationItem(scene=0, id="foo")


# ---------------------------------------------------------------------------
# ReviewRequest — actionability field (v3)
# ---------------------------------------------------------------------------

def test_review_request_actionability_defaults_to_none():
    from scripts.ddd.schemas.models import Decision, ReviewRequest

    d = Decision(
        id="D1", prompt="p", options=["A"], recommended="A", class_="concept_change"
    )
    rr = ReviewRequest(
        run_id="run-001",
        gate="concept_change",
        video={},
        decisions=[d],
        narration=[],
    )
    assert rr.actionability is None


def test_review_request_actionability_can_be_set():
    from scripts.ddd.schemas.models import Decision, ReviewRequest

    d = Decision(
        id="D1", prompt="p", options=["A"], recommended="A", class_="concept_change"
    )
    actionability = {
        "overall_score": 4.2,
        "per_scene": {
            "area-selection": {"score": 4.0, "missed": []},
            "sample-gen": {"score": 4.5, "missed": []},
        },
    }
    rr = ReviewRequest(
        run_id="run-001",
        gate="concept_change",
        video={},
        decisions=[d],
        narration=[],
        actionability=actionability,
    )
    assert rr.actionability is not None
    assert rr.actionability["overall_score"] == 4.2
    assert "area-selection" in rr.actionability["per_scene"]


def test_review_request_actionability_serializes_in_dump():
    from scripts.ddd.schemas.models import Decision, ReviewRequest

    d = Decision(
        id="D1", prompt="p", options=["A"], recommended="A", class_="concept_change"
    )
    actionability = {"overall_score": 3.8, "per_scene": {}}
    rr = ReviewRequest(
        run_id="run-001",
        gate="concept_change",
        video={},
        decisions=[d],
        narration=[],
        actionability=actionability,
    )
    dumped = rr.model_dump()
    assert "actionability" in dumped
    assert dumped["actionability"]["overall_score"] == 3.8


def test_review_request_narration_can_hold_narration_items():
    """ReviewRequest.narration accepts NarrationItem instances (v3 typed list)."""
    from scripts.ddd.schemas.models import Decision, Feature, NarrationItem, ReviewRequest

    d = Decision(
        id="narrative-verdict",
        prompt="Approve or redraft?",
        options=["approve", "redraft"],
        recommended="approve",
        class_="concept_change",
    )
    items = [
        NarrationItem(
            scene=0,
            id="area-selection",
            text="Users draw a boundary on the map.",
            features=[
                Feature(
                    id="map-draw",
                    description="Polygon drawing tool on the map",
                    verify="assert POST /areas with polygon GeoJSON returns 201",
                )
            ],
        )
    ]
    rr = ReviewRequest(
        run_id="run-001",
        gate="concept_change",
        video={},
        decisions=[d],
        narration=items,
    )
    dumped = rr.model_dump()
    assert len(dumped["narration"]) == 1
    assert dumped["narration"][0]["id"] == "area-selection"
    assert len(dumped["narration"][0]["features"]) == 1


# ---------------------------------------------------------------------------
# Unified verdict metadata + out-of-chain score cap
# ---------------------------------------------------------------------------


class TestVerdictOutOfChainCap:
    def _verdict(self, overall, **kw):
        from scripts.ddd.schemas.models import Dimension, Verdict

        return Verdict(
            dimensions={"d": Dimension(score=overall, weight=1.0)},
            overall_score=overall,
            verdict="pass",
            **kw,
        )

    def test_metadata_fields_default(self):
        # gate defaults to "advisory": an unstamped legacy
        # verdict must never become a gating, unverified verdict that blocks
        # convergence forever. The gating kinds (concept, user_artifact) are
        # stamped explicitly by their skills / KIND_DEFAULTS.
        v = self._verdict(4.0)
        assert v.kind is None
        assert v.gate == "advisory"
        assert v.live_state_verified is None
        assert v.calibration is None

    def test_unverified_verdict_score_is_capped(self):
        # ACE's out-of-chain fitness law: an eval whose grading anchor never
        # touches live state cannot claim excellence. live_state_verified=False
        # caps the emittable overall_score at LIVE_STATE_UNVERIFIED_CAP.
        v = self._verdict(4.8, live_state_verified=False)
        assert v.overall_score == 4.0
        assert v.uncapped_overall_score == 4.8

    def test_verified_verdict_score_is_not_capped(self):
        v = self._verdict(4.8, live_state_verified=True)
        assert v.overall_score == 4.8
        assert v.uncapped_overall_score is None

    def test_unknown_verification_is_not_capped(self):
        # back-compat: verdicts emitted before the field existed load unchanged
        v = self._verdict(4.8)
        assert v.overall_score == 4.8

    def test_unverified_below_cap_untouched(self):
        v = self._verdict(3.5, live_state_verified=False)
        assert v.overall_score == 3.5
        assert v.uncapped_overall_score is None
