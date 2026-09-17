"""The verdict/finding contract, enforced at EMIT rather than only at load (canopy#547).

Two judges were measured emitting fields outside the contract on
``hh-poverty-targeting/20260827-0323``:

  1. the user-artifact judge wrote ``overall_verdict`` where ``load_verdict``
     requires ``verdict`` — a hard ValidationError, discovered at ASSEMBLY, i.e.
     after the expensive multimodal judge dispatch was already paid for;
  2. the arc judge wrote ``fix_kind: targeted``, outside the routing vocabulary.

(2) is the dangerous one and it is silent. ``compute_auto_iterate`` selects
``mechanical`` and ``options``/``redesign`` by exact string, so a third value
matches NEITHER branch: the finding leaves the decision entirely and the loop
reports "No options/redesign ... re-fire" — actively asserting there is nothing
needing a human — while re-firing on a defect it will never apply and never
escalate.

So the fix has two halves and this file tests both:
  * a GATE at emit (``validate("findings", ...)``) that names the judge's
    offending field while its work is still in hand;
  * a FAIL-SAFE at the routing site, so an unrecognised kind surfaces to a human
    instead of vanishing.
"""
from __future__ import annotations

import json

import pytest

from scripts.ddd.run_pipeline import compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict
from scripts.narrative.models import (
    FIX_KINDS,
    ROUTES,
    UNROUTABLE_FIX_KIND_FALLBACK,
)


def _v(score: float, verdict_str: str = "pass") -> Verdict:
    return Verdict(
        schema_version=1,
        kind="concept",
        gate="gating",
        dimensions={},
        overall_score=score,
        verdict=verdict_str,
    )


def _finding(**overrides) -> dict:
    base = {
        "scene": "3",
        "dimension": "visual_polish",
        "route": "PRODUCT",
        "fix_kind": "options",
        "severity": "medium",
        "detail": "The legend overlaps the chart area on the revenue panel.",
        "fix_recommendation": "Move the legend below the chart.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. The routing fail-safe — an unrecognised fix_kind must not vanish
# ---------------------------------------------------------------------------


class TestUnroutableFixKindDoesNotVanish:
    def test_targeted_escalates_instead_of_silently_refiring(self) -> None:
        """The measured canopy#547 case: `targeted` from the arc judge.

        On origin/main this returned ``continue`` with the reason "No
        options/redesign and score still moving — re-fire."
        """
        state = RunState(run_id="r", narrative_slug="n")
        action, reason = compute_auto_iterate(
            state,
            _v(2.0, "fail"),
            _v(2.0, "fail"),
            [_finding(fix_kind="targeted")],
            unattended=True,
        )
        assert action == "stop_unclear", reason
        assert "No options/redesign" not in reason

    @pytest.mark.parametrize("bogus", ["targeted", "", "MECHANICAL", "mechanical ", None, 7])
    def test_every_unrecognised_kind_routes_to_a_human(self, bogus) -> None:
        state = RunState(run_id="r", narrative_slug="n")
        action, _ = compute_auto_iterate(
            state,
            _v(2.0, "fail"),
            _v(2.0, "fail"),
            [_finding(fix_kind=bogus)],
            unattended=True,
        )
        assert action == "stop_unclear"

    def test_the_fallback_is_the_human_branch_not_the_autonomous_one(self) -> None:
        """Routing an unclassified finding to `mechanical` would grant the loop
        autonomy over something nobody classified. Pin the safe direction."""
        assert UNROUTABLE_FIX_KIND_FALLBACK in FIX_KINDS
        assert UNROUTABLE_FIX_KIND_FALLBACK != "mechanical"

    @pytest.mark.parametrize("kind", FIX_KINDS)
    def test_recognised_kinds_are_untouched(self, kind) -> None:
        """The fail-safe must not perturb the behaviour it wraps."""
        state = RunState(run_id="r", narrative_slug="n")
        action, _ = compute_auto_iterate(
            state,
            _v(2.0, "fail"),
            _v(2.0, "fail"),
            [_finding(fix_kind=kind)],
            unattended=True,
        )
        assert action == ("continue" if kind == "mechanical" else "stop_unclear")

    def test_the_judges_original_word_is_preserved_on_the_finding(self) -> None:
        """A routing repair, not a rewrite — the artifact stays honest about
        what the judge actually emitted."""
        state = RunState(run_id="r", narrative_slug="n")
        compute_auto_iterate(
            state,
            _v(2.0, "fail"),
            _v(2.0, "fail"),
            [_finding(fix_kind="targeted")],
            unattended=True,
        )
        assert state.findings[0]["fix_kind"] == "targeted"

    def test_an_unroutable_kind_on_a_user_verdict_dimension_also_escalates(self) -> None:
        """compute_auto_iterate reads fix_kind from verdict DIMENSIONS too —
        the second, easily-missed entry point into the same branch."""
        user = _v(2.0, "fail")
        user.dimensions = {"layout": {"score": 2.0, "weight": 1.0, "fix_kind": "targeted"}}
        state = RunState(run_id="r", narrative_slug="n")
        action, _ = compute_auto_iterate(
            state, _v(2.0, "fail"), user, [], unattended=True
        )
        assert action == "stop_unclear"


# ---------------------------------------------------------------------------
# 2. The emit-time gate — validate("findings", ...)
# ---------------------------------------------------------------------------


class TestFindingsGate:
    def test_a_clean_findings_artifact_passes(self, tmp_path) -> None:
        from scripts.ddd.validate import validate

        p = tmp_path / "design_findings.json"
        p.write_text(json.dumps([_finding(), _finding(fix_kind="mechanical")]))
        ok, problems = validate("findings", p)
        assert ok is True, problems
        assert problems == []

    def test_an_out_of_enum_fix_kind_is_rejected_and_named(self, tmp_path) -> None:
        from scripts.ddd.validate import validate

        p = tmp_path / "arc_findings.json"
        p.write_text(json.dumps([_finding(fix_kind="targeted")]))
        ok, problems = validate("findings", p)
        assert ok is False
        assert any("fix_kind" in x and "targeted" in x for x in problems), problems
        # the message must say WHICH finding, or it is not actionable on a
        # 30-finding artifact
        assert any("findings[0]" in x for x in problems), problems

    def test_an_out_of_enum_route_is_rejected(self, tmp_path) -> None:
        from scripts.ddd.validate import validate

        p = tmp_path / "design_findings.json"
        p.write_text(json.dumps([_finding(route="PRODUCTS")]))
        ok, problems = validate("findings", p)
        assert ok is False
        assert any("route" in x for x in problems), problems

    def test_a_non_list_artifact_is_rejected(self, tmp_path) -> None:
        from scripts.ddd.validate import validate

        p = tmp_path / "design_findings.json"
        p.write_text(json.dumps({"findings": [_finding()]}))
        ok, problems = validate("findings", p)
        assert ok is False
        assert any("list" in x for x in problems), problems

    def test_the_offending_index_is_reported_among_valid_siblings(self, tmp_path) -> None:
        from scripts.ddd.validate import validate

        p = tmp_path / "design_findings.json"
        p.write_text(
            json.dumps([_finding(), _finding(fix_kind="targeted"), _finding()])
        )
        ok, problems = validate("findings", p)
        assert ok is False
        assert any("findings[1]" in x for x in problems), problems
        assert not any("findings[0]" in x or "findings[2]" in x for x in problems)

    @pytest.mark.parametrize("route", ROUTES)
    def test_every_declared_route_is_accepted(self, route, tmp_path) -> None:
        from scripts.ddd.validate import validate

        p = tmp_path / "design_findings.json"
        p.write_text(json.dumps([_finding(route=route)]))
        ok, problems = validate("findings", p)
        assert ok is True, problems


# ---------------------------------------------------------------------------
# 3. The alias near-miss — overall_verdict instead of verdict
# ---------------------------------------------------------------------------


class TestVerdictAliasHint:
    def test_overall_verdict_is_named_in_the_error(self, tmp_path) -> None:
        """Pydantic's bare 'verdict: Field required' is truthful and unhelpful:
        the judge believes it supplied a verdict. Name what it actually wrote."""
        import yaml

        from scripts.ddd.validate import validate

        p = tmp_path / "verdict-user.yaml"
        p.write_text(
            yaml.dump(
                {
                    "schema_version": 1,
                    "kind": "user_artifact",
                    "dimensions": {},
                    "overall_score": 3.0,
                    "overall_verdict": "warn",  # the measured mistake
                }
            )
        )
        ok, problems = validate("verdict", p)
        assert ok is False
        assert any("overall_verdict" in x for x in problems), problems

    def test_a_valid_verdict_still_passes_unannotated(self, tmp_path) -> None:
        import yaml

        from scripts.ddd.validate import validate

        p = tmp_path / "verdict-user.yaml"
        p.write_text(
            yaml.dump(
                {
                    "schema_version": 1,
                    "kind": "user_artifact",
                    "dimensions": {},
                    "overall_score": 3.0,
                    "verdict": "warn",
                }
            )
        )
        ok, problems = validate("verdict", p)
        assert ok is True, problems

    def test_an_unrelated_missing_field_is_not_falsely_annotated(self, tmp_path) -> None:
        """The hint must fire on a real near-miss only — a plain missing field
        with no alias present stays exactly as Pydantic reported it."""
        import yaml

        from scripts.ddd.validate import validate

        p = tmp_path / "verdict-user.yaml"
        p.write_text(
            yaml.dump({"schema_version": 1, "kind": "user_artifact", "dimensions": {}})
        )
        ok, problems = validate("verdict", p)
        assert ok is False
        assert not any("instead" in x for x in problems), problems
