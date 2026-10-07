"""canopy#788 — a finished run writes what it learned where the next run reads it.

Six connect-labs runs ended with no ``.canopy/ddd/learnings.md`` written; the
supply agent: "Nothing has gone back into the DDD process".
"""
from __future__ import annotations

from scripts.ddd import run_learnings
from scripts.ddd.schemas.models import RunState


def _state(**kw) -> RunState:
    s = RunState(run_id="supply-001", narrative_slug="supply")
    s.auto_iterate_next_action = "stop_out_of_scope"
    s.auto_iterate_reason = "The gating floor is held only by a registry string."
    s.terminal_status = "blocked_out_of_scope"
    s.score_history = [2.0, 3.0, 3.0]
    s.findings = [
        {"scene": 4, "dimension": "clarity", "recurring": 5, "route": "PRODUCT"},
        {"scene": 6, "dimension": "trust", "route": "PRODUCT"},
    ]
    s.gating_floor = {"findings": [{"scene": 5, "dimension": "clarity", "edit_scope": "out",
                                    "edit_scope_reason": "fixed surface: registry",
                                    "fix_recommendation": "Replace the targets footnote."}]}
    s.pass_timings = [{"wall_minutes": 40, "rejudged": 9, "reused": 0, "capture": "full"},
                      {"wall_minutes": 20, "rejudged": 2, "reused": 7, "capture": "scenes"}]
    s.inner_loop_policy = {"status": "dropped", "reason": "started local; now off"}
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_the_block_says_what_the_next_run_needs(tmp_path):
    path = run_learnings.record(_state(), tmp_path)
    text = path.read_text()
    assert "<!-- ddd-run:supply-001 -->" in text and "stop_out_of_scope" in text
    assert "2, 3, 3" in text
    assert "4:clarity (open 5 passes)" in text and "6:trust" not in text
    assert "Replace the targets footnote" in text and "registry" in text
    assert "re-judged 11, reused 7" in text and "full×1" in text
    assert "Inner loop dropped" in text


def test_rewriting_replaces_the_runs_block_and_keeps_the_rest(tmp_path):
    (tmp_path / "learnings.md").write_text("# DDD learnings\n\n- [2026-10-01] hand-written note\n")
    run_learnings.record(_state(), tmp_path)
    run_learnings.record(_state(score_history=[2.0, 3.0, 3.0, 3.0]), tmp_path)
    text = (tmp_path / "learnings.md").read_text()
    assert text.count("<!-- ddd-run:supply-001 -->") == 1
    assert "3, 3, 3" in text and "hand-written note" in text


def test_only_a_stop_ends_the_run():
    assert run_learnings.ends_run("stop_done") and run_learnings.ends_run("stop_max_iter")
    assert not run_learnings.ends_run("continue") and not run_learnings.ends_run("checkpoint")
    assert not run_learnings.ends_run(None)
