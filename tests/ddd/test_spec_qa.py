"""Behavioral tests for scripts/ddd/spec_qa.py (SP2.2).

TDD: these tests are written first and drive the implementation.
spec_qa(spec_obj_or_path) -> Verdict
  - "pass"  for a well-formed spec with no vacuous concept_claims
  - "fail"  (with blocking_reason) when:
      * a scene has an empty/whitespace concept_claim
      * a scene has a vacuous concept_claim (banned marketing phrases or < 5 words)
      * a scene's provenance doesn't match any spine id (via delegation)
      * a scene references an undefined persona (via delegation)
      * a required field is missing (via delegation)
  - Always returns Verdict (never raises) on missing/malformed input
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _why_brief_data(spine_ids: list[str] | None = None) -> dict:
    """Build a minimal valid WhyBrief dict."""
    if spine_ids is None:
        spine_ids = ["S1"]
    spine = [
        {
            "id": sid,
            "claim": f"Claim {sid}",
            "rationale": f"Because of evidence for {sid}",
            "status": "grounded",
            "evidence": [{"kind": "documented", "ref": f"doc://{sid}"}],
        }
        for sid in spine_ids
    ]
    return {
        "schema_version": 1,
        "narrative_slug": "Test Feature",
        "problem": "A real problem exists for users",
        "spine": spine,
        "gaps": [],
    }


def _spec_data(
    concept_claim: str = "Users can submit a form and see confirmation within 2 seconds",
    provenance: str = "S1",
    persona_key: str = "alice",
    why_brief_rel: str | None = None,
    features: list[dict] | None = None,
) -> dict:
    """Build a minimal valid UnifiedSpec dict."""
    if features is None:
        features = [
            {
                "id": "F1",
                "description": "Submit button on the form page triggers a POST request",
                "verify": "pytest: POST /form returns 200 and response contains confirmation_id",
            }
        ]
    return {
        "name": "My Feature Walkthrough",
        "narrative": "Demonstrates the core user journey",
        "base_url": "http://localhost:8000",
        "why_brief": why_brief_rel,
        "personas": {
            persona_key: {
                "name": "Alice",
                "role": "Program Manager",
                "color": "#3B82F6",
                "intro": "Alice manages program delivery.",
            }
        },
        "scenes": [
            {
                "persona": persona_key,
                "title": "Submit Form",
                "show": "navigate to /form, fill fields, click Submit",
                "concept_claim": concept_claim,
                "provenance": provenance,
                "design_intent": "Test that confirmation feedback is immediate",
                "features": features,
            }
        ],
    }


def _scene(
    *,
    title: str = "Submit Form",
    persona_key: str = "alice",
    concept_claim: str = "Users can submit a form and see confirmation within 2 seconds",
    narrative: str = "",
    provenance: str = "S1",
    actions: list[dict] | None = None,
    features: list[dict] | None = None,
    role: str | None = None,
) -> dict:
    """Build one scene dict, optionally with an actions list (for rule (j))."""
    if features is None:
        features = [
            {
                "id": "F1",
                "description": "Submit button on the form page triggers a POST request",
                "verify": "pytest: POST /form returns 200 and response contains confirmation_id",
            }
        ]
    scene: dict = {
        "persona": persona_key,
        "title": title,
        "show": "navigate to /form, fill fields, click Submit",
        "concept_claim": concept_claim,
        "provenance": provenance,
        "design_intent": "Test that confirmation feedback is immediate",
        "features": features,
    }
    if role is not None:
        scene["role"] = role
    if narrative:
        scene["narrative"] = narrative
    if actions is not None:
        scene["actions"] = actions
    return scene


def _spec_with_scene(scene: dict, persona_key: str = "alice") -> dict:
    """Wrap a scene dict in a minimal valid spec (no why_brief link)."""
    return {
        "name": "My Feature Walkthrough",
        "narrative": "Demonstrates the core user journey",
        "base_url": "http://localhost:8000",
        "why_brief": None,
        "personas": {
            persona_key: {
                "name": "Alice",
                "role": "Program Manager",
                "color": "#3B82F6",
                "intro": "Alice manages program delivery.",
            }
        },
        "scenes": [scene],
    }


def _write_yaml(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    p.write_text(yaml.dump(data))
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_spec_no_why_brief_passes():
    """A valid spec with no why_brief link passes."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data()  # no why_brief linked
    result = spec_qa(spec)
    assert result.verdict == "pass"
    assert result.blocking_reason is None


def test_overview_scene_without_features_passes():
    """A `role: overview` scene (the goal-setting 'why' opening) is exempt from
    the feature requirement — it demonstrates no capability."""
    from scripts.ddd.spec_qa import spec_qa

    overview = _scene(
        title="The goal — measure the program's real effect",
        role="overview",
        features=[],
        concept_claim="Maya's goal is to measure her program's real effect against a matched comparison group",
        actions=[{"kind": "hold", "seconds": 4}],
    )
    spec = _spec_with_scene(overview)
    result = spec_qa(spec)
    assert result.verdict == "pass", result.blocking_reason


def test_demo_scene_without_features_still_fails():
    """A default (demo) scene with no features still fails — the exemption is
    scoped to overview only, so the common case is unchanged."""
    from scripts.ddd.spec_qa import spec_qa

    demo = _scene(features=[])  # role defaults to "demo"
    result = spec_qa(_spec_with_scene(demo))
    assert result.verdict == "fail"
    assert "no features" in (result.blocking_reason or "")


def test_overview_scene_still_needs_falsifiable_concept_claim():
    """Overview is exempt from features, NOT from a real concept_claim — a
    vacuous goal claim still fails."""
    from scripts.ddd.spec_qa import spec_qa

    bad = _scene(role="overview", features=[], concept_claim="a world-class experience")
    result = spec_qa(_spec_with_scene(bad))
    assert result.verdict == "fail"


def test_valid_spec_with_why_brief_passes(tmp_path):
    """A valid spec with a resolvable why_brief and matching provenance passes."""
    from scripts.ddd.spec_qa import spec_qa

    _write_yaml(tmp_path, "why_brief.yaml", _why_brief_data(["S1"]))
    spec = _spec_data(provenance="S1", why_brief_rel="why_brief.yaml")
    spec_path = _write_yaml(tmp_path, "spec.yaml", spec)

    result = spec_qa(spec_path)
    assert result.verdict == "pass"
    assert result.blocking_reason is None


def test_valid_spec_from_path_passes(tmp_path):
    """spec_qa accepts a file path."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data()
    spec_path = _write_yaml(tmp_path, "spec.yaml", spec)
    result = spec_qa(spec_path)
    assert result.verdict == "pass"


# ---------------------------------------------------------------------------
# Failure: vacuous concept_claim (banned marketing phrases)
# ---------------------------------------------------------------------------

def test_vacuous_claim_world_class_fails():
    """A concept_claim containing 'world-class' is not falsifiable → fail."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(concept_claim="a world-class seamless experience")
    result = spec_qa(spec)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None
    assert "concept_claim" in result.blocking_reason.lower() or "falsifiable" in result.blocking_reason.lower()


def test_vacuous_claim_seamless_fails():
    """A concept_claim containing 'seamless' is not falsifiable → fail."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(concept_claim="Provides a seamless workflow for users")
    result = spec_qa(spec)
    assert result.verdict == "fail"


def test_vacuous_claim_powerful_fails():
    """A concept_claim containing 'powerful' is not falsifiable → fail."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(concept_claim="A powerful tool that empowers users")
    result = spec_qa(spec)
    assert result.verdict == "fail"


def test_vacuous_claim_robust_fails():
    """A concept_claim containing 'robust' is not falsifiable → fail."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(concept_claim="Robust error handling")
    result = spec_qa(spec)
    assert result.verdict == "fail"


def test_vacuous_claim_best_in_class_fails():
    """A concept_claim containing 'best-in-class' is not falsifiable → fail."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(concept_claim="best-in-class performance")
    result = spec_qa(spec)
    assert result.verdict == "fail"


def test_vacuous_claim_too_short_fails():
    """A concept_claim under 5 words is not falsifiable → fail."""
    from scripts.ddd.spec_qa import spec_qa

    # Only 2 words — too short to be a specific claim
    spec = _spec_data(concept_claim="fast loading")
    result = spec_qa(spec)
    assert result.verdict == "fail"


def test_empty_concept_claim_fails():
    """Empty concept_claim → fail."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(concept_claim="")
    result = spec_qa(spec)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None


def test_whitespace_concept_claim_fails():
    """Whitespace-only concept_claim → fail."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(concept_claim="   ")
    result = spec_qa(spec)
    assert result.verdict == "fail"


def test_falsifiable_claim_with_measurable_outcome_passes():
    """A claim with a specific measurable outcome passes."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(
        concept_claim="When a supervisor submits the audit form, the FLW receives a coaching task within 60 seconds"
    )
    result = spec_qa(spec)
    assert result.verdict == "pass"


def test_falsifiable_claim_with_verb_passes():
    """A claim describing a user action with a concrete result passes."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(
        concept_claim="Users can filter the task list by status and see only open tasks"
    )
    result = spec_qa(spec)
    assert result.verdict == "pass"


# ---------------------------------------------------------------------------
# False-positive guard: nominalized domain claims that verb-detection blocked
# ---------------------------------------------------------------------------

def test_nominalized_gps_claim_passes():
    """'GPS pinning accuracy within 5 meters' — a valid nominalized claim — passes.

    This is the canonical false-positive example: the old verb-pattern heuristic
    wrongly rejected it because 'pinning' wasn't in the verb list as a gerund
    and 'accuracy within 5 meters' has no finite verb.  The new heuristic (banned
    phrases + min length) correctly passes it.
    """
    from scripts.ddd.spec_qa import spec_qa
    from scripts.ddd.spec_qa import _is_falsifiable

    claim = "GPS pinning accuracy within 5 meters"
    assert _is_falsifiable(claim), f"Expected _is_falsifiable to return True for: {claim!r}"

    spec = _spec_data(concept_claim=claim)
    result = spec_qa(spec)
    assert result.verdict == "pass", f"Expected pass but got: {result.blocking_reason}"


def test_proportional_allocation_claim_passes():
    """'Per-stratum allocation proportional to population' passes (nominalized, no finite verb)."""
    from scripts.ddd.spec_qa import _is_falsifiable

    claim = "Per-stratum allocation proportional to population density"
    assert _is_falsifiable(claim), f"Expected True for: {claim!r}"


def test_autosamples_rooftops_claim_passes():
    """'Auto-samples thirty rooftops per population cluster' passes (≥5 words, no banned phrase)."""
    from scripts.ddd.spec_qa import spec_qa
    from scripts.ddd.spec_qa import _is_falsifiable

    claim = "Auto-samples thirty rooftops per population cluster"
    assert _is_falsifiable(claim), f"Expected True for: {claim!r}"

    spec = _spec_data(concept_claim=claim)
    result = spec_qa(spec)
    assert result.verdict == "pass", f"Expected pass but got: {result.blocking_reason}"


# ---------------------------------------------------------------------------
# False-negative-now-caught: short copula fluff caught by min-length rule
# ---------------------------------------------------------------------------

def test_system_is_good_fails_min_length():
    """'The system is good' (4 words) fails via the min-length rule.

    The old verb-pattern heuristic wrongly accepted this because 'is' matched
    \\bis\\b.  The new heuristic rejects it: 4 words < 5-word minimum.
    """
    from scripts.ddd.spec_qa import spec_qa
    from scripts.ddd.spec_qa import _is_falsifiable

    claim = "The system is good"
    assert not _is_falsifiable(claim), f"Expected _is_falsifiable to return False for: {claim!r}"

    spec = _spec_data(concept_claim=claim)
    result = spec_qa(spec)
    assert result.verdict == "fail", f"Expected fail but got pass for: {claim!r}"


# ---------------------------------------------------------------------------
# Failure: provenance mismatch (via delegation to validate)
# ---------------------------------------------------------------------------

def test_provenance_mismatch_fails(tmp_path):
    """A scene whose provenance doesn't match any spine id → fail (delegation)."""
    from scripts.ddd.spec_qa import spec_qa

    wb_path = _write_yaml(tmp_path, "why_brief.yaml", _why_brief_data(["S1"]))
    spec = _spec_data(
        provenance="S99",  # does not exist in why_brief
        why_brief_rel="why_brief.yaml",
    )
    spec_path = _write_yaml(tmp_path, "spec.yaml", spec)

    result = spec_qa(spec_path)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None
    assert "S99" in result.blocking_reason or "provenance" in result.blocking_reason.lower()


# ---------------------------------------------------------------------------
# Failure: undefined persona (via delegation to validate)
# ---------------------------------------------------------------------------

def test_undefined_persona_fails():
    """A scene referencing an undefined persona → fail (delegation)."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(persona_key="alice")
    # manually break: scene references 'bob' but only 'alice' is defined
    spec["scenes"][0]["persona"] = "bob"
    result = spec_qa(spec)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None
    assert "persona" in result.blocking_reason.lower() or "bob" in result.blocking_reason


# ---------------------------------------------------------------------------
# Failure: status-tag parenthetical in a scene title (story-beat enforcement)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_title",
    [
        "Pick where to work (frontier)",
        "Review and clean the plan (the hero)",
        "Monitor impact (gap)",
        "Push to Connect (built)",
        "Wire the dashboard (WIP)",
    ],
)
def test_status_tag_in_title_fails(bad_title):
    """A scene title carrying a build-status parenthetical → fail.

    Scene titles are story beats (what the viewer watches), not design-doc
    status annotations. Build status belongs in the why_brief spine."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data()
    spec["scenes"][0]["title"] = bad_title
    result = spec_qa(spec)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None
    assert "status tag" in result.blocking_reason.lower() or "story beat" in result.blocking_reason.lower()


def test_clean_story_beat_title_passes():
    """A normal story-beat title with incidental parentheses does not trip the check."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data()
    spec["scenes"][0]["title"] = "Maya picks the district (and draws a boundary)"
    result = spec_qa(spec)
    assert result.verdict == "pass"


# ---------------------------------------------------------------------------
# Failure: missing required field (via delegation to validate)
# ---------------------------------------------------------------------------

def test_missing_base_url_fails():
    """Missing required field (base_url) → fail (delegation)."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data()
    del spec["base_url"]
    result = spec_qa(spec)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None


# ---------------------------------------------------------------------------
# Missing / malformed input: returns Verdict (never raises)
# ---------------------------------------------------------------------------

def test_missing_file_returns_fail_verdict():
    """spec_qa(Path('/nonexistent.yaml')) returns fail Verdict, does not raise."""
    from scripts.ddd.spec_qa import spec_qa
    from scripts.ddd.schemas.models import Verdict

    result = spec_qa(Path("/nonexistent/spec.yaml"))
    assert isinstance(result, Verdict)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None


def test_malformed_yaml_returns_fail_verdict(tmp_path):
    """A malformed YAML file returns fail Verdict, does not raise."""
    from scripts.ddd.spec_qa import spec_qa
    from scripts.ddd.schemas.models import Verdict

    bad = tmp_path / "bad.yaml"
    bad.write_text("this: is: not: valid: yaml: :\n  - broken [indent")
    result = spec_qa(bad)
    assert isinstance(result, Verdict)
    assert result.verdict == "fail"


def test_none_dict_returns_fail_verdict():
    """spec_qa(None) returns fail Verdict, does not raise."""
    from scripts.ddd.spec_qa import spec_qa
    from scripts.ddd.schemas.models import Verdict

    result = spec_qa(None)  # type: ignore[arg-type]
    assert isinstance(result, Verdict)
    assert result.verdict == "fail"


def test_always_returns_verdict_model():
    """Return type is always Verdict."""
    from scripts.ddd.spec_qa import spec_qa
    from scripts.ddd.schemas.models import Verdict

    result = spec_qa(_spec_data())
    assert isinstance(result, Verdict)
    assert result.schema_version == 1


# ---------------------------------------------------------------------------
# Confirms delegation to validate() — not re-implemented here
# ---------------------------------------------------------------------------

def test_delegates_to_validate_for_provenance_check(tmp_path):
    """Provenance cross-check is done by validate(), not spec_qa's own code."""
    from scripts.ddd.spec_qa import spec_qa

    # Create a why_brief with S1 only
    wb_path = _write_yaml(tmp_path, "why_brief.yaml", _why_brief_data(["S1"]))
    # Create spec that references S2 (which doesn't exist)
    spec = _spec_data(provenance="S2", why_brief_rel="why_brief.yaml")
    spec_path = _write_yaml(tmp_path, "spec.yaml", spec)

    result = spec_qa(spec_path)
    assert result.verdict == "fail"
    # The error mentions S2, proving validate() caught it
    assert result.blocking_reason is not None
    assert "S2" in result.blocking_reason or "provenance" in result.blocking_reason.lower()


# ---------------------------------------------------------------------------
# CLI: python -m scripts.ddd.spec_qa exits 0 on pass, 1 on fail, 2 on usage
# ---------------------------------------------------------------------------

def test_cli_exit_0_on_valid(tmp_path):
    import subprocess
    import sys

    spec = _spec_data()
    spec_path = _write_yaml(tmp_path, "spec.yaml", spec)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ddd.spec_qa", str(spec_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cli_exit_1_on_invalid(tmp_path):
    import subprocess
    import sys

    spec = _spec_data(concept_claim="seamless world-class experience")
    spec_path = _write_yaml(tmp_path, "spec.yaml", spec)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ddd.spec_qa", str(spec_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1


def test_cli_no_args_exits_2():
    """Zero args → exit 2 (usage error)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "scripts.ddd.spec_qa"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


def test_cli_bad_path_exits_1():
    """Non-existent path → exit 1 (not a usage error)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "scripts.ddd.spec_qa", "/nonexistent/spec.yaml"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1


def test_cli_with_why_brief_in_spec_exit_0(tmp_path):
    """CLI passes when why_brief is resolvable from the spec's own why_brief field."""
    import subprocess
    import sys

    _write_yaml(tmp_path, "why_brief.yaml", _why_brief_data(["S1"]))
    spec = _spec_data(provenance="S1", why_brief_rel="why_brief.yaml")
    spec_path = _write_yaml(tmp_path, "spec.yaml", spec)

    result = subprocess.run(
        [sys.executable, "-m", "scripts.ddd.spec_qa", str(spec_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cli_extra_arg_exits_2(tmp_path):
    """Passing two args (old why_brief_path form) now triggers usage error (exit 2)."""
    import subprocess
    import sys

    spec = _spec_data()
    spec_path = _write_yaml(tmp_path, "spec.yaml", spec)

    result = subprocess.run(
        [sys.executable, "-m", "scripts.ddd.spec_qa", str(spec_path), "extra_arg"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# DDD v3: actionable features — spec_qa enforces ≥1 feature per scene
# ---------------------------------------------------------------------------

def test_scene_with_zero_features_fails():
    """spec_qa fails when a scene has no features (v3 requires ≥1)."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(features=[])
    result = spec_qa(spec)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None
    assert "feature" in result.blocking_reason.lower()


def test_scene_with_one_valid_feature_passes():
    """spec_qa passes when a scene has exactly one valid feature."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(features=[
        {
            "id": "F1",
            "description": "POST /form endpoint accepts form data and returns 200",
            "verify": "pytest: POST /form with valid payload returns 200 and confirmation_id in body",
        }
    ])
    result = spec_qa(spec)
    assert result.verdict == "pass", f"Expected pass, got: {result.blocking_reason}"


def test_scene_feature_verify_too_short_fails():
    """A feature whose verify is non-empty but fewer than 3 words fails spec_qa."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(features=[
        {
            "id": "F1",
            "description": "Button on the form triggers a POST",
            "verify": "it works",  # only 2 words — not a real validation step
        }
    ])
    result = spec_qa(spec)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None
    assert "verify" in result.blocking_reason.lower() or "F1" in result.blocking_reason


def test_scene_feature_verify_three_words_passes():
    """A verify string of exactly 3 words passes spec_qa."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(features=[
        {
            "id": "F1",
            "description": "Form submit button calls the backend endpoint",
            "verify": "assert API responds",  # exactly 3 words — passes
        }
    ])
    result = spec_qa(spec)
    assert result.verdict == "pass", f"Expected pass, got: {result.blocking_reason}"


def test_blocking_reason_lists_scene_and_feature():
    """The blocking_reason names the scene title and feature id for context."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(features=[])
    result = spec_qa(spec)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None
    # Should mention the scene title "Submit Form"
    assert "Submit Form" in result.blocking_reason


def test_multiple_scenes_one_missing_features_fails():
    """All scenes must have ≥1 feature; if any lack features, spec_qa fails."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(features=[
        {
            "id": "F1",
            "description": "Submit button triggers POST to /form endpoint",
            "verify": "pytest: POST /form returns 200 and confirmation_id in response body",
        }
    ])
    # Add a second scene with no features
    spec["personas"]["bob"] = {
        "name": "Bob",
        "role": "Field Worker",
        "color": "#10B981",
        "intro": "Bob delivers services in the field.",
    }
    spec["scenes"].append({
        "persona": "bob",
        "title": "View Task",
        "show": "navigate to /tasks",
        "concept_claim": "Field workers can see their assigned tasks ordered by due date",
        "provenance": "S1",
        "features": [],  # no features — should fail
    })
    result = spec_qa(spec)
    assert result.verdict == "fail"
    assert result.blocking_reason is not None
    assert "View Task" in result.blocking_reason or "feature" in result.blocking_reason.lower()


def test_fix_recommendation_mentions_verify():
    """fix_recommendation must mention 'verify' to guide the author."""
    from scripts.ddd.spec_qa import spec_qa

    spec = _spec_data(features=[])
    result = spec_qa(spec)
    assert result.fix_recommendation is not None
    assert "verify" in result.fix_recommendation.lower() or "feature" in result.fix_recommendation.lower()


# ---------------------------------------------------------------------------
# "Show, don't tell" gate (rule j): narrated effecting act + hover-only actions
# ---------------------------------------------------------------------------

def test_hover_only_scene_narrating_create_fails():
    """A scene that narrates 'create' but only hovers/scrolls → fail.

    This is the core diagnosis: the 'create the solicitation' scene whose
    actions are hover-only scored 3/5 because the judge saw the same end-frame.
    The spec gate now blocks it before any render."""
    from scripts.ddd.spec_qa import spec_qa

    scene = _scene(
        title="Dana creates the solicitation",
        concept_claim="Dana creates a new solicitation for the program in under a minute",
        actions=[
            {"kind": "hover", "target": "New solicitation"},
            {"kind": "scroll_to", "target": "Funding terms"},
            {"kind": "wait_for", "target": "Funding terms"},
        ],
    )
    result = spec_qa(_spec_with_scene(scene))
    assert result.verdict == "fail", result.blocking_reason
    assert result.blocking_reason is not None
    assert "create" in result.blocking_reason.lower()
    assert "effecting action" in result.blocking_reason.lower()


def test_hover_only_scene_narrating_award_fails():
    """A scene narrating 'award' but only hovering → fail."""
    from scripts.ddd.spec_qa import spec_qa

    scene = _scene(
        title="The reviewer awards the response",
        concept_claim="The reviewer awards the winning response to the chosen LLO",
        narrative="Maya reviews each response and awards the strongest one.",
        actions=[{"kind": "hover", "target": "Award"}],
    )
    result = spec_qa(_spec_with_scene(scene))
    assert result.verdict == "fail"
    assert "award" in result.blocking_reason.lower()


def test_scene_narrating_create_with_click_passes():
    """A scene that narrates 'create' AND actually clicks/fills → pass."""
    from scripts.ddd.spec_qa import spec_qa

    scene = _scene(
        title="Dana creates the solicitation",
        concept_claim="Dana creates a new solicitation for the program in under a minute",
        actions=[
            {"kind": "click", "target": "New solicitation"},
            {"kind": "fill", "target": "Title", "value": "Q3 nutrition program"},
            {"kind": "click", "target": "Create"},
        ],
    )
    result = spec_qa(_spec_with_scene(scene))
    assert result.verdict == "pass", result.blocking_reason


def test_scene_with_no_actions_is_exempt():
    """A narrative-only scene (no actions) narrating 'submit' is NOT blocked.

    Empty actions = legacy scroll-pan beat; the narration is the whole point.
    This is also why the existing happy-path tests (default claim contains
    'submit', no actions) still pass."""
    from scripts.ddd.spec_qa import spec_qa

    scene = _scene(
        concept_claim="Users can submit a form and see confirmation within 2 seconds",
        actions=None,  # no actions key at all
    )
    result = spec_qa(_spec_with_scene(scene))
    assert result.verdict == "pass", result.blocking_reason


def test_hover_only_scene_with_no_effecting_verb_passes():
    """Hover-only actions are fine when the narration promises no effecting act.

    A scene whose job is genuinely to SHOW something (hover to reveal a tooltip)
    and whose narration doesn't claim create/fill/submit must not be blocked."""
    from scripts.ddd.spec_qa import spec_qa

    scene = _scene(
        title="Maya inspects the coverage map",
        concept_claim="The coverage map shows every ward shaded by completion rate",
        actions=[
            {"kind": "hover", "target": "Kano ward"},
            {"kind": "scroll_to", "target": "Legend"},
        ],
    )
    result = spec_qa(_spec_with_scene(scene))
    assert result.verdict == "pass", result.blocking_reason


def test_mixed_actions_with_one_effecting_passes():
    """A scene with hovers AND at least one effecting action passes."""
    from scripts.ddd.spec_qa import spec_qa

    scene = _scene(
        concept_claim="Dana submits the completed solicitation form",
        actions=[
            {"kind": "hover", "target": "Funding terms"},
            {"kind": "scroll_to", "target": "Submit"},
            {"kind": "click", "target": "Submit"},  # the effecting act
        ],
    )
    result = spec_qa(_spec_with_scene(scene))
    assert result.verdict == "pass", result.blocking_reason


def test_narrative_field_triggers_show_dont_tell():
    """The check reads the scene's `narrative` field, not just concept_claim."""
    from scripts.ddd.spec_qa import spec_qa

    scene = _scene(
        title="Dana works on the form",
        # concept_claim has no effecting verb...
        concept_claim="The solicitation builder is the heart of the program flow",
        # ...but the narrative promises the user fills it out
        narrative="Dana fills out every funding term and submits the solicitation.",
        actions=[{"kind": "hover", "target": "Title field"}],
    )
    result = spec_qa(_spec_with_scene(scene))
    assert result.verdict == "fail"
    assert "fill" in result.blocking_reason.lower() or "submit" in result.blocking_reason.lower()


def test_show_dont_tell_does_not_false_positive_on_prototype():
    """'type' must not match inside 'prototype' (whole-word boundary)."""
    from scripts.ddd.spec_qa import _narrated_effecting_verb

    assert _narrated_effecting_verb("The prototype renders the archetype panel") is None
    assert _narrated_effecting_verb("Maya types her query into the box") == "types"


def test_blocking_reason_names_scene_and_non_effecting_kinds():
    """The blocking_reason names the scene title and the non-effecting kinds present."""
    from scripts.ddd.spec_qa import spec_qa

    scene = _scene(
        title="Dana creates the solicitation",
        concept_claim="Dana creates the solicitation in the builder",
        actions=[{"kind": "hover", "target": "X"}, {"kind": "scroll_to", "target": "Y"}],
    )
    result = spec_qa(_spec_with_scene(scene))
    assert result.verdict == "fail"
    assert "Dana creates the solicitation" in result.blocking_reason
    assert "hover" in result.blocking_reason
    assert "scroll_to" in result.blocking_reason


# ---------------------------------------------------------------------------
# Full-demo (≥4 scenes) must have an overview scene + per-scene narrative VO.
# Regression: nutrition-demo shipped with neither, so the render opened cold on
# scene 1 and read `show` aloud as the voiceover. Scoped to ≥4 scenes so tiny
# utility walkthroughs + the single-scene fixtures above stay exempt.
# ---------------------------------------------------------------------------
def _demo_spec_4_scenes(*, with_overview: bool = True, all_narratives: bool = True) -> dict:
    def _sc(title: str, *, overview: bool = False) -> dict:
        s: dict = {
            "persona": "alice",
            "title": title,
            "show": "navigate to /form, fill fields, click Submit",
            "concept_claim": "Users can submit a form and see confirmation within 2 seconds",
            "provenance": "S1",
            "design_intent": "immediate feedback",
            "narrative": (f"Alice {title.lower()}." if all_narratives else ""),
        }
        if overview:
            s["role"] = "overview"
        else:
            s["features"] = [
                {
                    "id": "F1",
                    "description": "Submit button triggers a POST request",
                    "verify": "pytest: POST /form returns 200 and response contains confirmation_id",
                }
            ]
        return s

    first = _sc("The goal", overview=True) if with_overview else _sc("Opens the form")
    scenes = [first, _sc("Fills the form"), _sc("Submits"), _sc("Sees confirmation")]
    return {
        "name": "Demo",
        "narrative": "the arc",
        "base_url": "http://localhost:8000",
        "why_brief": None,
        "personas": {"alice": {"name": "Alice", "role": "PM", "color": "#3B82F6", "intro": "Alice."}},
        "scenes": scenes,
    }


def test_full_demo_with_overview_and_narratives_passes():
    from scripts.ddd.spec_qa import spec_qa
    result = spec_qa(_demo_spec_4_scenes())
    assert result.verdict == "pass", result.blocking_reason


def test_full_demo_without_overview_scene_fails():
    from scripts.ddd.spec_qa import spec_qa
    result = spec_qa(_demo_spec_4_scenes(with_overview=False))
    assert result.verdict == "fail"
    assert "overview" in result.blocking_reason


def test_full_demo_scene_without_narrative_fails():
    from scripts.ddd.spec_qa import spec_qa
    result = spec_qa(_demo_spec_4_scenes(all_narratives=False))
    assert result.verdict == "fail"
    assert "narrative" in result.blocking_reason


def test_small_walkthrough_under_4_scenes_exempt():
    from scripts.ddd.spec_qa import spec_qa
    # the single-scene minimal fixture has no overview/narrative and must still pass
    result = spec_qa(_spec_data())
    assert result.verdict == "pass", result.blocking_reason
