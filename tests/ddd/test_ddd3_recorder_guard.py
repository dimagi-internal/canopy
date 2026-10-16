"""DDD-3 — two #954 follow-ups.

1. The recorder refuses a DDD run dir with no capture plan for the current
   iteration. ACE noora (``newborn-care-classes-programme-cascade-2026-10-15-001``)
   iterations 3–5 ran the recorder by hand from a command copied out of a render
   log (``--ddd-orchestrated`` included), so no plan was written and every pass
   skipped the incremental capture rules. ``--unplanned-capture "<why>"`` is the
   one override; it is recorded and ``capture_scope report`` flags it.
2. The pre-judge frame lint's jargon list is config (``product.lint.jargon``),
   merged onto the built-in default so a repo (ACE) can extend it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.ddd import capture_scope, loop_config, product_lint

REPO = Path(__file__).resolve().parents[2]


def _run_dir(tmp_path: Path, iteration: int = 3) -> Path:
    run = tmp_path / "runs" / "noora-001"
    (run / "snapshots").mkdir(parents=True)
    (run / "run_state.yaml").write_text(f"run_id: noora-001\niteration: {iteration}\n")
    return run


def _outs(run: Path, it: int = 3) -> list:
    return [str(run / "snapshots") + "/", str(run / f"iter{it}_clip.mp4"), str(run / "run-report.json"), None]


def _plan(run: Path, iteration: int, mode: str = capture_scope.FULL) -> None:
    capture_scope._write_plan(run, {"iteration": iteration, "mode": mode, "scenes": [1, 2], "video": False,
                                    "carried": [], "reason": "test"})


class TestRecorderGuard:
    def test_outside_a_run_dir_is_unaffected(self, tmp_path):
        out = capture_scope.recorder_guard([str(tmp_path / "x.mp4")], override_reason=None, argv=[])
        assert out["ok"] and out["run_dir"] is None

    def test_a_run_dir_without_a_plan_is_refused(self, tmp_path):
        run = _run_dir(tmp_path)
        out = capture_scope.recorder_guard(_outs(run), override_reason=None, argv=["--ddd-orchestrated"])
        assert not out["ok"] and out["iteration"] == 3
        assert "python -m scripts.ddd.iteration render noora-001" in out["message"]
        assert capture_scope.OVERRIDE_FLAG in out["message"]
        assert not (run / capture_scope.PLAN_FILE).exists()

    def test_the_run_dir_is_found_by_run_state_not_by_path_shape(self, tmp_path):
        run = _run_dir(tmp_path)  # no ".canopy/ddd/runs/" segment (the external runs root)
        assert capture_scope.ddd_run_dir([str(run / "iter3_clip.mp4")]) == run.resolve()
        assert capture_scope.ddd_run_dir([str(run / "snapshots") + "/"]) == run.resolve()

    def test_a_plan_for_this_iteration_lets_it_record(self, tmp_path):
        run = _run_dir(tmp_path)
        _plan(run, 3)
        assert capture_scope.recorder_guard(_outs(run), override_reason=None, argv=[])["ok"]

    def test_a_stale_plan_from_an_earlier_iteration_does_not(self, tmp_path):
        run = _run_dir(tmp_path, iteration=4)
        _plan(run, 3)
        assert not capture_scope.recorder_guard(_outs(run, 4), override_reason=None, argv=[])["ok"]

    def test_the_override_needs_a_reason_and_is_recorded(self, tmp_path):
        run = _run_dir(tmp_path)
        assert not capture_scope.recorder_guard(_outs(run), override_reason="   ", argv=[])["ok"]
        out = capture_scope.recorder_guard(
            _outs(run), override_reason="debugging a seed failure", argv=["--spec", "s.yaml", "--no-video"],
        )
        assert out["ok"] and "debugging a seed failure" in out["message"]
        data = json.loads((run / capture_scope.PLAN_FILE).read_text())
        (o,) = data["overrides"]
        assert o["iteration"] == 3 and o["reason"] == "debugging a seed failure" and o["video"] is False
        assert "current" not in data  # an override is not a plan: the next hand run is refused again
        assert not capture_scope.recorder_guard(_outs(run), override_reason=None, argv=[])["ok"]

    def test_a_later_plan_keeps_the_recorded_override(self, tmp_path):
        run = _run_dir(tmp_path)
        capture_scope.recorder_guard(_outs(run), override_reason="one-off", argv=[])
        _plan(run, 3)
        assert json.loads((run / capture_scope.PLAN_FILE).read_text())["overrides"][0]["reason"] == "one-off"


class TestReportFlagsOverrides:
    def test_override_on_an_unplanned_pass(self, tmp_path):
        run = _run_dir(tmp_path)
        _plan(run, 2)
        (run / "iter3_clip.mp4").write_bytes(b"x")
        capture_scope.recorder_guard(_outs(run), override_reason="checking the new seed", argv=[])
        rep = capture_scope.report(run)
        rows = {r["iteration"]: r for r in rep["passes"]}
        assert rows[3]["capture"]["mode"] == "unplanned"
        assert rows[3]["overrides"] == [{"reason": "checking the new seed", "at": rows[3]["overrides"][0]["at"],
                                         "video": True}]
        assert rows[2]["overrides"] == []
        assert rep["totals"]["overrides"] == 1
        text = capture_scope.format_report(rep)
        assert "capture-plan guard" in text and "checking the new seed" in text
        assert "overridden 1 time(s)" in text

    def test_no_override_no_flag(self, tmp_path):
        run = _run_dir(tmp_path)
        _plan(run, 3)
        rep = capture_scope.report(run)
        assert rep["totals"]["overrides"] == 0
        assert "capture-plan guard" not in capture_scope.format_report(rep)


def _recorder(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "walkthrough" / "record_video.py"), *args],
        capture_output=True, text=True, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)},
    )


class TestRecorderCli:
    """The guard runs in ``record_video.main`` before any spec load, ffmpeg or browser."""

    def test_a_hand_run_into_a_run_dir_is_refused_even_with_ddd_orchestrated(self, tmp_path):
        run = _run_dir(tmp_path)
        out = _recorder("--spec", str(tmp_path / "missing.yaml"), "--output", str(run / "iter3_clip.mp4"),
                        "--snapshots", str(run / "snapshots") + "/", "--ddd-orchestrated")
        assert out.returncode != 0
        assert "no capture plan for iteration 3" in out.stderr, out.stderr[-800:]

    def test_an_empty_override_reason_is_refused(self, tmp_path):
        run = _run_dir(tmp_path)
        out = _recorder("--spec", str(tmp_path / "missing.yaml"), "--output", str(run / "iter3_clip.mp4"),
                        "--ddd-orchestrated", "--unplanned-capture", "")
        assert out.returncode != 0 and "needs a reason" in out.stderr, out.stderr[-800:]
        assert not (run / capture_scope.PLAN_FILE).exists()


class TestJargonConfig:
    def test_default_is_the_built_in_list(self):
        cfg = loop_config.parse({})
        assert cfg.product.lint.jargon == loop_config.DEFAULT_JARGON
        assert product_lint.DEFAULT_JARGON is loop_config.DEFAULT_JARGON

    def test_a_repo_extends_it(self):
        cfg = loop_config.parse({"product": {"lint": {"jargon": {r"\bDQA\b": "an acronym ('DQA') — spell it out"}}}})
        j = cfg.product.lint.jargon
        assert set(loop_config.DEFAULT_JARGON) < set(j) and r"\bDQA\b" in j

    def test_null_drops_a_default_and_a_list_takes_a_generic_hint(self):
        (default,) = loop_config.DEFAULT_JARGON
        j = loop_config.parse({"product": {"lint": {"jargon": {default: None}}}}).product.lint.jargon
        assert j == {}
        j = loop_config.parse({"product": {"lint": {"jargon": ["\\bLLO\\b"]}}}).product.lint.jargon
        assert "LLO" in j["\\bLLO\\b"] and default in j

    def test_a_bad_regex_is_dropped_not_fatal(self):
        j = loop_config.parse({"product": {"lint": {"jargon": {"([": "broken"}}}}).product.lint.jargon
        assert j == loop_config.DEFAULT_JARGON

    def test_lint_reads_the_configured_list(self, tmp_path):
        snap = tmp_path / "snapshots"
        snap.mkdir()
        (snap / "scene_1_page_text.json").write_text(json.dumps({"page_text": "DQA status: complete\nn<1"}))
        base = loop_config.parse({}).product.lint
        cfg = loop_config.parse({"product": {"lint": {"jargon": {r"\bDQA\b": "an acronym ('DQA') — spell it out",
                                                                  next(iter(loop_config.DEFAULT_JARGON)): None}}}})
        details = [f["detail"] for f in product_lint.lint(tmp_path, cfg=cfg.product.lint)["findings"]
                   if f["dimension"] == "jargon"]
        assert len(details) == 1 and "DQA" in details[0]
        details = [f["detail"] for f in product_lint.lint(tmp_path, cfg=base)["findings"] if f["dimension"] == "jargon"]
        assert len(details) == 1 and "n<1" in details[0]

    def test_a_hand_built_cfg_without_jargon_falls_back_to_the_default(self, tmp_path):
        snap = tmp_path / "snapshots"
        snap.mkdir()
        (snap / "scene_1_page_text.json").write_text(json.dumps({"page_text": "n<1: too few records"}))
        base = loop_config.parse({}).product.lint
        cfg = SimpleNamespace(**{k: getattr(base, k) for k in ("max_words_per_screen", "max_long_lines_per_screen",
                                                                 "long_line_words", "max_new_long_lines",
                                                                 "glossary", "fonts")})
        assert any(f["dimension"] == "jargon" for f in product_lint.lint(tmp_path, cfg=cfg)["findings"])
