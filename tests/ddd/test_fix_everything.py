"""canopy#788 / #782 — every pass fixes everything it can; flat runs stop and ask.

Measured on the 2026-10-05..07 DDD runs:

* supply-sophie-sheets: 50 final findings, 43 deferred (33 mechanical) to a polish
  pass that only runs on convergence, which never came; five cells (4:clarity,
  5:clarity, 7:trust, 8:clarity, arc visual_variety) stayed open all 11 passes.
* the same run scored 3.0 on six consecutive passes while its findings churned
  (fix one email-wording finding, the judge raises the next), so the
  identical-findings plateau never fired and the loop ran until a human stepped in.
* Spark -004 stopped on an uneditable floor only after a checkpoint pass that
  could not have moved it.
"""
from __future__ import annotations

from scripts.ddd import loop_config
from scripts.ddd.loop_config import LoopConfig
from scripts.ddd.run_pipeline import _flat, _stamp_recurring, compute_auto_iterate
from scripts.ddd.schemas.models import Dimension, RunState, Verdict


def _v(kind: str, score: float) -> Verdict:
    return Verdict(
        schema_version=1, kind=kind, gate="gating", rubric_name=kind, ran_at="2026-10-07T00:00:00Z",
        dimensions={"clarity": Dimension(score=score, weight=1.0)},
        overall_score=score, overall_rule="lowest", verdict="warn",
    )


def _f(scene: int, dim: str, detail: str, **kw) -> dict:
    return {"scene": scene, "dimension": dim, "detail": detail, "route": "PRODUCT", "fix_kind": "mechanical",
            "severity": "medium", "fix_recommendation": "Fix it.", **kw}


def _pass(state: RunState, findings: list[dict], score: float = 3.0, **kw):
    out = compute_auto_iterate(
        state, _v("concept", score), _v("user_artifact", score), [dict(f) for f in findings],
        converged=False, unattended=True, judge_full=True, loop_config=kw.pop("cfg", LoopConfig()), **kw,
    )
    state.iteration += 1
    return out


class TestRecurring:
    def test_a_cell_open_two_passes_is_stamped_and_briefed_first(self):
        s = RunState(run_id="r", narrative_slug="n")
        _pass(s, [_f(4, "clarity", "Column header is jargon."), _f(2, "trust", "No source shown.")])
        # The judge re-words the same defect; the cell is what recurs.
        action, _ = _pass(s, [_f(2, "task_completion", "Save is hidden."), _f(4, "clarity", "Header 'LC' unexplained.")])
        assert action == "continue"
        first = s.findings[0]
        assert (first["scene"], first["dimension"], first["recurring"]) == (4, "clarity", 2)
        assert "recurring" not in s.findings[1]

    def test_the_count_resets_when_the_cell_clears(self):
        hist = [["4::clarity::a"], ["2::trust::b"], ["4::clarity::c"]]
        f = _f(4, "clarity", "c")
        assert _stamp_recurring([f], hist, 2) == [] and "recurring" not in f
        hist.append(["4::clarity::d"])
        assert _stamp_recurring([f], hist, 2) == [f] and f["recurring"] == 2

    def test_judge_defer_and_parked_are_not_forced(self):
        hist = [["4::clarity::a"], ["4::clarity::a"]]
        assert _stamp_recurring([_f(4, "clarity", "a", route="DEFER")], hist, 2) == []
        assert _stamp_recurring([_f(4, "clarity", "a", parked=True)], hist, 2) == []


class TestFlat:
    def test_score_and_backlog_flat_for_three_full_passes_stops_with_a_direction_question(self):
        # Supply's shape: the score never moves, the backlog does not shrink, but
        # the findings churn and the mean cell creeps up — so the progress-aware
        # stall rule never fires and the plateau rule (identical findings) cannot.
        s = RunState(run_id="r", narrative_slug="n")
        out = []
        for i in range(4):
            fs = [_f(6, "clarity", f"Email wording {i}."), _f(8, "trust", f"Draft tone {i}."), _f(3, "clarity", f"x{i}")]
            out.append(_pass(s, fs, distribution={"mean": 2.0 + 0.5 * i}))
        assert [a for a, _ in out[:3]] == ["continue", "continue", "continue"]
        action, reason = out[3]
        assert action == "stop_max_iter"
        assert reason.startswith("FLAT:") and "6:clarity" in reason and "Direction" in reason

    def test_a_shrinking_backlog_is_progress_even_at_a_flat_score(self):
        prog = [{"score": 3.0, "open_findings": n, "full": True} for n in (40, 30, 20, 12)]
        assert _flat(prog, 3) is None

    def test_a_score_move_is_progress(self):
        prog = [{"score": s, "open_findings": 10, "full": True} for s in (2.0, 2.0, 2.0, 3.0)]
        assert _flat(prog, 3) is None

    def test_incremental_points_do_not_count(self):
        prog = [{"score": 3.0, "open_findings": 10, "full": f} for f in (True, False, False, True, True)]
        assert _flat(prog, 3) is None  # only three full points
        assert _flat(prog + [{"score": 3.0, "open_findings": 10, "full": True}], 3)

    def test_configurable(self):
        assert loop_config.parse({"loop": {"plateau_rounds": 5, "recurring_after": 3}}).loop.plateau_rounds == 5
        assert loop_config.parse({}).loop.recurring_after == 2

