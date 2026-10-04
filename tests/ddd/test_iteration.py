"""``scripts.ddd.iteration`` — render and publish an iteration as two commands.

Why: ``ddd-run`` Steps 2/2b were copy-paste bash plus an inline ``python -c`` that
edited run_state; the first live product-objective run re-assembled it by hand
every iteration (and went looking in an old run's render log for the command).
"""
from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from scripts.ddd import iteration, runstate


@pytest.fixture
def run(monkeypatch, tmp_path):
    ddd = tmp_path / ".canopy" / "ddd"
    ddd.mkdir(parents=True)
    monkeypatch.setenv("DDD_DIR", str(ddd))
    monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
    rid = runstate.new_run("supply", ddd_dir=ddd)
    spec = tmp_path / "supply.recipe.yaml"
    spec.write_text("name: Supply walkthrough\nscenes: []\n")
    return SimpleNamespace(id=rid, dir=runstate.run_dir_for(rid, ddd), spec=spec, ddd=ddd)


def _fake_recorder(run_dir, *, rc=0, scenes=(1, 2), clip=True):
    """A popen that 'records': writes what record_video writes, then exits ``rc``."""
    calls = []

    def popen(cmd, **kw):
        calls.append((cmd, kw))
        (run_dir / "snapshots").mkdir(exist_ok=True)
        for n in scenes:
            (run_dir / "snapshots" / f"scene_{n}.png").write_bytes(b"png")
        (run_dir / "walkthrough-run-data.json").write_text(json.dumps({"scenes_run": list(scenes)}))
        (run_dir / "run-report.json").write_text("{}")
        if clip:
            (run_dir / "iter0_clip.mp4").write_bytes(b"mp4")
        return SimpleNamespace(poll=lambda: rc)

    popen.calls = calls
    return popen


class TestRender:
    def test_one_command_with_the_ddd_flag_set(self, run):
        popen = _fake_recorder(run.dir)
        out = iteration.render(run.id, run.spec, popen=popen)
        assert out["exit_code"] == 0 and out["status"] == "ok"
        cmd, kw = popen.calls[0]
        assert cmd[:6] == ["uv", "run", "--project", str(iteration.RUNTIME), "--extra", "browser"]
        for flag in ("--ddd-orchestrated", "--capture-action-frames", "--skip-same-url", "--skip-empty-scenes"):
            assert flag in cmd
        assert cmd[cmd.index("--output") + 1].endswith("iter0_clip.mp4")
        assert kw["stdout"].name.endswith("render-iter0.log")
        assert kw["env"]["PYTHONPATH"].split(":")[0] == str(iteration.RUNTIME)
        assert (run.dir / ".render_start").exists() and (run.dir / ".render_rc").read_text().strip() == "0"
        st = runstate.load(run.id, ddd_dir=run.ddd)
        assert st.steps["render"]["status"] == "ok"

    def test_an_inner_loop_pass_renders_the_local_build(self, run):
        st = runstate.load(run.id, ddd_dir=run.ddd)
        st.current_target = {"iteration": 0, "target": "inner", "base_url": "http://localhost:8000"}
        runstate.save(st, ddd_dir=run.ddd)
        popen = _fake_recorder(run.dir)
        iteration.render(run.id, run.spec, popen=popen)
        cmd = popen.calls[0][0]
        assert cmd[cmd.index("--base-url") + 1] == "http://localhost:8000"

    def test_a_stale_target_stamp_is_ignored(self, run):
        st = runstate.load(run.id, ddd_dir=run.ddd)
        st.current_target = {"iteration": 5, "target": "inner", "base_url": "http://localhost:8000"}
        runstate.save(st, ddd_dir=run.ddd)
        popen = _fake_recorder(run.dir)
        iteration.render(run.id, run.spec, popen=popen)
        assert "--base-url" not in popen.calls[0][0]

    def test_a_failed_recorder_keeps_its_exit_code(self, run):
        out = iteration.render(run.id, run.spec, popen=_fake_recorder(run.dir, rc=3))
        assert out["exit_code"] == 3 and out["status"] == "failed"
        assert (run.dir / ".render_rc").read_text().strip() == "3"


class FakeTools:
    """subprocess.run for the deck generator and the uploader."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        script = cmd[cmd.index("python") + 1]
        if script.endswith("generate_presentation.py"):
            out = cmd[cmd.index("--output") + 1]
            open(out, "w").write("<html>deck</html>")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        role = cmd[cmd.index("--role") + 1]
        if role in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "error: upload failed (HTTP 502)")
        wid = f"{role}-id"
        stdout = f"View: https://canopy.test/walkthrough/{wid}\nShare: https://canopy.test/walkthrough/{wid}?t=TOKEN\n"
        return subprocess.CompletedProcess(cmd, 0, stdout, "")


class TestPublish:
    def _rendered(self, run, **kw):
        iteration.render(run.id, run.spec, popen=_fake_recorder(run.dir, **kw))

    def test_checks_uploads_links_and_stamps(self, run):
        self._rendered(run)
        st = runstate.load(run.id, ddd_dir=run.ddd)
        st.narrative_review_url = "https://canopy.test/review/n1/"
        runstate.save(st, ddd_dir=run.ddd)
        tools = FakeTools()
        out = iteration.publish(run.id, run.spec, run=tools)
        assert out["ok"] and out["upload_errors"] == []
        assert out["deck_url"] == "https://canopy.test/walkthrough/deck-id"  # the View: line, never Share:
        clip_cmd = tools.calls[-1]
        assert clip_cmd[clip_cmd.index("--companion-url") + 1] == out["deck_url"]
        assert clip_cmd[clip_cmd.index("--narrative-url") + 1] == "https://canopy.test/review/n1/"
        assert "--public" in clip_cmd and clip_cmd[clip_cmd.index("--title") + 1] == "Supply walkthrough iter0 (video)"
        st = runstate.load(run.id, ddd_dir=run.ddd)
        assert st.iteration_decks[0] == out["deck_url"] and st.iteration_clips[0] == out["clip_url"]
        assert st.steps["render_check"]["ok"] is True

    def test_refuses_a_failed_render_and_uploads_nothing(self, run):
        self._rendered(run, rc=1)
        tools = FakeTools()
        out = iteration.publish(run.id, run.spec, run=tools)
        assert out["ok"] is False and "exited 1" in out["reason"]
        assert tools.calls == []
        assert runstate.load(run.id, ddd_dir=run.ddd).steps["render_check"]["ok"] is False

    def test_refuses_a_stale_render(self, run):
        self._rendered(run)
        (run.dir / ".render_start").write_text(f"{time.time() + 3600:.3f}\n")  # artifacts predate it
        out = iteration.publish(run.id, run.spec, run=FakeTools())
        assert out["ok"] is False and "stale" in out["reason"]

    def test_refuses_without_a_render_stamp(self, run):
        out = iteration.publish(run.id, run.spec, run=FakeTools())
        assert out["ok"] is False and "iteration render" in out["reason"]

    def test_an_upload_failure_is_logged_and_left_unset(self, run):
        self._rendered(run)
        out = iteration.publish(run.id, run.spec, run=FakeTools(fail={"clip"}))
        assert out["ok"] and out["deck_url"] and out["clip_url"] is None
        assert "HTTP 502" in (run.dir / "upload-errors.md").read_text()
        st = runstate.load(run.id, ddd_dir=run.ddd)
        assert 0 not in st.iteration_clips and st.iteration_decks[0] == out["deck_url"]

    def test_no_clip_means_deck_only(self, run):
        self._rendered(run, clip=False)
        tools = FakeTools()
        out = iteration.publish(run.id, run.spec, run=tools)
        assert out["clip_url"] is None and len(tools.calls) == 2  # generate + deck upload


def test_cli_passes_extra_recorder_args_after_double_dash(run, monkeypatch):
    seen = {}

    def fake_render(run_id, spec, **kw):
        seen.update(kw)
        return {"exit_code": 0, "status": "ok", "reason": "", "log": "x"}

    monkeypatch.setattr(iteration, "render", fake_render)
    assert iteration._main(["render", run.id, "--spec", str(run.spec), "--", "--full-page-snapshots"]) == 0
    assert seen["extra"] == ["--full-page-snapshots"]
