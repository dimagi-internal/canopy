"""ddd-concept-eval's prose must never disagree with its rubric again (canopy#491).

The shipped ``rubric.yaml`` declared SEVEN weighted dimensions while ``SKILL.md``
said "six" and listed a weight vector that matched neither file. Three consecutive
ACE runs (``spark-facilitator/20260813-2126`` iterations 2-4) judged six
dimensions and recorded the wrong weights. Nothing failed; nothing warned; the
verdicts read as complete. The omitted dimension — ``use_case_soundness`` — is the
one whose findings are always ``route=CONCEPT, fix_kind=redesign``, so the loop
under-detected precisely the class it most needed to catch.

These assertions turn a silent omission into a red CI run.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.ddd.concept_rubric import check, load

SKILL_DIR = Path(__file__).resolve().parents[2] / "plugins" / "canopy" / "skills" / "ddd-concept-eval"
RUBRIC_PATH = SKILL_DIR / "rubric.yaml"
SKILL_PATH = SKILL_DIR / "SKILL.md"


@pytest.fixture(scope="module")
def rubric():
    return load(RUBRIC_PATH)


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_PATH.read_text()


class TestRubricIsHealthy:
    def test_weights_sum_to_one(self, rubric) -> None:
        assert check(rubric) == [], check(rubric)

    def test_use_case_soundness_is_declared(self, rubric) -> None:
        """The dimension whose absence caused canopy#491."""
        assert "use_case_soundness" in rubric.ids

    def test_exactly_one_advisory_dimension(self, rubric) -> None:
        assert rubric.advisory_ids == ("claim_reality_coherence",)


class TestSkillMatchesRubric:
    def test_every_dimension_appears_in_the_output_schema(self, rubric, skill_text) -> None:
        """A dimension the SKILL never emits is a hole the verdict cannot show."""
        missing = [d for d in rubric.ids if f"{d}:" not in skill_text]
        assert not missing, f"rubric dimensions absent from SKILL.md: {missing}"

    def test_every_gating_dimension_is_named_in_the_weakest_link_step(self, rubric, skill_text) -> None:
        step4 = skill_text.split("### Step 4 — Aggregate overall score", 1)
        assert len(step4) == 2, "Step 4 heading moved — update this test"
        body = step4[1].split("### Step 5", 1)[0]
        missing = [d for d in rubric.gating_ids if d not in body]
        assert not missing, f"gating dimensions missing from Step 4's list: {missing}"

    def test_the_declared_dimension_count_matches(self, rubric, skill_text) -> None:
        words = {5: "five", 6: "six", 7: "seven", 8: "eight"}
        expected_total = words[len(rubric.ids)]
        expected_gating = words[len(rubric.gating_ids)]
        assert re.search(
            rf"Scores {expected_total} weighted dimensions", skill_text
        ), f"SKILL.md's description must say '{expected_total} weighted dimensions'"
        assert re.search(
            rf"\*\*{expected_gating} gating dimensions\*\*", skill_text
        ), f"Step 4 must say '**{expected_gating} gating dimensions**'"

    def test_every_declared_weight_matches_the_rubric(self, rubric, skill_text) -> None:
        """The recorded weights were wrong, so every weighted mean was meaningless."""
        # The front-matter description carries the canonical "<dim> .NN" list.
        desc = skill_text.split("---", 2)[1]
        wrong = []
        for dim, weight in rubric.weights.items():
            m = re.search(rf"{re.escape(dim)} (\.\d+)", desc)
            if not m:
                wrong.append(f"{dim}: not declared in the description")
                continue
            declared = float("0" + m.group(1))
            if abs(declared - weight) > 1e-9:
                wrong.append(f"{dim}: description says {declared}, rubric says {weight}")
        assert not wrong, wrong

    def test_the_advisory_dimension_is_marked_advisory(self, rubric, skill_text) -> None:
        for dim in rubric.advisory_ids:
            assert re.search(rf"{re.escape(dim)}.{{0,80}}advisory", skill_text, re.IGNORECASE), (
                f"{dim} is advisory in the rubric but the SKILL never says so"
            )


class TestSkillCarriesTheAutonomyContract:
    def test_finding_class_is_in_the_finding_schema(self, skill_text) -> None:
        assert "finding_class: accuracy | strategy | unclassified" in skill_text

    def test_accuracy_is_stated_as_always_mechanical(self, skill_text) -> None:
        assert "forced to `fix_kind: mechanical`" in skill_text

    def test_the_denoise_step_exists(self, skill_text) -> None:
        assert "Step 4a" in skill_text
        assert "capping cells" in skill_text
