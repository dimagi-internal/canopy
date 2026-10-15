"""``--snapshots`` must not leave its full-page capture in the film (canopy#912).

A full-page ``page.screenshot`` resizes the live viewport for the beyond-viewport
paint, and the video recorder films it: the master shows the page shrunk onto a
grey canvas for ~1.5 s at the scene boundary. ddd-video-judge flagged it critical
in cut after cut. The frame must come from THIS page's state, so the capture
stays on the filmed page — but the recorder now records its span in
``RunReport.capture_spans`` and the emitter cuts it out of every segment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ddd import snippets  # noqa: E402
from scripts.walkthrough._lib.orchestrator import Recorder  # noqa: E402
from scripts.walkthrough._lib.results import RunReport  # noqa: E402

from tests.walkthrough.test_record_video_snapshots import FakePage  # noqa: E402


def _scene(idx: int, **extra) -> dict:
    return {"title": f"S{idx}", "actions": [{"kind": "press", "value": "Enter"}],
            "scene_index": idx, **extra}


def test_full_page_snapshot_records_its_footage_span(tmp_path):
    rec = Recorder(snapshot_dir=tmp_path)
    rec.run_scene(FakePage(), _scene(3))
    spans = rec.report.capture_spans
    assert len(spans) == 1
    span = spans[0]
    assert span["scene_index"] == 3 and span["kind"] == "snapshot"
    # The span sits inside the scene's slot on the recording timeline.
    scene = rec.report.scene_timing_for(3)
    assert scene["start_seconds"] <= span["start_seconds"]
    assert span["start_seconds"] + span["duration_seconds"] <= (
        scene["start_seconds"] + scene["duration_seconds"] + 1e-3
    )
    assert rec.report.as_dict()["capture_spans"] == spans


def test_before_frame_full_page_capture_is_recorded_too(tmp_path):
    rec = Recorder(snapshot_dir=tmp_path, capture_action_frames=True)
    rec.run_scene(FakePage(), _scene(2, actions=[{"kind": "click", "target": "Save"}]))
    kinds = [s["kind"] for s in rec.report.capture_spans]
    assert kinds == ["before", "snapshot"]


def test_viewport_snapshot_records_no_span(tmp_path):
    """A viewport capture resizes nothing, so there is nothing to cut."""
    rec = Recorder(snapshot_dir=tmp_path)
    rec.run_scene(FakePage(), _scene(2, full_page=False))
    assert rec.report.capture_spans == []
    assert "capture_spans" not in rec.report.as_dict()


def test_no_snapshots_records_no_span(tmp_path):
    rec = Recorder()
    rec.run_scene(FakePage(), _scene(1))
    assert rec.report.capture_spans == []


def test_run_report_capture_span_round_trip():
    r = RunReport()
    r.record_capture_span(scene_index=4, start_seconds=12.34567, duration_seconds=1.5, kind="snapshot")
    assert r.as_dict()["capture_spans"] == [
        {"scene_index": 4, "start_seconds": 12.346, "duration_seconds": 1.5, "kind": "snapshot"}
    ]


def test_excise_capture_spans_cuts_the_whole_span_no_lead_in():
    out = snippets.excise_capture_spans(
        [(100.0, 20.0)],
        [{"scene_index": 1, "start_seconds": 118.0, "duration_seconds": 1.5}],
    )
    assert out == [(100.0, 18.0), (119.5, 0.5)]


def test_build_snippets_never_keeps_a_snapshot_span():
    """Scene 1 ends with a 1.5 s full-page capture; scene 2 opens with a before-
    frame. Neither may survive into the emitted segments — including in a teach
    scene, where nothing else is de-dwelled."""
    spec = {"scenes": [
        {"title": "A", "pace": "teach", "narrative": "a"},
        {"title": "B", "pace": "teach", "narrative": "b"},
    ]}
    report = {
        "scenes": [
            {"scene_index": 1, "start_seconds": 0.0, "duration_seconds": 20.0},
            {"scene_index": 2, "start_seconds": 20.0, "duration_seconds": 10.0},
        ],
        "capture_spans": [
            {"scene_index": 1, "start_seconds": 18.5, "duration_seconds": 1.5, "kind": "snapshot"},
            {"scene_index": 2, "start_seconds": 20.5, "duration_seconds": 1.2, "kind": "before"},
        ],
    }
    snips = snippets.build_snippets(
        narrative_slug="x", spec=spec, report=report,
        source_clip_local=None, source_clip_hosted=None,
    )
    assert snips[0]["segments"] == [{"start_seconds": 0.0, "duration_seconds": 18.5}]
    assert snips[1]["segments"] == [
        {"start_seconds": 20.0, "duration_seconds": 0.5},
        {"start_seconds": 21.7, "duration_seconds": 8.3},
    ]
    for snip in snips:
        for seg in snip["segments"]:
            s, e = seg["start_seconds"], seg["start_seconds"] + seg["duration_seconds"]
            for cs in report["capture_spans"]:
                cs_s = cs["start_seconds"]
                cs_e = cs_s + cs["duration_seconds"]
                assert e <= cs_s + 1e-6 or s >= cs_e - 1e-6, (seg, cs)
    assert snips[0]["duration_seconds"] == pytest.approx(18.5)


def test_scoped_take_merge_keeps_capture_spans():
    from scripts.walkthrough._lib.scene_merge import merge_report

    prior = {"capture_spans": [{"scene_index": 1, "start_seconds": 1.0, "duration_seconds": 1.0}]}
    new = {"capture_spans": [{"scene_index": 2, "start_seconds": 5.0, "duration_seconds": 1.0}]}
    merged = merge_report(prior, new, targets=[2])
    assert [c["scene_index"] for c in merged["capture_spans"]] == [1, 2]
