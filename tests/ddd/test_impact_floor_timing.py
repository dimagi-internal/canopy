"""canopy#780 — impact-aware reuse, floor-first judging, out-of-scope stop, pass timing.

Measured on four ACE Spark runs (``spark-facilitator-programme-cascade-2026-10-06-00{1,2,4,5}``,
about 40 judged passes, none converged, gating score never above 3.0):

1. Scoped judging reused ZERO scenes: the v2 fingerprint compared each frame byte for
   byte, and every fix batch edited a shared report template.
2. Judge tiering ran the user-artifact judge only every third pass, so with the floor
   on that judge two of every three passes could not move the gating score.
3. Run ``-004``'s floor was a registry string the loop may not edit; it iterated on to
   the stall rule anyway.
4. Nobody could say where a pass's time went without reconstructing it by hand.

Each class pins one fix. Judge semantics and convergence thresholds are untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from scripts.ddd import floor, impact, judge_scope, loop_config, pass_timing
from scripts.ddd.run_pipeline import classify_termination, compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict
from scripts.walkthrough._lib import regions

W, H = 400, 300


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _frame(*, row_y: int = 100, row_colour=(220, 60, 60), footer: int = 0, noise: int = 0, seed: int = 0):
    """A synthetic report page: a header bar, the scene's subject (a coloured row),
    and a footer band elsewhere on the template."""
    img = np.full((H, W, 3), 250, dtype=np.int16)
    img[0:30, :, :] = (40, 40, 120)  # header
    img[row_y : row_y + 20, 40:360, :] = row_colour  # the subject
    img[250:270, 40:40 + 60 + footer, :] = (90, 90, 90)  # a footer band
    if noise:
        rng = np.random.default_rng(seed)
        img = img + rng.integers(-noise, noise + 1, size=img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _write_frame(run: Path, scene: int, arr) -> None:
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(snaps / f"scene_{scene}.png")


def _write_regions(run: Path, scene: int, *, row_y: int = 100, text: str = "Kuunika 66.0%") -> None:
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / f"scene_{scene}_regions.json").write_text(
        json.dumps(
            {
                "full_page": False,
                "dpr": 1,
                "regions": [
                    {
                        "key": "action:css:tr:has-text(\"Kuunika\")",
                        "source": "action",
                        "found": True,
                        "matches": [{"tag": "tr", "text": text, "attrs": {}, "box": [40, row_y, 320, 20]}],
                    }
                ],
            }
        )
    )


def _write_text(run: Path, scene: int, text: str) -> None:
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / f"scene_{scene}_page_text.json").write_text(
        json.dumps({"scene_index": scene, "page_text": text, "render_id": "r"})
    )


def _spec(run: Path, scenes: int) -> Path:
    spec = run / "spec.yaml"
    spec.write_text(
        yaml.safe_dump({"name": "t", "scenes": [{"title": f"S{i}", "narrative": f"N{i}."} for i in range(1, scenes + 1)]})
    )
    return spec


def _baseline(run: Path, scenes: int = 2, **frame_kw) -> Path:
    for i in range(1, scenes + 1):
        _write_frame(run, i, _frame(**frame_kw))
        _write_regions(run, i)
        _write_text(run, i, f"Programme report. Scene {i}.")
    spec = _spec(run, scenes)
    judge_scope.plan(run, spec)
    judge_scope.record(run, spec, iteration=0)
    return spec


def _v(score: float, kind: str = "concept", dims: dict | None = None) -> Verdict:
    dims = {k: {"weight": 1.0, **v} for k, v in (dims or {}).items()}
    return Verdict(
        schema_version=1,
        kind=kind,
        gate="gating",
        rubric_name="r",
        ran_at="2026-10-06T00:00:00Z",
        dimensions=dims or {},
        overall_score=score,
        overall_rule="lowest",
        verdict="fail" if score < 3 else "warn",
    )


# ---------------------------------------------------------------------------
# 1. impact-aware reuse
# ---------------------------------------------------------------------------


class TestRegionCapture:
    def test_narration_terms_name_what_the_scene_points_at(self) -> None:
        scene = {
            "narrative": "One click narrows the report to Kuunika: meeting regularity 66.0%, on Watch. "
            "Its facilitators start with Ulemu Chirwa and Chimwemwe Kamanga at 40.0%.",
            "show": "The MEETING REGULARITY tile reads 66.0% (Watch); 'Step 7 on time' reads 41.7%.",
            "features": [{"verify": "the first rows are Ulemu Chirwa then Mwayi Nyirenda"}],
        }
        terms = regions.narration_terms(scene)
        for t in ("Ulemu Chirwa", "Chimwemwe Kamanga", "MEETING REGULARITY", "66.0%", "41.7%", "Step 7 on time", "Mwayi Nyirenda"):
            assert t in terms, (t, terms)
        assert "7" not in terms and "One" not in terms  # bare digits / lone words say nothing

    def test_target_entries_keep_spec_form_and_skip_camera_moves(self) -> None:
        scene = {
            "actions": [
                {"kind": "click", "target": "css:tr:has-text(\"${lagging_partner_label}\")"},
                {"kind": "wait_for", "target": "css:tr:has-text(\"Ulemu Chirwa\")"},
                {"kind": "wait_for", "target": "1500"},
                {"kind": "hold", "seconds": 3},
                {"kind": "goto", "target": "/x/"},
                {"kind": "click", "target": "css:tr:has-text(\"${lagging_partner_label}\")"},
            ]
        }
        assert regions.target_entries(scene) == [
            {"key": "action:css:tr:has-text(\"${lagging_partner_label}\")", "source": "action",
             "target": "css:tr:has-text(\"${lagging_partner_label}\")"},
            {"key": "wait_for:css:tr:has-text(\"Ulemu Chirwa\")", "source": "wait_for",
             "target": "css:tr:has-text(\"Ulemu Chirwa\")"},
        ]

    def test_capture_scales_boxes_to_frame_pixels(self) -> None:
        class Loc:
            def __init__(self, rect):
                self.rect = rect

            @property
            def first(self):
                return self

            def count(self):
                return 1

            def evaluate(self, js, args, timeout=None):
                return {"tag": "tr", "text": "Kuunika 66.0%", "attrs": {"class": "row"}, "rect": self.rect}

        class Page:
            def evaluate(self, js, args):
                assert args[0] == ["Ulemu Chirwa"]  # ${var} resolved for matching
                return {"dpr": 2, "scroll": [0, 500], "viewport": [1440, 900], "terms": [
                    {"term": "Ulemu Chirwa", "matches": [{"tag": "td", "text": "Ulemu Chirwa", "attrs": {}, "rect": [10, 20, 30, 40]}]}
                ]}

            def locator(self, sel):
                return Loc([5, 6, 7, 8])

        scene = {"narrative": "Then ${worker} is first.", "actions": [{"kind": "click", "target": "css:tr.k"}]}
        out = regions.capture(Page(), scene, full_page=True, resolve=lambda s: s.replace("${worker}", "Ulemu Chirwa"))
        # full-page frames are document coordinates: rect + scroll, times the DPR
        assert out["regions"][0]["matches"][0]["box"] == [10, 1012, 14, 16]
        term = out["regions"][1]
        assert term["key"] == "term:${worker}"
        assert term["matches"][0]["box"] == [20, 1040, 60, 80]

    def test_recorder_writes_regions_beside_the_frame(self, tmp_path: Path) -> None:
        from scripts.walkthrough._lib.orchestrator import Recorder

        class Page:
            url = "https://x/"

            def wait_for_timeout(self, ms): ...

            def screenshot(self, *, path, full_page=False, timeout=None):
                Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

            def evaluate(self, script, *args):
                if "createTreeWalker" in script:
                    return {"dpr": 1, "scroll": [0, 0], "viewport": [1440, 900], "terms": []}
                if "innerText" in script:
                    return "text"
                return None

            def locator(self, sel):
                raise RuntimeError("no element")

        Recorder(snapshot_dir=tmp_path).take_snapshot(
            Page(), {"title": "t", "full_page": False, "actions": [{"kind": "click", "target": "css:#a"}]}, 4
        )
        data = json.loads((tmp_path / "scene_4_regions.json").read_text())
        assert data["scene_index"] == 4 and data["render_id"]
        assert data["regions"] == [{"key": "action:css:#a", "source": "action", "found": False, "matches": []}]


class TestImpactAwareReuse:
    def test_volatile_stamps_are_not_a_change(self) -> None:
        a = impact.scrub_volatile("Saved 2026-10-06T14:39:36+00:00 · refreshed 3 minutes ago at 14:40")
        b = impact.scrub_volatile("Saved 2026-10-06T15:02:11+00:00 · refreshed 12 minutes ago at 3:02 PM")
        assert a == b
        assert impact.scrub_volatile("figures as of 4 Oct") == "figures as of 4 Oct"  # data, not a stamp

    def test_a_template_edit_outside_the_scene_subject_reuses_the_scene(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        _write_frame(tmp_path, 1, _frame(footer=40))  # the shared footer grew on every page
        _write_frame(tmp_path, 2, _frame(footer=40))
        scope = judge_scope.plan(tmp_path, spec)
        assert (scope["rejudge"], scope["reuse"]) == ([], [1, 2])

    def test_anti_aliasing_noise_is_not_a_change(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        _write_frame(tmp_path, 1, _frame(noise=6, seed=1))
        scope = judge_scope.plan(tmp_path, spec)
        assert scope["rejudge"] == []

    def test_a_change_to_the_subject_rejudges_and_names_the_component(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        _write_frame(tmp_path, 1, _frame(row_colour=(230, 160, 40)))  # the row went amber
        scope = judge_scope.plan(tmp_path, spec)
        assert (scope["rejudge"], scope["reuse"]) == ([1], [2])
        assert scope["changed_components"] == {"1": ["region_image"]}
        assert scope["impact"]["1"]["region_image"]

    def test_the_subject_moving_with_its_content_intact_is_not_a_region_change(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        _write_frame(tmp_path, 1, _frame(row_y=104))
        _write_regions(tmp_path, 1, row_y=104)
        scope = judge_scope.plan(tmp_path, spec)
        assert scope["rejudge"] == []

    def test_a_large_layout_change_still_rejudges(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        shifted = np.full((H, W, 3), 250, dtype=np.uint8)
        shifted[0:30, :] = (40, 40, 120)
        shifted[100:120, 40:360] = (220, 60, 60)  # the subject: identical
        shifted[130:300, 0:W] = (20, 120, 20)  # a new panel fills the rest of the frame
        _write_frame(tmp_path, 1, shifted)
        scope = judge_scope.plan(tmp_path, spec)
        assert scope["rejudge"] == [1] and scope["changed_components"]["1"] == ["layout"]

    def test_region_dom_text_change_rejudges(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        _write_regions(tmp_path, 2, text="Kuunika 67.0%")
        scope = judge_scope.plan(tmp_path, spec)
        assert scope["changed_components"] == {"2": ["region_dom"]}

    def test_page_text_still_rejudges(self, tmp_path: Path) -> None:
        # The judges read the whole page text and cite below-the-fold lines
        # (run -004's floor finding quoted "scene 1 page text, below the fold").
        spec = _baseline(tmp_path)
        _write_text(tmp_path, 1, "Programme report. Scene 1. Targets come from the pilot design.")
        scope = judge_scope.plan(tmp_path, spec)
        assert scope["changed_components"] == {"1": ["page_text"]}

    def test_without_regions_the_whole_frame_is_the_region_tolerantly(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        for i in (1, 2):
            (tmp_path / "snapshots" / f"scene_{i}_regions.json").unlink()
        judge_scope.plan(tmp_path, spec)
        judge_scope.record(tmp_path, spec, iteration=1)
        _write_frame(tmp_path, 1, _frame(noise=6, seed=3))
        _write_frame(tmp_path, 2, _frame(footer=40))
        scope = judge_scope.plan(tmp_path, spec)
        assert (scope["rejudge"], scope["reuse"]) == ([2], [1])

    def test_sub_tolerance_drift_does_not_accumulate_across_reused_passes(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        tiny = _frame()
        tiny[100:102, 40:46] = (0, 0, 0)  # 12 changed pixels: under MIN_CHANGED_PIXELS
        _write_frame(tmp_path, 1, tiny)
        assert judge_scope.plan(tmp_path, spec)["rejudge"] == []
        judge_scope.record(tmp_path, spec, iteration=1)
        more = tiny.copy()
        more[104:106, 40:46] = (0, 0, 0)  # another 12: 24 since the cell was JUDGED
        _write_frame(tmp_path, 1, more)
        assert judge_scope.plan(tmp_path, spec)["rejudge"] == [1]

    def test_every_pass_is_kept_in_judge_scope_history(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        _write_frame(tmp_path, 1, _frame(row_colour=(230, 160, 40)))
        judge_scope.plan(tmp_path, spec, iteration=1)
        scope = judge_scope.plan(tmp_path, spec, force_full=True, iteration=1)
        hist = scope["passes"]
        assert [p["iteration"] for p in hist] == [None, 1, 1]
        assert hist[1]["changed_components"] == {"1": ["region_image"]}
        # A full pass still records what an incremental one would have reused.
        assert hist[2]["full"] is True and hist[2]["would_reuse"] == [2]


# ---------------------------------------------------------------------------
# 2. floor-first judging
# ---------------------------------------------------------------------------


USER_SCENES_SHAPE = {
    "kind": "user_artifact",
    "dimensions": {
        "task_completion": {"score": 4, "weight": 0.4},
        "clarity": {
            "score": 2,
            "weight": 0.35,
            "justification": "Minimum is scene 2 (2): a footnote shows internal codes.",
            "fix_recommendation": "Replace the footnote with plain words. [CODE]",
            "fix_kind": "mechanical",
        },
        "trust": {"score": 3, "weight": 0.25},
    },
    "overall_score": 2,
    "verdict": "fail",
    "scenes": [
        {"scene": 1, "overall": 3, "dimensions": {"task_completion": 4, "clarity": 3, "trust": 3}},
        {"scene": 2, "overall": 2, "dimensions": {"task_completion": 4, "clarity": 2, "trust": 3}},
    ],
}


class TestLocateFloor:
    def _verdicts(self, tmp_path: Path) -> dict:
        (tmp_path / "verdict-user.yaml").write_text(yaml.safe_dump(USER_SCENES_SHAPE))
        concept = _v(3, dims={"visual_polish": {"score": 3}, "concept_clarity": {"score": 4}})
        user = _v(2, "user_artifact", dims={k: {"score": v["score"]} for k, v in USER_SCENES_SHAPE["dimensions"].items()})
        return {"concept": concept, "user_artifact": user}

    def test_the_floor_is_the_lowest_judge_dimension_and_scene(self, tmp_path: Path) -> None:
        f = floor.locate(self._verdicts(tmp_path), run_dir=tmp_path)
        assert (f["score"], f["judges"], f["dimensions"], f["scenes"]) == (2.0, ["user"], ["clarity"], [2])

    def test_concept_scenes_come_from_the_justification(self, tmp_path: Path) -> None:
        (tmp_path / "verdict-concept.yaml").write_text(yaml.safe_dump({
            "dimensions": {"visual_polish": {"score": 2, "justification": "min over scenes [3] (scores by scene: s1=4, s2=3, s3=2)."}}
        }))
        f = floor.locate({"concept": _v(2, dims={"visual_polish": {"score": 2}})}, run_dir=tmp_path)
        assert f["cells"] == [{"verdict": "concept", "judge": "concept", "dimension": "visual_polish", "scenes": [3]}]

    def test_product_objective_floor_is_the_weakest_product_dimension(self) -> None:
        from scripts.ddd.objective import PRODUCT_DIMENSIONS

        dim = sorted(PRODUCT_DIMENSIONS)[0]
        f = floor.locate(
            {"concept": _v(4, dims={dim: {"score": 2}, "visual_polish": {"score": 1}})}, objective="product"
        )
        assert f["dimensions"] == [dim] and f["score"] == 2.0


class TestFloorFirstScope:
    FLOOR = {"score": 2.0, "judges": ["user"], "dimensions": ["clarity"], "scenes": [2],
             "cells": [{"verdict": "user_artifact", "judge": "user", "dimension": "clarity", "scenes": [2]}]}

    def test_the_floor_judge_rejudges_its_changed_scene(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        _write_frame(tmp_path, 2, _frame(row_colour=(230, 160, 40)))
        scope = judge_scope.plan(tmp_path, spec, tiered=True, floor=self.FLOOR)
        assert scope["judges"] == ["concept", "user"] and scope["user_scenes"] == [2]
        assert scope["arc"] is False and "FLOOR-FIRST" in scope["reason"]
        assert judge_scope.user_reuse(scope) == [1]

    def test_an_untouched_floor_is_carried_and_said_so(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        _write_frame(tmp_path, 1, _frame(row_colour=(230, 160, 40)))
        scope = judge_scope.plan(tmp_path, spec, tiered=True, floor=self.FLOOR)
        assert scope["judges"] == ["concept"] and "none of which changed" in scope["reason"]

    def test_an_arc_floor_reruns_the_arc_when_anything_changed(self, tmp_path: Path) -> None:
        spec = _baseline(tmp_path)
        _write_frame(tmp_path, 1, _frame(row_colour=(230, 160, 40)))
        scope = judge_scope.plan(tmp_path, spec, tiered=True, floor={"judges": ["arc"], "score": 2.0, "cells": []})
        assert scope["judges"] == ["concept", "arc"] and scope["arc"] is True

    def test_merge_user_lays_floor_scene_rows_over_the_scenes_list_shape(self) -> None:
        partial = {
            "kind": "user_artifact",
            "dimensions": {"task_completion": {"score": 4}, "clarity": {"score": 4, "justification": "fixed"}, "trust": {"score": 3}},
            "scenes": [{"scene": 2, "overall": 3, "dimensions": {"task_completion": 4, "clarity": 4, "trust": 3}}],
        }
        merged = judge_scope.merge_user(USER_SCENES_SHAPE, partial, reuse=[1])
        assert merged["dimensions"]["clarity"]["score"] == 3.0  # scene 1's carried clarity 3 is the new floor
        assert merged["overall_score"] == 3.0
        assert [e["scene"] for e in merged["scenes"]] == [1, 2]


# ---------------------------------------------------------------------------
# 3. stop on an out-of-scope floor
# ---------------------------------------------------------------------------


class TestEditScope:
    @pytest.mark.parametrize(
        "finding,kw,expected",
        [
            ({"route": "DEFER"}, {}, "out"),
            ({"fix_recommendation": "Rename the indicator title [REGISTRY]"}, {}, "out"),
            ({"fix_recommendation": "Regenerate the visits [DATA]"}, {}, "out"),
            ({"fix_scope": "narrative", "fix_recommendation": "say 66%"}, {"narrative_locked": True}, "out"),
            ({"fix_scope": "narrative", "fix_recommendation": "say 66%"}, {"narrative_locked": False}, "in"),
            ({"fix_recommendation": "edit display.targets_note"}, {"fixed_surfaces": ("display\\.targets_note",)}, "out"),
            ({"fix_recommendation": "Replace the footnote [CODE]", "dimension": "clarity", "scene": 5},
             {"declined": [{"match": "the footnote", "reason": "registry display.targets_note (ACE-owned)", "scene": "5"}]}, "out"),
            ({"fix_recommendation": "Collapse the legend into one line [PRODUCT]"}, {}, "in"),
        ],
    )
    def test_classification(self, finding: dict, kw: dict, expected: str) -> None:
        assert floor.edit_scope(finding, **kw)[0] == expected

    def test_a_floor_with_no_findings_is_not_out_of_scope(self) -> None:
        cls = floor.classify_floor({"cells": [{"judge": "user", "dimension": "trust", "scenes": [1]}]}, [])
        assert cls["all_out"] is False


FLOOR_U = {"score": 2.0, "judges": ["user"], "dimensions": ["clarity"], "scenes": [5],
           "cells": [{"verdict": "user_artifact", "judge": "user", "dimension": "clarity", "scenes": [5]}]}
REGISTRY_FLOOR = {
    "scene": 5, "dimension": "clarity", "route": "PRODUCT", "fix_kind": "mechanical", "source": "user_artifact",
    "detail": "The footnote 'Targets are the PDD's own' shows internal codes.",
    "fix_recommendation": "In the report render code, replace the footnote. [CODE]",
}
POLISH = {"scene": "1", "dimension": "visual_polish", "route": "PRODUCT", "fix_kind": "mechanical",
          "detail": "Legend is dense.", "fix_recommendation": "Collapse the legend."}


class TestOutOfScopeStop:
    def _run(self, *, declined: list | None = None, judges=None, judge_full: bool = True, findings=None, held=None):
        state = RunState(run_id="r", narrative_slug="n", loop_mode="backlog")
        action, reason = compute_auto_iterate(
            state, _v(3), _v(2, "user_artifact"),
            [dict(f) for f in (findings or [REGISTRY_FLOOR, POLISH])],
            converged=False, unattended=True, judge_full=judge_full, judges=judges,
            floor=FLOOR_U, edit_scope={"declined": declined or []}, held=held,
        )
        return state, action, reason

    def test_run_004_shape_stops_instead_of_iterating_to_the_stall_rule(self) -> None:
        state, action, reason = self._run(
            declined=[{"match": "Targets are the PDD", "reason": "registry display.targets_note (ACE-owned)"}]
        )
        assert action == "stop_out_of_scope", reason
        assert state.terminal_status == "blocked_out_of_scope"
        assert "registry display.targets_note" in reason
        assert state.gating_floor["all_out_of_scope"] is True

    def test_a_partial_pass_stops_without_a_checkpoint_detour(self) -> None:
        # canopy#788: no checkpoint can move a cell the loop may not edit, and
        # floor-first already re-judged the floor's scenes if they changed.
        state, action, _ = self._run(
            declined=[{"match": "Targets are the PDD", "reason": "registry"}], judges=["concept", "user"], judge_full=False
        )
        assert action == "stop_out_of_scope" and state.terminal_status == "blocked_out_of_scope"

    def test_a_pass_that_held_scenes_still_hands_the_stop_to_a_checkpoint(self) -> None:
        # A held scene changed and was not re-judged: it could hide a different floor.
        state, action, _ = self._run(
            declined=[{"match": "Targets are the PDD", "reason": "registry"}], judges=["concept"], judge_full=False,
            held=[3],
        )
        assert action == "checkpoint" and state.next_judge_full is True

    def test_an_in_scope_floor_continues_floor_first(self) -> None:
        state, action, reason = self._run()
        assert action == "continue"
        assert reason.startswith("FLOOR FIRST")
        assert state.findings[0]["dimension"] == "clarity" and state.findings[0]["floor"] is True
        assert "floor" not in state.findings[1]

    def test_terminal_status_is_distinct(self) -> None:
        out = classify_termination("stop_out_of_scope", converged=False, score_history=[2.0, 2.0])
        assert out["status"] == "blocked_out_of_scope" and out["terminal"]

    def test_a_stop_out_of_scope_blocks_the_next_pass(self) -> None:
        from scripts.ddd import decision

        assert "stop_out_of_scope" in decision.BLOCKING_STOPS


# ---------------------------------------------------------------------------
# 4. per-pass timing
# ---------------------------------------------------------------------------


class TestPassTiming:
    def test_rows_split_fix_render_and_each_judge_and_append(self) -> None:
        state = RunState(run_id="r", narrative_slug="n", iteration=1)
        state.pass_timings = [{"iteration": 0, "assembled_at": "2026-10-06T14:00:00+00:00"}]
        state.steps = {
            "fixer:b0-prog": {"started_at": "2026-10-06T14:01:00+00:00", "finished_at": "2026-10-06T14:05:00+00:00"},
            "fixer:b0-worker": {"started_at": "2026-10-06T14:01:00+00:00", "finished_at": "2026-10-06T14:06:00+00:00"},
            "render": {"started_at": "2026-10-06T14:07:00+00:00", "finished_at": "2026-10-06T14:10:00+00:00"},
            "judge:concept": {"started_at": "2026-10-06T14:10:30+00:00", "finished_at": "2026-10-06T14:16:30+00:00"},
            "judge:user": {"started_at": "2026-10-06T14:10:30+00:00", "finished_at": "2026-10-06T14:14:30+00:00"},
            "fixer:old": {"started_at": "2026-10-06T13:00:00+00:00", "finished_at": "2026-10-06T13:30:00+00:00"},
        }
        from datetime import datetime

        now = datetime.fromisoformat("2026-10-06T14:20:00+00:00").timestamp()
        row = pass_timing.record(state, scope={"full": False, "judges": ["concept", "user"]}, now=now)
        assert row["fix_minutes"] == 5.0 and row["render_minutes"] == 3.0
        assert row["judge_minutes"] == {"concept": 6.0, "user": 4.0}
        assert row["wall_minutes"] == 20.0 and row["other_minutes"] == 2.0
        assert "fixer:old" not in row["steps"]
        assert len(state.pass_timings) == 2
        assert "scoped" in pass_timing.format_table(state.pass_timings)

    def test_untimed_judges_fall_back_to_their_verdict_files(self, tmp_path: Path) -> None:
        import os

        (tmp_path / "verdict-concept.yaml").write_text("x")
        os.utime(tmp_path / "verdict-concept.yaml", (1_000_420, 1_000_420))
        out = pass_timing.summarize({}, since=None, now=1_000_500, judges=["concept"], run_dir=tmp_path, planned_at=1_000_000)
        assert out["judge_minutes"] == {"concept": 7.0} and out["judge_timing_source"] == "files"


class TestConfig:
    def test_floor_first_and_fixed_surfaces_parse(self) -> None:
        cfg = loop_config.parse({"loop": {"floor_first": False, "fixed_surfaces": ["registry", "(bad"]}})
        assert cfg.loop.floor_first is False and cfg.loop.fixed_surfaces == ("registry",)
        assert loop_config.parse({}).loop.floor_first is True


# ---------------------------------------------------------------------------
# end to end through assemble
# ---------------------------------------------------------------------------


class TestAssembleEndToEnd:
    def test_assemble_stamps_floor_and_timing_and_stops_on_a_declined_floor(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
        from scripts.ddd import assemble
        from scripts.ddd.runstate import load, new_run, run_dir_for

        ddd = tmp_path / "ddd"
        ddd.mkdir()
        (ddd / "config.yaml").write_text(yaml.safe_dump({"loop": {"mode": "backlog"}}))
        rid = new_run("spark", ddd_dir=ddd)
        run = run_dir_for(rid, ddd_dir=ddd)
        spec = _baseline(run, scenes=2)
        judge_scope.plan(run, spec)
        base = {"schema_version": 1, "overall_rule": "lowest", "ran_at": "t", "rubric_name": "r"}
        (run / "verdict-concept.yaml").write_text(yaml.safe_dump({
            **base, "kind": "concept", "dimensions": {"visual_polish": {"score": 3, "weight": 1.0}},
            "overall_score": 3, "verdict": "warn",
        }))
        (run / "verdict-user.yaml").write_text(yaml.safe_dump({
            **base, **USER_SCENES_SHAPE,
            "findings": [{"scene": 2, "dimension": "clarity", "fix_kind": "mechanical",
                          "detail": "The footnote shows internal codes.",
                          "fix_recommendation": "Replace the footnote. [CODE]"}],
        }))
        (run / "design_findings.json").write_text(json.dumps([POLISH]))
        (run / floor.DECLINED_FILE).write_text(json.dumps(
            [{"match": "footnote", "reason": "registry display.targets_note (ACE-owned)", "scene": "2"}]
        ))

        out = assemble.assemble(rid, spec=str(spec), ddd_dir=ddd)
        assert out["auto_iterate_next_action"] == "stop_out_of_scope"
        assert out["terminal_status"] == "blocked_out_of_scope"
        state = load(rid, ddd_dir=ddd)
        assert state.gating_floor["scenes"] == [2] and state.gating_floor["judges"] == ["user"]
        assert len(state.pass_timings) == 1
