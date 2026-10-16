"""DDD-2 — stop re-walking everything.

Measured on two ACE runs of 2026-10-15/16 (shapes copied, minimally, from their
run dirs):

* smallholder ``smallholder-seasonal-programme-cascade-2026-10-16-001`` —
  iterations 1 and 2 were full VIDEO re-films of all four scenes because the arc
  judge named two findings ``scene: partner-benchmarks`` (a spec id).
* noora ``newborn-care-classes-programme-cascade-2026-10-15-001`` — every
  product batch "edited scene(s) [1..5]" (one shared labs template), and the
  judge ledger was salted with different context files than every plan, so the
  ``context`` component moved on every scene of every pass: 0 of 30 scene
  judgements reused over six passes.

These pin: scene attribution and the global bucket; stills (not video) for
checkpoints and shared-template batches, with impact.py deciding per scene; a
consistent judge context between plan and record; the pre-judge frame lint; and
the capture/judge report.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

from scripts.ddd import capture_scope, fix_scope, judge_scope, objective, product_lint

# The smallholder spec's scene ids (the arc judge named scene 4 by its id).
SMALLHOLDER_SCENES = [
    {"id": "journey-funnel", "title": "Journey funnel", "url": "/labs/workflow/1/run/"},
    {"id": "programme-headline", "title": "Programme headline", "url": "/labs/workflow/2/run/"},
    {"id": "worker-against-peers", "title": "Worker against peers", "url": "/labs/workflow/3/run/"},
    {"id": "partner-benchmarks", "title": "Partner benchmarks", "url": "/labs/workflow/4/run/"},
]

# The two arc findings that made smallholder iterations 1–2 full video re-films.
ARC_FINDINGS = [
    {"scene": "partner-benchmarks", "dimension": "arc_shape", "route": "CONCEPT", "fix_kind": "mechanical",
     "severity": "medium", "detail": "Scene 4's close restates rather than pays off.",
     "fix_recommendation": "Rewrite scene 4's narration to land the payoff."},
    {"scene": "partner-benchmarks", "dimension": "visual_variety", "route": "CONCEPT", "fix_kind": "mechanical",
     "severity": "low", "detail": "Scene 4's end frame shares scene 2's report layout.",
     "fix_recommendation": "End scene 4 on the benchmark chart instead."},
]


# ---------------------------------------------------------------------------
# 1. Scene attribution + the global bucket
# ---------------------------------------------------------------------------


class TestAttribution:
    def test_a_spec_id_resolves_to_its_index(self):
        out = fix_scope.attribute_scenes(ARC_FINDINGS, SMALLHOLDER_SCENES)
        assert [f["scene"] for f in out] == [4, 4]
        assert out[0]["scene_ref"] == "partner-benchmarks"
        assert ARC_FINDINGS[0]["scene"] == "partner-benchmarks"  # input not mutated

    def test_titles_slugs_and_scene_n_resolve_too(self):
        idx = fix_scope.scene_index_map(SMALLHOLDER_SCENES)
        assert fix_scope.resolve_scene("Worker against peers", idx) == 3
        assert fix_scope.resolve_scene("worker-against-peers", idx) == 3
        assert fix_scope.resolve_scene("scene 2", idx) == 2
        assert fix_scope.resolve_scene("the whole arc", idx) is None

    def test_an_ambiguous_name_is_not_an_attribution(self):
        scenes = [{"title": "Report"}, {"title": "Report"}]
        assert fix_scope.attribute_scenes([{"scene": "report"}], scenes)[0]["scene"] == "report"

    def test_once_attributed_the_smallholder_batch_is_not_unscoped(self):
        bp = fix_scope.batch_plan(fix_scope.attribute_scenes(ARC_FINDINGS, SMALLHOLDER_SCENES), for_iteration=1)
        assert bp["unscoped"] is False and bp["scenes"] == [4] and bp["global"]["findings"] == 0

    def test_a_sceneless_arc_finding_goes_to_the_global_words_bucket(self):
        bp = fix_scope.batch_plan([{**ARC_FINDINGS[0], "scene": None}], for_iteration=1)
        assert bp["unscoped"] is False
        assert bp["global"] == {"findings": 1, "words": 1, "product": 0, "judges": ["arc"]}
        # The fingerprint decides which scenes' spec moved — not a recipe hold.
        assert bp["judge_scenes"] is None

    def test_a_sceneless_product_finding_is_unscoped(self):
        f = {"scene": None, "dimension": "visual_polish", "route": "PRODUCT", "fix_kind": "mechanical",
             "detail": "the shared template's footer", "fix_recommendation": "Fix the base template CSS."}
        bp = fix_scope.batch_plan([f], for_iteration=1)
        assert bp["unscoped"] is True and bp["global"]["product"] == 1

    def test_product_scenes_name_only_what_a_product_fix_touches(self):
        fs = [
            {"scene": 1, "dimension": "visual_polish", "route": "PRODUCT", "fix_kind": "mechanical"},
            {"scene": 3, "dimension": "concept_clarity", "route": "PRODUCT", "fix_kind": "mechanical",
             "fix_scope": "narrative"},
        ]
        bp = fix_scope.batch_plan(fs, for_iteration=1)
        assert bp["scenes"] == [1, 3] and bp["product_scenes"] == [1]


# ---------------------------------------------------------------------------
# 2–3. Capture: stills for templates and checkpoints; video only where needed
# ---------------------------------------------------------------------------


SPEC = {"name": "N", "base_url": "https://labs.example", "scenes": SMALLHOLDER_SCENES}


def _decide(**kw):
    hashes = capture_scope.recipe_hashes(SPEC)
    kw.setdefault("ledger", {"base_url": None, **hashes})
    return capture_scope.decide(hashes=hashes, base_url=kw.pop("base_url", None),
                                judge_full=kw.pop("judge_full", False), batch=kw.pop("batch", None), **kw)


class TestCapture:
    def test_the_smallholder_batch_after_attribution_films_nothing(self):
        bp = fix_scope.batch_plan(fix_scope.attribute_scenes(ARC_FINDINGS, SMALLHOLDER_SCENES), for_iteration=1)
        out = _decide(batch=bp)
        assert out["mode"] == "none" and out["video"] is False

    def test_a_global_words_batch_films_nothing(self):
        bp = fix_scope.batch_plan([{**ARC_FINDINGS[0], "scene": None}], for_iteration=1)
        out = _decide(batch=bp)
        assert out["mode"] == "none" and "scene-less" in out["reason"]

    def test_the_noora_product_batch_is_stills_of_every_scene_verified_by_impact(self):
        out = _decide(batch={"scope": "product", "scenes": [1, 2, 3, 4], "product_scenes": [1, 3]})
        assert out["mode"] == "scenes" and out["scenes"] == [1, 2, 3, 4]
        assert out["video"] is False and out["verify_by_impact"] and not out["carried_unverified"]

    def test_a_checkpoint_is_stills(self):
        out = _decide(judge_full=True, batch={"scope": "product", "scenes": [1]})
        assert out["mode"] == "full" and out["video"] is False
        assert capture_scope.recorder_args(out) == ["--no-video"]

    def test_a_spec_global_change_still_records_video(self):
        hashes = capture_scope.recipe_hashes(SPEC)
        out = capture_scope.decide(hashes=hashes, ledger={"base_url": None, **hashes, "top": "other"},
                                   base_url=None, judge_full=True, batch=None)
        assert out["mode"] == "full" and out["video"] is True


# ---------------------------------------------------------------------------
# 2. Shared-template edit: stills of every scene, impact decides, rest reused
# ---------------------------------------------------------------------------

W, H = 480, 240


def _take(run: Path, *, scene1_colour=(30, 30, 30)) -> None:
    """Two scenes on one template; only scene 1's subject element changes colour."""
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    for scene, text in ((1, "Classes against plan 84.3%"), (2, "Partners as of 11 Oct")):
        img = Image.new("RGB", (W, H), (250, 250, 250))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 30], fill=(40, 40, 120))  # the shared header
        d.text((48, 92), text, fill=scene1_colour if scene == 1 else (30, 30, 30))
        img.save(snaps / f"scene_{scene}.png")
        (snaps / f"scene_{scene}_regions.json").write_text(json.dumps({
            "full_page": False, "dpr": 1,
            "regions": [{"key": f"narration:{text}", "source": "narration", "found": True,
                         "matches": [{"tag": "h2", "text": text, "attrs": {}, "box": [40, 80, 300, 40]}]}],
        }))
        (snaps / f"scene_{scene}_page_text.json").write_text(json.dumps(
            {"scene_index": scene, "url": "https://labs/x/", "page_text": text, "render_id": "r"}))


def _spec(tmp: Path) -> Path:
    p = tmp / "spec.yaml"
    p.write_text(yaml.safe_dump({"name": "N", "scenes": [
        {"title": "Headline", "url": "/x/", "narrative": "Classes against plan."},
        {"title": "Partners", "url": "/x/#p", "narrative": "The partners."},
    ]}))
    return p


class TestSharedTemplate:
    def test_only_the_scene_that_actually_changed_is_rejudged(self, tmp_path):
        spec = _spec(tmp_path)
        rubric = tmp_path / "rubric.yaml"
        rubric.write_text("dimensions: []\n")
        (tmp_path / "why_brief.yaml").write_text("problem: x\n")
        _take(tmp_path)
        judge_scope.plan(tmp_path, spec, context=[rubric])
        judge_scope.record(tmp_path, spec, iteration=0)
        # The product batch named both scenes (one template) — stills of both were taken.
        _take(tmp_path, scene1_colour=(220, 30, 30))
        out = judge_scope.plan(tmp_path, spec, context=[rubric], edited_scenes=[1, 2])
        assert out["rejudge"] == [1] and out["reuse"] == [2]
        assert out["changed_components"] == {"1": ["region_image"]}


# ---------------------------------------------------------------------------
# The judge context travels between plan and record
# ---------------------------------------------------------------------------


class TestContextConsistency:
    def _run(self, tmp_path):
        spec = _spec(tmp_path)
        rubric = tmp_path / "rubric.yaml"
        rubric.write_text("dimensions: []\n")
        (tmp_path / "why_brief.yaml").write_text("problem: x\n")
        _take(tmp_path)
        return spec, rubric

    def test_record_without_context_hashes_what_plan_hashed(self, tmp_path):
        # ddd-run Step 2f: `plan --context <rubric>`; assemble: `record` (no context).
        spec, rubric = self._run(tmp_path)
        judge_scope.plan(tmp_path, spec, context=[rubric])
        judge_scope.record(tmp_path, spec, iteration=0)
        out = judge_scope.plan(tmp_path, spec, context=[rubric])
        assert out["rejudge"] == [] and out["reuse"] == [1, 2]
        assert not out.get("changed_components")
        assert judge_scope.load_ledger(tmp_path)["context_files"] == out["context_files"]

    def test_a_plan_without_context_reuses_the_ledgers_files(self, tmp_path):
        spec, rubric = self._run(tmp_path)
        judge_scope.plan(tmp_path, spec, context=[rubric])
        judge_scope.record(tmp_path, spec, iteration=0)
        assert judge_scope.plan(tmp_path, spec)["reuse"] == [1, 2]

    def test_a_real_context_edit_still_rejudges_everything(self, tmp_path):
        spec, rubric = self._run(tmp_path)
        judge_scope.plan(tmp_path, spec, context=[rubric])
        judge_scope.record(tmp_path, spec, iteration=0)
        (tmp_path / "why_brief.yaml").write_text("problem: a sharper problem\n")
        out = judge_scope.plan(tmp_path, spec, context=[rubric])
        assert out["rejudge"] == [1, 2] and out["changed_components"]["2"] == ["context"]

    def test_a_global_arc_finding_reruns_the_arc_judge_on_a_tiered_pass(self, tmp_path):
        spec, rubric = self._run(tmp_path)
        judge_scope.plan(tmp_path, spec, context=[rubric])
        judge_scope.record(tmp_path, spec, iteration=0)
        out = judge_scope.plan(tmp_path, spec, context=[rubric], tiered=True, global_judges=["arc"])
        assert out["rejudge"] == [] and "arc" in out["judges"] and out["arc"] is True

    def test_global_judges_come_from_run_state(self):
        class S:
            iteration = 2
            batch_plan = {"for_iteration": 2, "global": {"judges": ["arc"]}}

        assert judge_scope._batch_global_judges(S()) == ["arc"]
        S.batch_plan = {"for_iteration": 1, "global": {"judges": ["arc"]}}
        assert judge_scope._batch_global_judges(S()) is None


# ---------------------------------------------------------------------------
# 4. Pre-judge frame lint
# ---------------------------------------------------------------------------

# Lines copied from noora's scene_1_page_text.json.
NOORA_TEXT = (
    "Workflows\n"
    "Newborn Care Companion Classes programme report (synthetic, ace noora-health/20261015-1129)\n"
    "Partners\nOff target\nWatch\nn<1: too few records\nClick a column name for its definition"
)


def _el(path, text="", **kw):
    e = {"path": path, "tag": "div", "classes": ["x"], "display": "block", "visibility": "visible",
         "opacity": "1", "aria_hidden": False, "color_is_inherited": False}
    if text:
        e["text"] = text
    e.update(kw)
    return e


def _capture(scene, *extra, pill=True):
    els = [
        _el("header.fixed.top-0", "", position="fixed", x=64, y=0, w=1376, h=64, label="Opportunities"),
        _el("div.footer>p", "Data refreshed nightly from Connect", x=200, y=860, w=600, h=20),
        *extra,
    ]
    if pill:
        els.append(_el("button.ask-agent", "", position="fixed", x=520, y=850, w=160, h=40, label="Ask an agent"))
        els.append(_el("button.ask-agent>span", "Ask an agent", x=540, y=860, w=120, h=20, in_floating=True))
    return {"scene_index": scene, "viewport": [1440, 900], "doc_scroll_w": 1440, "doc_client_w": 1440,
            "elements": els}


class TestFrameLint:
    def test_noora_text_leaks(self):
        fs = product_lint.text_leak_findings({1: NOORA_TEXT})
        dims = {f["dimension"]: f for f in fs}
        assert set(dims) == {"internal_id_leak", "jargon"}
        assert dims["internal_id_leak"]["scene"] == 1 and dims["internal_id_leak"]["severity"] == "high"
        assert dims["internal_id_leak"]["owner"] == "demo_data"
        assert "n<1" in dims["jargon"]["detail"]

    def test_clean_text_is_silent(self):
        assert product_lint.text_leak_findings({1: "Classes against plan 84.3%\nas of 11 Oct\n10:42"}) == []

    def test_a_pill_on_every_scene_is_shared_chrome(self):
        fs, status = product_lint.geometry_findings({1: _capture(1), 2: _capture(2)})
        occ = [f for f in fs if f["dimension"] == "text_occlusion"]
        assert status == "ran" and [f["scene"] for f in occ] == [1, 2]
        assert all(f["owner"] == "shared_chrome" and f["severity"] == "low" for f in occ)
        assert "Data refreshed nightly" in occ[0]["detail"]
        # its own label (inside the floater) is not "covered by itself"
        assert "'Ask an agent'" not in occ[0]["detail"].split("covers this text")[1]

    def test_a_scene_local_floater_is_product_layout(self):
        fs, _ = product_lint.geometry_findings({1: _capture(1), 2: _capture(2, pill=False)})
        occ = [f for f in fs if f["dimension"] == "text_occlusion"]
        assert [(f["scene"], f["owner"], f["severity"]) for f in occ] == [(1, "product_layout", "medium")]

    def test_a_line_cut_in_half_under_the_header(self):
        cut = _el("div>span", "Opportunities: 5 selected", x=200, y=50, w=300, h=30)
        hidden = _el("div>h1", "Programme", x=200, y=10, w=300, h=30)  # fully under: scrolled away, fine
        fs, _ = product_lint.geometry_findings({1: _capture(1, cut, hidden, pill=False)})
        occ = [f for f in fs if f["dimension"] == "text_occlusion"]
        assert len(occ) == 1 and "Opportunities: 5 selected" in occ[0]["detail"]
        assert "Programme" not in occ[0]["detail"]

    def test_overflow_and_truncation(self):
        off = _el("th", "Attention", x=1400, y=300, w=90, h=20)
        tile = _el("p.truncate", "AT PAYMENT FLOOR", x=1100, y=200, w=120, h=20,
                   scroll_w=180, client_w=120, overflow_x="hidden", text_overflow="ellipsis")
        clipper = _el("div.overflow-hidden", "", x=120, y=280, w=1300, h=300,
                      scroll_w=1500, client_w=1300, overflow_x="hidden")
        cap = _capture(1, off, tile, clipper, pill=False)
        cap["doc_scroll_w"] = 1490
        fs, _ = product_lint.geometry_findings({1: cap}, {1: "Watch the payment floor tile."})
        by = {f["dimension"]: f for f in fs}
        assert "'Attention' runs off the right edge" in by["horizontal_overflow"]["detail"]
        assert "50px wider" in by["horizontal_overflow"]["detail"]
        assert "AT PAYMENT FLOOR" in by["text_truncation"]["detail"]
        assert by["text_truncation"]["severity"] == "high"  # the narration names it
        # the ellipsis element is reported once, as truncation, not as overflow too
        assert "p.truncate" not in by["horizontal_overflow"]["detail"]

    def test_a_capture_without_geometry_skips_and_says_so(self):
        old = {"scene_index": 1, "elements": [{"path": "p", "text": "x", "w": 10, "h": 10}]}
        fs, status = product_lint.geometry_findings({1: old})
        assert fs == [] and status.startswith("skipped")

    def test_lint_writes_the_findings_assemble_reads(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DDD_DIR", str(tmp_path / "ddd"))
        snaps = tmp_path / "snapshots"
        snaps.mkdir()
        (snaps / "scene_1_page_text.json").write_text(json.dumps({"scene_index": 1, "page_text": NOORA_TEXT}))
        (snaps / "scene_1_visual.json").write_text(json.dumps(_capture(1)))
        (snaps / "scene_2_visual.json").write_text(json.dumps(_capture(2)))
        out = product_lint.lint(tmp_path)
        assert out["checks"]["geometry"] == "ran"
        from scripts.ddd.assemble import _load_findings

        demo = {(f["scene"], f["dimension"]): f["route"] for f in _load_findings(tmp_path, demo=True)
                if f.get("source") == "product_lint"}
        # A demo films the run id: fixed, not deferred. The chrome pill stays deferred.
        assert demo[(1, "internal_id_leak")] == "PRODUCT" and demo[(1, "jargon")] == "PRODUCT"
        assert demo[(1, "text_occlusion")] == "DEFER"

    def test_frame_lint_does_not_hold_stop_done(self):
        f = {"source": "product_lint", "dimension": "internal_id_leak", "severity": "high"}
        assert objective.feeds_every_objective(f) and not objective.blocks_every_objective(f)


# ---------------------------------------------------------------------------
# 5. The report
# ---------------------------------------------------------------------------


def _write_history(run: Path) -> None:
    """Pass shapes copied from smallholder's capture-plan.json / judge-scope.json."""
    caps = [
        {"iteration": 0, "mode": "full", "scenes": [1, 2, 3, 4], "video": True, "carried": [],
         "reason": "this pass judges every scene in full — every scene is re-filmed"},
        {"iteration": 1, "mode": "full", "scenes": [1, 2, 3, 4], "video": True, "carried": [],
         "reason": "the last batch had a finding with no readable scene"},
    ]
    (run / "capture-plan.json").write_text(json.dumps({"current": caps[-1], "passes": caps}))
    passes = [
        {"iteration": 0, "full": True, "rejudge": [1, 2, 3, 4], "reuse": [], "judges": ["concept", "user", "arc"],
         "reason": "full — polish mode"},
        {"iteration": 1, "full": False, "rejudge": [1, 2, 3, 4], "reuse": [], "judges": ["concept", "arc", "user"],
         "reason": "4 scene(s) changed since iteration 0",
         "changed_components": {"1": ["context", "region_dom", "region_image", "spec", "trace"],
                                "2": ["context"], "3": ["context"], "4": ["context", "region_image", "spec", "trace"]}},
    ]
    (run / "judge-scope.json").write_text(json.dumps({"passes": passes}))
    (run / "iter0_clip.mp4").write_bytes(b"x")
    (run / "iter1_clip.mp4").write_bytes(b"x")


class TestReport:
    def test_smallholder_shape(self, tmp_path):
        _write_history(tmp_path)
        rep = capture_scope.report(tmp_path)
        t = rep["totals"]
        assert t["passes"] == 2 and t["video_refilms"] == 2 and t["scenes_rejudged"] == 8
        assert t["scenes_reused"] == 0 and t["context_only_rejudges"] == 2
        assert rep["passes"][1]["judge"]["context_only"] == [2, 3]
        # Under the current rules: iteration 1 is stills of every scene, scenes 2–3 reused.
        assert rep["replay_totals"] == {"video_refilms": 1, "scenes_rejudged": 6}
        text = capture_scope.format_report(rep)
        assert "re-judged ONLY because the judge context moved" in text

    def test_an_unplanned_render_is_visible(self, tmp_path):
        (tmp_path / "judge-scope.json").write_text(json.dumps({"passes": [
            {"iteration": 3, "full": True, "rejudge": [1], "reuse": []}]}))
        (tmp_path / "iter3_clip.mp4").write_bytes(b"x")
        row = capture_scope.report(tmp_path)["passes"][0]
        assert row["capture"]["mode"] == "unplanned" and row["capture"]["video"] is True

    def test_cli(self, tmp_path, capsys):
        _write_history(tmp_path)
        assert capture_scope._main(["report", str(tmp_path), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["totals"]["passes"] == 2
