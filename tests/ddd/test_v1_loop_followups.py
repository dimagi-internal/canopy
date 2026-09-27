"""Follow-ups from the first live v1/backlog DDD run (connect-labs
``supply-sophie-rutf-2026-09-26-001``, canopy 0.2.528, loop-metrics M1–M14).

One class per item; M1 lives in ``test_runstate.py``, M9/M11/M14 in
``test_state_mutating_reuse.py``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ddd import gap_walk, spec_io, version_skew
from scripts.walkthrough._lib import scene_hooks
from scripts.walkthrough._lib.scene_hooks import SceneHookError


# ---------------------------------------------------------------------------
# M3 — per-scene before: hooks
# ---------------------------------------------------------------------------


class _Done:
    def __init__(self, code: int) -> None:
        self.returncode = code


class TestSceneHooks:
    def test_shorthand_and_mapping_normalise(self) -> None:
        assert scene_hooks.normalize_hook("echo hi") == {"command": "echo hi", "timeout_seconds": 300}
        assert scene_hooks.normalize_hook({"command": "x", "timeout_seconds": 5})["timeout_seconds"] == 5
        assert scene_hooks.normalize_hook(None) is None
        with pytest.raises(SceneHookError):
            scene_hooks.normalize_hook({"command": "  "})

    def test_vars_resolve_late_and_the_command_runs_from_cwd(self, tmp_path: Path) -> None:
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw["cwd"]))
            return _Done(0)

        out = scene_hooks.run_scene_hook(
            "answer --tender ${tid}",
            scene_index=5,
            variables={"tid": 94},
            cwd=tmp_path,
            resolve=lambda text, v: text.replace("${tid}", str(v["tid"])),
            runner=runner,
        )
        assert calls == [("answer --tender 94", str(tmp_path))]
        assert out["scene_index"] == 5 and out["exit_code"] == 0

    def test_an_unresolved_placeholder_never_runs(self) -> None:
        ran = []
        with pytest.raises(SceneHookError, match="unresolved"):
            scene_hooks.run_scene_hook(
                "answer ${tid}", scene_index=5, variables={}, runner=lambda *a, **k: ran.append(1)
            )
        assert ran == []

    def test_a_failing_hook_aborts(self) -> None:
        with pytest.raises(SceneHookError, match="exit 3"):
            scene_hooks.run_scene_hook("x", scene_index=2, runner=lambda *a, **k: _Done(3))

    def test_the_recorder_runs_it_records_it_and_marks_the_pause(self, tmp_path: Path) -> None:
        from scripts.walkthrough._lib.orchestrator import Recorder

        rec = Recorder(hook_cwd=tmp_path)
        rec.recording_epoch = 0.0
        rec.run_before_hook({"before": "echo answered > marker.txt"}, 4)
        assert (tmp_path / "marker.txt").read_text().strip() == "answered"
        assert [h["scene_index"] for h in rec.report.scene_hooks] == [4]
        assert rec.report.load_waits[-1]["target"] == "before-hook"
        assert "scene_hooks" in rec.report.as_dict()
        with pytest.raises(SceneHookError):
            rec.run_before_hook({"before": "exit 1"}, 5)

    def test_run_scene_fires_the_hook_before_the_scenes_nav(self, tmp_path: Path, monkeypatch) -> None:
        from scripts.walkthrough._lib import orchestrator
        from scripts.walkthrough._lib.orchestrator import Recorder
        from scripts.walkthrough._lib.results import ActionResult

        order: list[str] = []

        class Page:
            url = "https://x/prev"

            def wait_for_timeout(self, ms):
                pass

            def wait_for_load_state(self, *a, **k):
                pass

            def goto(self, url, **kw):
                order.append("goto:" + ("hooked" if (tmp_path / "m").exists() else "unhooked"))
                self.url = url

            def screenshot(self, **kw):
                pass

            def evaluate(self, *a):
                return ""

        monkeypatch.setattr(
            orchestrator, "execute_action", lambda *a, **k: ActionResult(kind="hover", ok=True)
        )
        rec = Recorder(hook_cwd=tmp_path, base_url="https://x")
        rec.run_scene(
            Page(),
            {"title": "s", "scene_index": 2, "url": "/next", "before": "touch m",
             "actions": [{"kind": "hover", "target": "x"}]},
        )
        assert order == ["goto:hooked"]
        assert rec.report.scene_hooks[0]["scene_index"] == 2

    def test_no_hook_is_a_no_op(self) -> None:
        from scripts.walkthrough._lib.orchestrator import Recorder

        rec = Recorder()
        assert rec.run_before_hook({"title": "x"}, 1) is None
        assert "scene_hooks" not in rec.report.as_dict()

    def test_the_render_keeps_the_field_on_the_scene(self) -> None:
        from scripts.walkthrough.record_video import build_scenes_from_spec

        spec = {"scenes": [{"title": "A", "url": "/a"}, {"title": "B", "before": "echo b"}]}
        scenes = build_scenes_from_spec(spec, "https://x", run_data=None)
        assert [s.get("before") for s in scenes] == [None, "echo b"]

    def test_the_schema_accepts_both_shapes(self) -> None:
        from scripts.narrative.models import Scene

        base = {"persona": "p", "title": "t", "show": "s", "concept_claim": "c", "provenance": "x"}
        assert Scene.model_validate({**base, "before": "echo"}).before == "echo"
        assert Scene.model_validate({**base, "before": {"command": "echo"}}).before.command == "echo"

    def test_preflight_treats_a_hook_as_mutating(self) -> None:
        from scripts.ddd.recipe_preflight import scenes_mutate

        assert scenes_mutate([{"actions": [{"kind": "hover", "target": "x"}]}]) is False
        assert scenes_mutate([{"before": "echo", "actions": []}]) is True


# ---------------------------------------------------------------------------
# M4 — storage-state personas
# ---------------------------------------------------------------------------


class TestStorageStatePersonas:
    def _spec(self, path: str) -> dict:
        return {
            "auth": {"type": "storage_state", "personas": {"sophie": path}},
            "scenes": [{"persona": "sophie"}, {"persona": "unmapped"}],
        }

    def test_cookies_load_relative_to_the_setup_cwd(self, tmp_path: Path) -> None:
        from scripts.walkthrough.identities import mint_identities

        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "sophie.json").write_text(
            json.dumps({"cookies": [{"name": "sessionid", "value": "v", "domain": "x"}], "origins": []})
        )
        # No browser needed — nothing is signed in on a page.
        got = mint_identities(None, self._spec("state/sophie.json"), "https://x", base_dir=tmp_path)
        assert list(got) == ["sophie"] and got["sophie"][0]["name"] == "sessionid"

    def test_a_missing_or_empty_state_refuses(self, tmp_path: Path) -> None:
        from scripts.walkthrough.identities import IdentityError, mint_identities

        with pytest.raises(IdentityError, match="not found"):
            mint_identities(None, self._spec("nope.json"), "https://x", base_dir=tmp_path)
        (tmp_path / "empty.json").write_text(json.dumps({"cookies": []}))
        with pytest.raises(IdentityError, match="no cookies"):
            mint_identities(None, self._spec("empty.json"), "https://x", base_dir=tmp_path)


# ---------------------------------------------------------------------------
# M2 — gap walk accuracy / strategy split
# ---------------------------------------------------------------------------


class TestGapWalkAccuracySplit:
    def _doc(self, gap: dict) -> dict:
        return {"covered": [], "gaps": [gap]}

    def _gap(self, **kw) -> dict:
        return {
            "scene": 1,
            "claim": "Hauwa awards the quote",
            "missing_capability": "no award route",
            "evidence": ["urls.py"],
            "kind": "decision",
            **kw,
        }

    def test_a_decision_whose_hint_offers_a_restatement_does_not_open_the_gate(self) -> None:
        g = self._gap(
            build_hint="Choose one: restore the label, or change scene 1's narration to the heading as built"
        )
        out = gap_walk.decide(self._doc(g))
        assert out["action"] == "build" and out["restate"][0]["scene"] == 1

    def test_explicit_accuracy_is_restated_and_explicit_strategy_decides(self) -> None:
        assert gap_walk.decide(self._doc(self._gap(finding_class="accuracy")))["action"] == "build"
        g = self._gap(finding_class="strategy", build_hint="reword the claim")
        assert gap_walk.decide(self._doc(g))["action"] == "decide"

    def test_a_real_product_decision_still_decides(self) -> None:
        g = self._gap(build_hint="Decide whether awards need a second approver")
        assert gap_walk.decide(self._doc(g))["action"] == "decide"

    def test_restate_is_a_valid_kind(self) -> None:
        assert gap_walk.validate(self._doc(self._gap(kind="restate")), scene_count=1) == []
        assert gap_walk.decide(self._doc(self._gap(kind="restate")))["action"] == "build"


# ---------------------------------------------------------------------------
# M8 + M12 — composed copies carry the canonical why-brief
# ---------------------------------------------------------------------------


class TestComposedSpec:
    def test_a_composed_copy_elsewhere_still_resolves_its_why_brief(self, tmp_path: Path) -> None:
        src = tmp_path / "repo" / "docs" / "walkthroughs"
        src.mkdir(parents=True)
        (src / "demo.why_brief.yaml").write_text("problem: p\n")
        (src / "demo.yaml").write_text(
            yaml.safe_dump({"name": "demo", "why_brief": "demo.why_brief.yaml", "scenes": []})
        )
        out = tmp_path / "run" / "unified_spec.yaml"
        res = spec_io.write_composed(src / "demo.yaml", out)
        composed = yaml.safe_load(out.read_text())
        wb = Path(composed["why_brief"])
        assert wb.is_absolute() and wb.exists()
        assert res["why_brief"] == str((src / "demo.why_brief.yaml").resolve())
        # validate() joins spec dir + why_brief — an absolute path survives it
        assert (out.parent / composed["why_brief"]).exists()

    def test_the_spec_adjacent_brief_is_canonical_when_none_is_declared(self, tmp_path: Path) -> None:
        (tmp_path / "demo.why_brief.yaml").write_text("problem: p\n")
        (tmp_path / "demo.yaml").write_text(yaml.safe_dump({"name": "demo", "scenes": []}))
        assert spec_io.resolve_why_brief(tmp_path / "demo.yaml") == (tmp_path / "demo.why_brief.yaml").resolve()


# ---------------------------------------------------------------------------
# M7 — skill text vs runtime version
# ---------------------------------------------------------------------------


class TestVersionSkew:
    def test_skew_between_skill_cache_and_runtime_warns(self, tmp_path: Path) -> None:
        (tmp_path / "VERSION").write_text("0.2.528\n")
        skill = "/Users/x/.claude/plugins/cache/canopy/canopy/0.2.524/skills/ddd-arc-eval"
        out = version_skew.check(skill, tmp_path)
        assert out["skew"] and "0.2.524" in out["message"] and "0.2.528" in out["message"]
        assert version_skew._main(["--skill-dir", skill, "--runtime-root", str(tmp_path)]) == 1

    def test_same_version_or_unknown_is_quiet(self, tmp_path: Path) -> None:
        (tmp_path / "VERSION").write_text("0.2.528\n")
        assert not version_skew.check("/c/canopy/0.2.528/skills/x", tmp_path)["skew"]
        assert not version_skew.check("/dev/checkout/plugins/canopy/skills/x", tmp_path)["skew"]

    def test_an_installed_bundle_reads_its_plugin_manifest(self, tmp_path: Path) -> None:
        plugin = tmp_path / "0.2.530"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "0.2.530"}))
        (plugin / "runtime").mkdir()
        assert version_skew.runtime_version(plugin / "runtime") == "0.2.530"


# ---------------------------------------------------------------------------
# M6 — silent iteration clips
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frozen_silent_clip(tmp_path_factory) -> str:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")
    p = tmp_path_factory.mktemp("clip") / "iter.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=320x240:d=8",
         "-pix_fmt", "yuv420p", str(p)],
        check=True, capture_output=True,
    )
    return str(p)


class TestSilentIterationClip:
    def test_default_audit_stays_strict(self, frozen_silent_clip: str) -> None:
        from scripts.ddd.render_pacing_audit import audit

        a = audit(frozen_silent_clip)
        assert not a["silence_flags_skipped"]

    def test_no_audio_expected_skips_the_silence_flags_only(self, frozen_silent_clip: str) -> None:
        from scripts.ddd.render_pacing_audit import audit, render

        a = audit(frozen_silent_clip, audio_expected=False)
        assert a["silence_flags_skipped"]
        assert not [f for f in a["flags"] if f[0] in ("DEAD-AIR", "SILENT-MOTION")]
        assert "not scored" in render(a)


# ---------------------------------------------------------------------------
# M13 — scroll: top waits for the scroll to land
# ---------------------------------------------------------------------------


class TestScrollSettle:
    def test_scroll_top_waits_until_the_page_stops_moving(self) -> None:
        from scripts.walkthrough._lib import recorder

        class Page:
            def __init__(self) -> None:
                self.evaluated: list[str] = []

            def evaluate(self, js, *args):
                self.evaluated.append(js)
                return 0

            def wait_for_timeout(self, ms):
                pass

        page = Page()
        recorder.scroll_page(page, "top")
        assert page.evaluated[-1] == recorder._SCROLL_SETTLE_JS

    def test_settle_is_best_effort(self) -> None:
        from scripts.walkthrough._lib.recorder import wait_scroll_settled

        class Broken:
            def evaluate(self, *a):
                raise RuntimeError("page gone")

        assert wait_scroll_settled(Broken()) is None


# ---------------------------------------------------------------------------
# M5 — the canopy runtime is not an agent repo
# ---------------------------------------------------------------------------


class TestRuntimeIsNotAnAgent:
    def test_running_inside_the_canopy_plugin_does_not_claim_an_agent_identity(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from orchestrator import canopy_web

        plugin = tmp_path / "canopy" / "0.2.530"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "canopy"}))
        runtime = plugin / "runtime"
        runtime.mkdir()
        tok = tmp_path / "token"
        tok.write_text("operator-token")
        monkeypatch.setattr(canopy_web, "TOKEN_FILE", tok)
        monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
        monkeypatch.chdir(runtime)
        canopy_web._reset_identity_warning()
        assert canopy_web.resolve_token(None) == "operator-token"
        assert "WARNING" not in capsys.readouterr().err
