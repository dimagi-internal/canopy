"""canopy#785 — re-film only what a batch changed, and keep the rest of the last render.

Measured: connect-labs ``supply-sophie-sheets-2026-10-06-001`` made 11 full
recordings of 9 scenes; ACE Spark ``-002`` made 32. Judges between checkpoints
read only stills, and a ``--scene`` render overwrote the report and manifest, so
it could not be used to re-film a subset. These pin the planner, the recorder's
merge, and the bookkeeping that makes the saving measurable.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.ddd import capture_scope, iteration, pass_timing, render_check, runstate
from scripts.walkthrough._lib import scene_merge

SPEC = {
    "name": "Supply",
    "base_url": "https://labs.example",
    "setup": {"command": "seed", "rerun": "per_render", "outputs": "out.json"},
    "scenes": [
        {"title": "Tender", "url": "/t/${round2_tender_id}/", "narrative": "The tender.", "actions": [{"kind": "hover", "target": "css:h1"}]},
        {"title": "Quote", "url": "/t/${round2_tender_id}/#q", "narrative": "A quote.", "actions": [{"kind": "click", "target": "text:Record"}]},
        {"title": "Compare", "url": "/t/${round2_tender_id}/compare/", "narrative": "Compare.", "actions": [{"kind": "hover", "target": "css:tr"}]},
    ],
}


def _hashes(spec: dict) -> dict:
    return capture_scope.recipe_hashes(spec)


def _edit(spec: dict, scene: int, **fields) -> dict:
    out = json.loads(json.dumps(spec))
    out["scenes"][scene - 1].update(fields)
    return out


class TestDecide:
    LEDGER = {"base_url": None, **_hashes(SPEC)}

    def _decide(self, spec=SPEC, *, ledger=None, judge_full=False, batch=None, **kw):
        return capture_scope.decide(
            hashes=_hashes(spec), ledger=self.LEDGER if ledger is None else ledger,
            base_url=kw.pop("base_url", None), judge_full=judge_full, batch=batch, **kw,
        )

    def test_a_narration_only_batch_films_nothing(self):
        spec = _edit(SPEC, 2, narrative="A sealed quote, recorded as it arrived.")
        out = self._decide(spec, batch={"scope": "recipe", "scenes": [2]})
        assert out["mode"] == "none" and out["scenes"] == []

    def test_a_recipe_change_films_that_scene_as_a_still(self):
        spec = _edit(SPEC, 3, actions=[{"kind": "scroll_to", "target": "css:tr"}])
        out = self._decide(spec, batch={"scope": "recipe", "scenes": [3]})
        assert out["mode"] == "scenes" and out["scenes"] == [3] and out["video"] is False
        assert not out["carried_unverified"]

    def test_a_product_batch_films_the_scenes_it_edited_and_flags_the_rest(self):
        out = self._decide(batch={"scope": "product", "scenes": [1, 2]})
        assert out["mode"] == "scenes" and out["scenes"] == [1, 2]
        assert out["carried_unverified"] is True

    @pytest.mark.parametrize(
        "kw, why",
        [
            ({"judge_full": True}, "in full"),
            ({"ledger": {}}, "no earlier capture"),
            ({"base_url": "http://localhost:8000"}, "target changed"),
            ({"batch": {"scope": "product", "scenes": [], "unscoped": True}}, "no readable scene"),
            ({"batch": None}, "no batch plan"),
            ({"enabled": False}, "off"),
        ],
    )
    def test_full_whenever_it_cannot_tell(self, kw, why):
        out = self._decide(**kw)
        assert out["mode"] == "full" and out["video"] is True and why in out["reason"]

    def test_a_spec_level_change_films_everything(self):
        spec = {**SPEC, "video_viewport_width": 1440}
        assert self._decide(spec, batch={"scope": "recipe", "scenes": [1]})["mode"] == "full"
        # ...but a words-only top-level edit does not.
        spec = {**SPEC, "narrative": "Sophie closes the round."}
        assert self._decide(spec, batch={"scope": "recipe", "scenes": [1]})["mode"] == "none"

    def test_a_pending_recipe_rejudge_films_its_scenes(self):
        out = self._decide(batch=None, recipe_rejudge=[2])
        assert out["mode"] == "scenes" and out["scenes"] == [2]

    def test_recorder_args(self):
        assert capture_scope.recorder_args({"mode": "scenes", "scenes": [2, 3]}) == [
            "--capture-scenes", "2,3", "--no-video",
        ]
        assert capture_scope.recorder_args({"mode": "full"}) == []


class TestReplay:
    def test_a_reseeding_narrative_replays_every_scene_before_the_target(self):
        assert scene_merge.replay_scenes(SPEC, [2])[0] == [1, 2]

    def test_independent_scenes_film_alone(self):
        spec = {**SPEC, "setup": {"rerun": "once"}}
        assert scene_merge.replay_scenes(spec, [3])[0] == [3]

    def test_a_scene_continuing_from_the_last_one_needs_it(self):
        spec = {"scenes": [{"url": "/a"}, {"actions": [{"kind": "click", "target": "x"}]}]}
        run, why = scene_merge.replay_scenes(spec, [2])
        assert run == [1, 2] and "no url" in why

    def test_a_leading_goto_is_its_own_url(self):
        spec = {"scenes": [{"url": "/a"}, {"actions": [{"kind": "goto", "target": "/b"}]}]}
        assert scene_merge.replay_scenes(spec, [2])[0] == [2]

    def test_a_var_captured_earlier_needs_the_capturing_scene(self):
        spec = {
            "scenes": [
                {"url": "/a", "actions": [{"kind": "capture", "var": "quote_id", "target": "css:x"}]},
                {"url": "/q/${quote_id}/"},
            ]
        }
        run, why = scene_merge.replay_scenes(spec, [2])
        assert run == [1, 2] and "quote_id" in why

    def test_parse_scenes(self):
        assert scene_merge.parse_scenes("3,6") == [3, 6]
        assert scene_merge.parse_scenes("2-4,7") == [2, 3, 4, 7]
        assert scene_merge.parse_scenes(None) == []
        with pytest.raises(ValueError):
            scene_merge.parse_scenes("0")


def _report(tender: int, scenes, *, ok=True) -> dict:
    return {
        "setup": {"rerun": "per_render", "variables": {"round2_tender_id": tender}},
        "actions": [
            {"scene_index": s, "kind": "hover", "target": f"css:[data-tender='{tender}']", "ok": ok} for s in scenes
        ],
        "scenes": [{"scene_index": s, "title": f"S{s}", "start_seconds": 4.0 * s, "duration_seconds": 4.0} for s in scenes],
    }


class TestMerge:
    def test_targets_replace_their_rows_and_the_rest_keep_theirs(self):
        prior = _report(341, [1, 2, 3])
        new = _report(342, [1, 2])  # scene 1 was replayed, scene 2 re-filmed
        merged = scene_merge.merge_report(prior, new, [2])
        targets = {a["scene_index"]: a["target"] for a in merged["actions"]}
        assert targets == {1: "css:[data-tender='341']", 2: "css:[data-tender='342']", 3: "css:[data-tender='341']"}
        assert merged["captured_scenes"] == [2] and merged["carried_scenes"] == [1, 3]
        assert merged["total"] == 3 and merged["failed"] == 0
        # each carried scene keeps the ids it was filmed with
        assert merged["scene_variables"] == {"1": {"round2_tender_id": "341"}, "3": {"round2_tender_id": "341"}}
        assert merged["setup"]["variables"]["round2_tender_id"] == 342

    def test_a_scene_carried_twice_keeps_its_original_bindings(self):
        first = scene_merge.merge_report(_report(341, [1, 2]), _report(342, [1, 2]), [2])
        second = scene_merge.merge_report(first, _report(343, [1, 2]), [2])
        assert second["scene_variables"]["1"] == {"round2_tender_id": "341"}

    def test_adopt_replaces_a_targets_files_only(self, tmp_path):
        snaps, scratch = tmp_path / "snaps", tmp_path / "scratch"
        snaps.mkdir()
        scratch.mkdir()
        for n in (1, 2):
            (snaps / f"scene_{n}.png").write_bytes(b"old")
            (snaps / f"scene_{n}_before.png").write_bytes(b"old")
            (scratch / f"scene_{n}.png").write_bytes(b"new")
        scene_merge.adopt_scene_files(scratch, snaps, [2])
        assert (snaps / "scene_1.png").read_bytes() == b"old" and (snaps / "scene_1_before.png").exists()
        assert (snaps / "scene_2.png").read_bytes() == b"new"
        assert not (snaps / "scene_2_before.png").exists()  # the new take has no before-frame

    def test_judge_scope_unsubstitutes_a_carried_scene_with_its_own_ids(self):
        from scripts.ddd import stable_ids

        merged = scene_merge.merge_report(_report(341, [1, 2]), _report(342, [1, 2]), [2])
        assert stable_ids.scene_vars(merged, 1) == {"round2_tender_id": "341"}
        assert stable_ids.scene_vars(merged, 2)["round2_tender_id"] == "342"


class TestRenderCheck:
    def test_only_refilmed_scenes_must_be_fresh(self, tmp_path):
        snaps = tmp_path / "snapshots"
        snaps.mkdir()
        for n in (1, 2):
            (snaps / f"scene_{n}.png").write_bytes(b"x")
        old = 1_000_000.0
        import os

        os.utime(snaps / "scene_1.png", (old, old))
        (tmp_path / "run-report.json").write_text("{}")
        (tmp_path / "walkthrough-run-data.json").write_text(
            json.dumps({"scenes_run": [1, 2], "carried_scenes": [1]})
        )
        out = render_check.check(tmp_path, since=old + 100)
        assert out["ok"], out
        assert out["scenes_checked"] == [2] and out["scenes_carried"] == [1]


@pytest.fixture
def run(monkeypatch, tmp_path):
    ddd = tmp_path / ".canopy" / "ddd"
    ddd.mkdir(parents=True)
    monkeypatch.setenv("DDD_DIR", str(ddd))
    monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
    rid = runstate.new_run("supply", ddd_dir=ddd)
    spec = tmp_path / "supply.yaml"
    spec.write_text(yaml.safe_dump(SPEC))
    return SimpleNamespace(id=rid, dir=runstate.run_dir_for(rid, ddd), spec=spec, ddd=ddd)


def _popen(run_dir, calls):
    def popen(cmd, **kw):
        calls.append(cmd)
        (run_dir / "snapshots").mkdir(exist_ok=True)
        (run_dir / "run-report.json").write_text("{}")
        return SimpleNamespace(poll=lambda: 0)

    return popen


class TestIteration:
    def _next_pass(self, run, *, batch):
        st = runstate.load(run.id, ddd_dir=run.ddd)
        st.iteration = 1
        st.next_judge_full = False
        st.loop_mode = "backlog"
        st.batch_plan = {"for_iteration": 1, **batch}
        runstate.save(st, ddd_dir=run.ddd)

    def test_first_pass_full_then_stills_of_the_changed_scene(self, run):
        calls: list = []
        out = iteration.render(run.id, run.spec, popen=_popen(run.dir, calls))
        assert out["capture"]["mode"] == "full" and "--capture-scenes" not in calls[0]
        assert capture_scope.load_ledger(run.dir)["scenes"].keys() == {"1", "2", "3"}

        self._next_pass(run, batch={"scope": "product", "scenes": [2]})
        out = iteration.render(run.id, run.spec, popen=_popen(run.dir, calls))
        assert out["capture"]["mode"] == "scenes"
        cmd = calls[1]
        assert cmd[cmd.index("--capture-scenes") + 1] == "2" and "--no-video" in cmd
        assert capture_scope.load_plan(run.dir, 1)["carried"] == [1, 3]

    def test_a_words_only_batch_runs_no_recorder(self, run):
        calls: list = []
        iteration.render(run.id, run.spec, popen=_popen(run.dir, calls))
        spec = _edit(SPEC, 1, narrative="Sophie opens the tender she issued.")
        run.spec.write_text(yaml.safe_dump(spec))
        self._next_pass(run, batch={"scope": "recipe", "scenes": [1]})
        out = iteration.render(run.id, run.spec, popen=_popen(run.dir, calls))
        assert out["status"] == "skipped" and out["exit_code"] == 0 and len(calls) == 1
        pub = iteration.publish(run.id, run.spec)
        assert pub["ok"] and pub["deck_url"] is None and pub["capture"]["mode"] == "none"

    def test_full_capture_flag_overrides(self, run):
        calls: list = []
        iteration.render(run.id, run.spec, popen=_popen(run.dir, calls))
        self._next_pass(run, batch={"scope": "product", "scenes": [2]})
        out = iteration.render(run.id, run.spec, popen=_popen(run.dir, calls), full_capture=True)
        assert out["capture"]["mode"] == "full" and "--capture-scenes" not in calls[1]


class TestTiming:
    def test_the_row_counts_reuse_and_capture(self):
        st = SimpleNamespace(iteration=3, steps={}, pass_timings=[])
        scope = {
            "full": False, "rejudge": [2], "reuse": [1, 3], "judges": ["concept"],
            "capture": {"mode": "scenes", "scenes": [2], "carried": [1, 3]},
        }
        row = pass_timing.record(st, scope=scope)
        assert (row["rejudged"], row["reused"], row["capture"], row["captured"], row["carried"]) == (1, 2, "scenes", 1, 2)
        assert "scenes 1/3" in pass_timing.format_table([row])


class TestJudgeScopeReadsTheCapture:
    def _run(self, tmp_path, carried_unverified):
        from scripts.ddd import judge_scope

        snaps = tmp_path / "snapshots"
        snaps.mkdir()
        for n in (1, 2, 3):
            (snaps / f"scene_{n}.png").write_bytes(b"png%d" % n)
            (snaps / f"scene_{n}_page_text.json").write_text(json.dumps({"page_text": f"s{n}"}))
        spec = tmp_path / "spec.yaml"
        spec.write_text(yaml.safe_dump({"scenes": [{"title": "a"}, {"title": "b"}, {"title": "c"}]}))
        judge_scope.plan(tmp_path, spec, iteration=0)
        judge_scope.record(tmp_path, spec, iteration=0)
        (snaps / "scene_2.png").write_bytes(b"new")
        capture_scope._write_plan(
            tmp_path,
            {"iteration": 1, "mode": "scenes", "scenes": [2], "carried": [1, 3],
             "carried_unverified": carried_unverified},
        )
        return judge_scope.plan(tmp_path, spec, iteration=1)

    def test_scenes_carried_after_a_product_batch_cannot_decide(self, tmp_path):
        scope = self._run(tmp_path, True)
        assert scope["rejudge"] == [2] and scope["reuse"] == [1, 3]
        assert scope["carried_unverified"] == [1, 3]
        assert scope["capture"]["mode"] == "scenes"
        from scripts.ddd import target

        assert target.decision_needs_checkpoint("deploy", None, scope["carried_unverified"])

    def test_scenes_carried_after_a_recipe_edit_are_fine(self, tmp_path):
        scope = self._run(tmp_path, False)
        assert "carried_unverified" not in scope
