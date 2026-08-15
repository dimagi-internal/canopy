"""De-noising the weakest-link verdict (ace#1393).

Measured in ACE ``spark-facilitator/20260813-2126``: scene 12 scored 3/2/3 and
scene 10 scored 4/3 on BYTE-IDENTICAL frames, and iteration 4 fixed its entire
target scene (2/3/2/3/3 -> 4/3/3/4/4) while the headline stayed 2 because the
minimum migrated to untouched scenes that had drawn 3 the round before.
"""
from __future__ import annotations

from scripts.ddd.denoise import (
    CAP_THRESHOLD,
    NOISE_BAND,
    Cell,
    capping_cells,
    confirm,
    confirmed_floor,
    fingerprint_findings,
    improved,
    summarize,
    to_cells,
    trend,
    unconfirmed_caps,
)

# Iteration 4's shape, compressed: the fixed scene is good, four untouched
# scenes drew a 2, everything else is 3-4.
ITER4 = {
    "5": {"concept_clarity": 4, "design_soundness": 3, "visual_polish": 3, "why_groundedness": 4, "motion_friction": 4},
    "3": {"concept_clarity": 3, "design_soundness": 2, "visual_polish": 3, "why_groundedness": 3, "motion_friction": 3},
    "6": {"concept_clarity": 2, "design_soundness": 3, "visual_polish": 3, "why_groundedness": 3, "motion_friction": 3},
    "11": {"concept_clarity": 3, "design_soundness": 3, "visual_polish": 2, "why_groundedness": 3, "motion_friction": 3},
    "12": {"concept_clarity": 3, "design_soundness": 3, "visual_polish": 3, "why_groundedness": 2, "motion_friction": 3},
}


class TestDistribution:
    def test_summary_exposes_what_the_min_hides(self) -> None:
        s = summarize(to_cells(ITER4))
        assert s.n_cells == 25
        assert s.floor == 2
        assert s.mean > 2.9
        assert s.cells_at_or_above_3 == 21
        assert len(s.capping_cells) == 4
        assert set(s.per_dimension_mean) == {
            "concept_clarity",
            "design_soundness",
            "visual_polish",
            "why_groundedness",
            "motion_friction",
        }

    def test_two_runs_with_the_same_floor_are_distinguishable(self) -> None:
        """The whole point: overall_score 2 is not one object."""
        good = summarize(to_cells({"a": {"d": 4, "e": 4}, "b": {"d": 4, "e": 2}}))
        bad = summarize(to_cells({"a": {"d": 2, "e": 2}, "b": {"d": 2, "e": 2}}))
        assert good.floor == bad.floor == 2
        assert good.mean > bad.mean
        assert good.cells_at_or_above_3 > bad.cells_at_or_above_3

    def test_empty_summary_is_not_a_zero(self) -> None:
        s = summarize([])
        assert s.floor is None and s.mean is None and s.n_cells == 0

    def test_as_dict_round_trips(self) -> None:
        d = summarize(to_cells(ITER4)).as_dict()
        assert d["n_cells"] == 25
        assert len(d["capping_cells"]) == 4


class TestCappingCells:
    def test_only_cells_at_or_below_threshold_can_cap(self) -> None:
        caps = capping_cells(to_cells(ITER4))
        assert all(c.score <= CAP_THRESHOLD for c in caps)
        assert len(caps) == 4

    def test_rejudge_cost_is_bounded_to_the_caps(self) -> None:
        """The argument for the protocol: 8 extra dispatches, not 120."""
        cells = to_cells(ITER4)
        caps = capping_cells(cells)
        extra_dispatches = 2 * len(caps)  # k=3 means 2 more draws each
        assert extra_dispatches == 8
        assert extra_dispatches < len(cells)


class TestConfirmation:
    def test_median_of_three(self) -> None:
        assert confirm([3, 2, 3]) == 3  # the measured scene-12 draws
        assert confirm([4, 3]) == 3.5  # the measured scene-10 draws
        assert confirm([2]) == 2

    def test_a_cap_that_does_not_reproduce_stops_setting_the_floor(self) -> None:
        cells = to_cells({"12": {"why_groundedness": 2}, "5": {"concept_clarity": 4}})
        floor = confirmed_floor(cells, {"12::why_groundedness": [2, 3, 3]})
        assert floor == 3

    def test_a_cap_that_reproduces_still_blocks(self) -> None:
        cells = to_cells({"12": {"why_groundedness": 2}, "5": {"concept_clarity": 4}})
        floor = confirmed_floor(cells, {"12::why_groundedness": [2, 2, 3]})
        assert floor == 2

    def test_no_confirmations_is_the_old_behaviour(self) -> None:
        cells = to_cells(ITER4)
        assert confirmed_floor(cells, None) == 2

    def test_non_capping_cells_are_never_rejudged(self) -> None:
        """A confirmation on a high cell must not be able to LOWER the floor."""
        cells = to_cells({"a": {"d": 4}, "b": {"d": 3}})
        assert confirmed_floor(cells, {"a::d": [4, 1, 1]}) == 3

    def test_unconfirmed_caps_are_reported_not_swallowed(self) -> None:
        cells = to_cells({"12": {"why_groundedness": 2}, "6": {"concept_clarity": 2}})
        noise = unconfirmed_caps(
            cells,
            {"12::why_groundedness": [2, 3, 3], "6::concept_clarity": [2, 2, 1]},
        )
        assert [c.key for c in noise] == ["12::why_groundedness"]

    def test_empty_floor_is_none_not_zero(self) -> None:
        assert confirmed_floor([], {}) is None


class TestNoiseBand:
    def test_a_sub_band_move_is_not_evidence_either_way(self) -> None:
        assert improved(3.47, 3.42) is None  # the measured iter3 -> iter4 mean move
        assert improved(2.0, 2.0) is None

    def test_a_real_move_is_read(self) -> None:
        assert improved(2.0, 3.0) is True
        assert improved(3.0, 2.0) is False

    def test_band_is_smaller_than_the_measured_variance(self) -> None:
        assert 0 < NOISE_BAND <= 1.0

    def test_none_input_is_unknown(self) -> None:
        assert improved(None, 3.0) is None
        assert improved(3.0, None) is None


class TestTrend:
    def test_flat_is_flat_not_regressing(self) -> None:
        """Four iterations of real fixes reading '2, 2, 2, 2' is FLAT, not a regression."""
        assert trend([2.0, 2.0, 2.0, 2.0]) == "flat"

    def test_noise_wobble_is_flat(self) -> None:
        assert trend([3.47, 3.42, 3.45]) == "flat"

    def test_real_climb_is_improving(self) -> None:
        assert trend([2.0, 3.0, 4.0]) == "improving"

    def test_real_fall_is_regressing(self) -> None:
        assert trend([4.0, 3.0, 2.0]) == "regressing"

    def test_too_few_points_is_unknown(self) -> None:
        assert trend([]) == "unknown"
        assert trend([3.0]) == "unknown"


class TestFingerprints:
    def test_identical_findings_fingerprint_identically(self) -> None:
        a = [{"scene": "1", "dimension": "d", "detail": "The label is wrong."}]
        b = [{"scene": "1", "dimension": "d", "detail": "the   label is  wrong."}]
        assert fingerprint_findings(a) == fingerprint_findings(b)

    def test_order_does_not_matter(self) -> None:
        a = [{"scene": "1", "dimension": "d", "detail": "x"}, {"scene": "2", "dimension": "d", "detail": "y"}]
        assert fingerprint_findings(a) == fingerprint_findings(list(reversed(a)))

    def test_a_different_defect_fingerprints_differently(self) -> None:
        a = [{"scene": "1", "dimension": "d", "detail": "x"}]
        b = [{"scene": "1", "dimension": "d", "detail": "z"}]
        assert fingerprint_findings(a) != fingerprint_findings(b)

    def test_the_signal_is_not_score_dependent(self) -> None:
        """Unlike the score, the fingerprint does not wobble with the judge's draw."""
        a = [{"scene": "1", "dimension": "d", "detail": "x", "severity": "medium"}]
        b = [{"scene": "1", "dimension": "d", "detail": "x", "severity": "high"}]
        assert fingerprint_findings(a) == fingerprint_findings(b)

    def test_empty(self) -> None:
        assert fingerprint_findings(None) == []


def test_cell_key_is_stable() -> None:
    assert Cell(scene="12", dimension="why_groundedness", score=2).key == "12::why_groundedness"
