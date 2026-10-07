"""The product objective — the loop optimizes the PRODUCT, not the demo's weakest cell.

Measured case: connect-labs ``supply-sophie-unanswered-round-2026-10-03-002``
iteration 6. Every judge sat at 3/5; 25 of 27 open findings were severity
``low``; the two ``medium`` ones were a claim/reality wording note and an arc
option. Under the demo objective that run could only stall
(``stopped_not_converged``). Under the product objective it is done: one polish
pass, then ``stop_done``.
"""
from __future__ import annotations

import json

from scripts.ddd import fix_scope, loop_config, objective, product_lint
from scripts.ddd.loop_config import LoopConfig, ProductConfig
from scripts.ddd.run_pipeline import compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict
from scripts.narrative.models import Dimension


def _v(kind: str, dims: dict[str, float], gate: str = "gating") -> Verdict:
    return Verdict(
        schema_version=1,
        kind=kind,
        gate=gate,
        rubric_name=kind,
        ran_at="2026-10-04T00:00:00Z",
        dimensions={k: Dimension(score=s, weight=1.0) for k, s in dims.items()},
        overall_score=min(dims.values()),
        overall_rule="lowest",
        verdict="warn",
    )


# The shape of the real iteration-6 verdicts.
CONCEPT = _v(
    "concept",
    {
        "concept_clarity": 4,
        "use_case_soundness": 4,
        "design_soundness": 3,
        "visual_polish": 3,
        "why_groundedness": 4,
        "motion_friction": 3,
    },
)
USER = _v("user_artifact", {"clarity": 3, "task_completion": 4, "trust": 4})
ARC = _v("arc", {"arc_shape": 3, "visual_variety": 3})

LOW_POLISH = {
    "scene": "1",
    "dimension": "visual_polish",
    "route": "PRODUCT",
    "fix_kind": "mechanical",
    "severity": "low",
    "detail": "Render the 'Missing from quote' values as the outlined fact-gap chip.",
    "fix_recommendation": "Render the cell values as the outlined fact-gap chip.",
}
LOW_PRODUCT_NIT = {
    "scene": "1",
    "dimension": "design_soundness",
    "route": "PRODUCT",
    "fix_kind": "mechanical",
    "severity": "low",
    "detail": "The same act is labelled 'Open draft' in the table and 'Remind' in the panel.",
    "fix_recommendation": "Use one label for the reminder action in both places.",
}
CLAIM_WORDING = {
    "scene": "7",
    "dimension": "claim_reality_coherence",
    "route": "DEFER",
    "fix_kind": "mechanical",
    "severity": "medium",
    "detail": "The narration says the row reads what changed; the screen shows only the current state.",
    "fix_recommendation": "Re-script the narration.",
}
ARC_OPTION = {
    "scene": "5,6,7",
    "dimension": "arc_shape",
    "route": "CONCEPT",
    "fix_kind": "options",
    "severity": "medium",
    "detail": "The arc peaks at scene 5.",
    "fix_recommendation": "Either (a) ... or (b) ...",
}
MEDIUM_DOMAIN = {
    "scene": "4",
    "dimension": "trust",
    "route": "PRODUCT",
    "fix_kind": "mechanical",
    "severity": "medium",
    "detail": "Landed cost omits clearing under buyer-import terms.",
    "fix_recommendation": "Add a clearing estimate to the tender and include it in landed cost.",
}
ACCURACY_CLARITY = {
    "scene": "4",
    "dimension": "clarity",
    "route": "PRODUCT",
    "fix_kind": "options",
    "severity": "low",
    "detail": 'The narration says "settled" but the panel is titled NOT SETTLED.',
    "fix_recommendation": "Either rename the panel or change the line.",
}


def _state(**kw) -> RunState:
    kw.setdefault("objective", "product")
    return RunState(run_id="r-1", narrative_slug="supply", **kw)


def _iterate(state, findings, **kw):
    kw.setdefault("loop_config", LoopConfig(objective="product"))
    kw.setdefault("product_config", ProductConfig())
    kw.setdefault("extra_verdicts", {"arc": ARC})
    kw.setdefault("unattended", True)
    return compute_auto_iterate(state, CONCEPT, USER, findings, **kw)


class TestConfig:
    def test_objective_and_product_block_parse(self):
        cfg = loop_config.parse(
            {
                "loop": {"objective": "Product"},
                "product": {
                    "floor": 3.5,
                    "block_severities": ["high"],
                    "polish_pass": False,
                    "lint": {"glossary": {"Round": "tender"}, "fonts": "Inter", "max_new_long_lines": 1},
                },
            }
        )
        assert cfg.loop.objective == "product"
        assert cfg.product.floor == 3.5
        assert cfg.product.block_severities == ("high",)
        assert cfg.product.polish_pass is False
        assert cfg.product.lint.glossary == {"round": "tender"}
        assert cfg.product.lint.fonts == ("Inter",)
        assert cfg.product.lint.max_new_long_lines == 1

    def test_defaults_and_garbage(self):
        cfg = loop_config.parse({"loop": {"objective": "vibes"}, "product": "nope"})
        assert cfg.loop.objective == "demo"  # the default: ACE's demo runs ship the video
        assert cfg.product == ProductConfig()


class TestResolve:
    def test_auto_picks_product_on_a_big_backlog_and_sticks(self):
        s = RunState(run_id="r", narrative_slug="n")
        assert objective.resolve("auto", s, 27, backlog_min_findings=8, judge_full=True) == "product"
        s.objective = "product"
        assert objective.resolve("auto", s, 1, backlog_min_findings=8, judge_full=True) == "product"

    def test_auto_picks_demo_on_a_near_converged_product(self):
        s = RunState(run_id="r", narrative_slug="n")
        assert objective.resolve("auto", s, 2, backlog_min_findings=8, judge_full=True) == "demo"

    def test_explicit_config_wins(self):
        s = RunState(run_id="r", narrative_slug="n", objective="product")
        assert objective.resolve("demo", s, 40, backlog_min_findings=8, judge_full=True) == "demo"


class TestPartition:
    def test_roles(self):
        from scripts.ddd import finding_class

        fs = finding_class.normalize_findings(
            [LOW_POLISH, LOW_PRODUCT_NIT, MEDIUM_DOMAIN, ARC_OPTION, ACCURACY_CLARITY]
        )
        out = objective.partition(fs, block_severities=("high", "medium"))
        roles = {f["dimension"] + ":" + f["severity"]: f["objective_role"] for f in out}
        assert roles["visual_polish:low"] == "deferred"
        assert roles["design_soundness:low"] == "deferred"
        assert roles["trust:medium"] == "blocking"
        assert roles["arc_shape:medium"] == "deferred"  # presentation, whatever the severity
        assert roles["clarity:low"] == "deferred"  # low narration nits wait for the polish pass

    def test_accuracy_is_a_narration_edit_never_a_product_pr(self):
        from scripts.ddd import finding_class

        (f,) = objective.partition(
            finding_class.normalize_findings([{**ACCURACY_CLARITY, "severity": "medium"}]),
            block_severities=("medium",),
        )
        assert f["objective_role"] == "ride_along"
        assert f["fix_scope"] == "narrative"
        assert fix_scope.lands_in_product(f) is False
        assert fix_scope.batch_plan([f])["scope"] == fix_scope.RECIPE

    def test_deferred_findings_keep_their_route_for_the_polish_pass(self):
        (f,) = objective.partition([LOW_POLISH], block_severities=("medium",))
        assert f["route"] == "DEFER" and f["deferred_route"] == "PRODUCT"
        (g,) = objective.restore_deferred([f])
        assert g["route"] == "PRODUCT" and g["objective_role"] == "polish"


class TestConvergence:
    def test_the_real_iteration_6_converges_under_product(self):
        ok, why = objective.converged(
            {"concept": CONCEPT, "user_artifact": USER, "arc": ARC},
            objective.partition([LOW_POLISH, LOW_PRODUCT_NIT, ARC_OPTION], block_severities=("high", "medium")),
            ProductConfig(),
        )
        assert ok, why

    def test_a_blocking_product_finding_holds_it_open(self):
        ok, why = objective.converged(
            {"concept": CONCEPT, "user_artifact": USER},
            objective.partition([MEDIUM_DOMAIN], block_severities=("high", "medium")),
            ProductConfig(),
        )
        assert not ok and "blocking" in why

    def test_a_product_dimension_below_the_floor_holds_it_open(self):
        weak_user = _v("user_artifact", {"clarity": 2, "task_completion": 4, "trust": 4})
        ok, why = objective.converged({"concept": CONCEPT, "user_artifact": weak_user}, [], ProductConfig())
        assert not ok and "clarity" in why

    def test_a_broken_presentation_cell_still_blocks(self):
        broken = _v("concept", {"design_soundness": 4, "visual_polish": 1})
        ok, why = objective.converged({"concept": broken, "user_artifact": USER}, [], ProductConfig())
        assert not ok and "broken" in why


class TestLoop:
    FINDINGS = [LOW_POLISH, LOW_PRODUCT_NIT, CLAIM_WORDING, ARC_OPTION]

    def test_demo_objective_is_unchanged_and_cannot_converge_here(self):
        s = _state(objective="demo")
        action, _ = _iterate(s, self.FINDINGS, loop_config=LoopConfig(objective="demo"))
        assert action != "stop_done"
        assert s.objective == "demo"

    def test_product_objective_polishes_once_then_stops_done(self):
        s = _state()
        action, reason = _iterate(s, self.FINDINGS)
        assert action == "continue" and "POLISH PASS" in reason
        assert s.polish_pass == {"iteration": 1, "status": "pending"}
        polish = [f for f in s.findings if f.get("objective_role") == "polish"]
        assert {f["dimension"] for f in polish} == {"visual_polish", "design_soundness"}
        assert all(f["route"] != "DEFER" for f in polish)

        s.iteration = 1
        action, reason = _iterate(s, self.FINDINGS)
        assert s.polish_pass["status"] == "done"
        assert action == "stop_done", reason
        assert s.terminal_status.startswith("converged")

    def test_polish_pass_can_be_turned_off(self):
        s = _state()
        action, _ = _iterate(s, self.FINDINGS, product_config=ProductConfig(polish_pass=False))
        assert action == "stop_done"

    def test_blocking_product_finding_keeps_the_loop_working(self):
        s = _state()
        action, reason = _iterate(s, self.FINDINGS + [MEDIUM_DOMAIN])
        assert action == "continue"
        assert "POLISH" not in reason
        # Deferred polish does not ride in the product batch.
        assert s.batch_plan["findings"] == 1

    def test_progress_score_is_the_weakest_product_dimension(self):
        s = _state()
        _iterate(s, self.FINDINGS + [MEDIUM_DOMAIN])
        assert s.score_history == [3.0]
        assert s.progress_history[-1]["open_findings"] == 1

    def test_never_decides_from_an_inner_loop_pass(self):
        s = _state()
        action, _ = _iterate(s, self.FINDINGS, target="inner")
        assert action == "checkpoint"
        assert s.polish_pass is None


class TestProductLint:
    def _run(self, tmp_path, texts: dict[int, str], fonts: dict[int, list[str]] | None = None):
        snap = tmp_path / "snapshots"
        snap.mkdir(exist_ok=True)
        for i, t in texts.items():
            (snap / f"scene_{i}_page_text.json").write_text(
                json.dumps({"scene_index": i, "page_text": t})
            )
        for i, fams in (fonts or {}).items():
            (snap / f"scene_{i}_visual.json").write_text(
                json.dumps(
                    {"scene_index": i, "elements": [{"text": "x", "font_family": f} for f in fams]}
                )
            )
        return tmp_path

    PROSE = (
        "First, the tender's own decision: two quotes leave the import to us, so there is "
        "no landed total for them until the tender settles its import duty terms today."
    )
    ROW = "\t".join(["Savanna Ready Foods", "16 Sep 2026", "No reply · 17 days"] * 4)

    def test_counts_prose_not_table_rows(self, tmp_path):
        cfg = loop_config.LintConfig(max_long_lines_per_screen=1)
        run = self._run(tmp_path, {1: "\n".join([self.PROSE, self.PROSE + " x", self.ROW, self.ROW])})
        out = product_lint.lint(run, cfg=cfg)
        (f,) = [f for f in out["findings"] if "lines of" in f["detail"]]
        assert f["measured"] == 2 and f["source"] == "product_lint"

    def test_flags_prose_the_run_added_since_its_first_render(self, tmp_path):
        cfg = loop_config.LintConfig(max_long_lines_per_screen=10)
        run = self._run(tmp_path, {1: "Tender 2\nStatus: collecting quotes"})
        assert product_lint.lint(run, cfg=cfg)["findings"] == []
        self._run(tmp_path, {1: "Tender 2\nStatus: collecting quotes\n" + self.PROSE})
        (f,) = product_lint.lint(run, cfg=cfg)["findings"]
        assert f["severity"] == "high" and "ADDED 1" in f["detail"]

    def test_glossary_on_screen_and_in_narration(self, tmp_path):
        import yaml

        cfg = loop_config.LintConfig(glossary={"round": "tender"})
        run = self._run(tmp_path, {1: "RUTF round 2 · 6 invited"})
        spec = tmp_path / "spec.yaml"
        spec.write_text(yaml.safe_dump({"scenes": [{"title": "Rounds", "narration": "the round stalls"}]}))
        out = product_lint.lint(run, spec=spec, cfg=cfg)
        wheres = sorted(f["fix_scope"] for f in out["findings"])
        assert wheres == ["narrative", "product"]

    def test_fonts_outside_the_design_system(self, tmp_path):
        cfg = loop_config.LintConfig(fonts=("Inter",))
        run = self._run(tmp_path, {1: "ok"}, fonts={1: ["Inter", "Inter", "Georgia", "monospace"]})
        (f,) = product_lint.lint(run, cfg=cfg)["findings"]
        assert f["dimension"] == "design_system" and "Georgia" in f["detail"] and "monospace" not in f["detail"]


class TestAssembleLoadsLensFindings:
    def test_product_and_lint_findings_are_loaded_with_their_source(self, tmp_path):
        from scripts.ddd.assemble import _load_findings

        (tmp_path / "lint_findings.json").write_text(json.dumps({"findings": [{"dimension": "terminology"}]}))
        (tmp_path / "product_findings.json").write_text(json.dumps({"findings": [{"dimension": "missing_view"}]}))
        got = {f["source"]: f for f in _load_findings(tmp_path)}
        assert set(got) == {"product_lint", "product_lens"}
        assert got["product_lens"]["route"] == "PRODUCT"
        demo = _load_findings(tmp_path, demo=True)
        assert all(f["route"] == "DEFER" for f in demo)


class TestProseBlocksInDemo:
    """canopy#786: prose_density lint blocks in the demo objective too."""

    PROSE = {
        "scene": 6,
        "dimension": "prose_density",
        "source": "product_lint",
        "route": "PRODUCT",
        "fix_kind": "mechanical",
        "severity": "high",
        "detail": "This run ADDED 1 explanatory line(s) to scene 6 since its first render.",
        "fix_recommendation": "Remove the added sentences and express what they explain as structure.",
    }
    PASSING = (_v("concept", {"concept_clarity": 4, "design_soundness": 4}), _v("user_artifact", {"clarity": 4}))

    def _demo(self, findings):
        s = _state(objective="demo")
        action, reason = compute_auto_iterate(
            s, *self.PASSING, findings, loop_config=LoopConfig(objective="demo"),
            product_config=ProductConfig(), unattended=True,
        )
        return s, action, reason

    def test_every_judge_passing_is_not_done_while_prose_is_open(self):
        _, action, reason = self._demo([self.PROSE])
        assert action == "continue"
        assert "prose_density" in reason

    def test_converges_once_the_prose_is_gone(self):
        _, action, _ = self._demo([])
        assert action == "stop_done"

    def test_a_low_or_deferred_prose_finding_does_not_hold_it(self):
        _, action, _ = self._demo([{**self.PROSE, "severity": "low"}])
        assert action == "stop_done"
        _, action, _ = self._demo([{**self.PROSE, "route": "DEFER"}])
        assert action == "stop_done"

    def test_demo_loader_keeps_prose_density_live_and_defers_the_rest(self, tmp_path):
        from scripts.ddd.assemble import _load_findings

        (tmp_path / "lint_findings.json").write_text(
            json.dumps({"findings": [{"dimension": "prose_density"}, {"dimension": "terminology"}]})
        )
        (tmp_path / "product_findings.json").write_text(json.dumps({"findings": [{"dimension": "prose_density"}]}))
        got = {(f["source"], f["dimension"]): f["route"] for f in _load_findings(tmp_path, demo=True)}
        assert got == {
            ("product_lint", "prose_density"): "PRODUCT",
            ("product_lint", "terminology"): "DEFER",
            ("product_lens", "prose_density"): "DEFER",
        }
