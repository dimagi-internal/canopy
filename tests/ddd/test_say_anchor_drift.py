"""canopy#911: `say:` anchors added to the spec AFTER recording were silently ignored.

Marks are built from the recorder report's actions, which froze the spec's
`say:` words at recording time. A hover with a css target (no `id_…` token) and
no note yields no candidate words at all, so a `say:` added afterwards was the
only thing that could bind it — and it never reached the marks. The emitter now
re-anchors from the current spec when the scene's actions still pair 1:1 with
the filmed ones, and refuses (naming the anchors, telling you to re-record) when
they do not.
"""
from __future__ import annotations

import copy
import json

import pytest
import yaml

from scripts.ddd.snippets import (
    SayAnchorDriftError,
    build_snippets,
    emit_explainer_from_capture,
    reconcile_say_anchors,
)

NARRATION = "Open the dispenser log, then check the refill date and the volume."


def _spec(scene_actions):
    return {
        "name": "chlorine",
        "scenes": [
            {"title": "Log", "narrative": NARRATION, "actions": scene_actions},
        ],
    }


# What the recorder filmed: the recipe as it was, with no `say:` anywhere.
REPORT = {
    "scenes": [{"scene_index": 1, "start_seconds": 0.0, "duration_seconds": 12.0}],
    "actions": [
        {"kind": "hover", "ok": True, "target": "css:.refill-cell", "note": None,
         "elapsed_ms": 300, "start_seconds": 2.0, "scene_index": 1, "say": None},
        {"kind": "hover", "ok": True, "target": "css:.volume-cell", "note": None,
         "elapsed_ms": 300, "start_seconds": 6.0, "scene_index": 1, "say": None},
    ],
}


def _marks(spec, report):
    snips = build_snippets(
        narrative_slug="chlorine", spec=spec, report=report,
        source_clip_local=None, source_clip_hosted=None,
    )
    return [m for s in snips for m in s["action_marks"]]


def test_say_added_after_recording_reaches_the_marks():
    """The bug: the spec now says `refill` / `volume`, the marks said nothing."""
    spec = _spec([
        {"kind": "hover", "target": "css:.refill-cell", "say": "refill"},
        {"kind": "hover", "target": "css:.volume-cell", "say": "volume"},
    ])
    marks = _marks(spec, REPORT)
    assert [m["say"] for m in marks] == ["refill", "volume"]
    assert [m["on_seconds"] for m in marks] == [2.0, 6.0]  # footage timing untouched


def test_a_changed_say_wins_over_the_recorded_one():
    report = copy.deepcopy(REPORT)
    report["actions"][0]["say"] = "date"
    spec = _spec([
        {"kind": "hover", "target": "css:.refill-cell", "say": "refill"},
        {"kind": "hover", "target": "css:.volume-cell"},
    ])
    marks = _marks(spec, report)
    assert [m.get("say") for m in marks] == ["refill"]


def test_the_report_passed_in_is_not_mutated():
    report = copy.deepcopy(REPORT)
    spec = _spec([
        {"kind": "hover", "target": "css:.refill-cell", "say": "refill"},
        {"kind": "hover", "target": "css:.volume-cell", "say": "volume"},
    ])
    reconcile_say_anchors(spec, report)
    assert report == REPORT


def test_templated_target_pairs_with_its_resolved_recording():
    report = copy.deepcopy(REPORT)
    report["actions"][0]["target"] = "css:#row-42 .refill-cell"
    spec = _spec([
        {"kind": "hover", "target": "css:#row-${row_id} .refill-cell", "say": "refill"},
        {"kind": "hover", "target": "css:.volume-cell"},
    ])
    new, notes = reconcile_say_anchors(spec, report)
    assert new["actions"][0]["say"] == "refill"
    assert notes and "scene 1" in notes[0]


def test_actions_changed_since_recording_refuses_and_names_the_anchors():
    """Can't tell which footage moment a word belongs to — refuse, don't guess."""
    spec = _spec([
        {"kind": "scroll_to", "target": "css:.table"},  # added after recording
        {"kind": "hover", "target": "css:.refill-cell", "say": "refill"},
        {"kind": "hover", "target": "css:.volume-cell", "say": "volume"},
    ])
    with pytest.raises(SayAnchorDriftError) as e:
        _marks(spec, REPORT)
    msg = str(e.value)
    assert "re-record" in msg and "refill" in msg and "volume" in msg and "scene 1" in msg


def test_changed_actions_with_unchanged_anchors_still_emit():
    """Drift that doesn't touch say: is not this check's business."""
    spec = _spec([
        {"kind": "scroll_to", "target": "css:.table"},
        {"kind": "hover", "target": "css:.refill-cell"},
        {"kind": "hover", "target": "css:.volume-cell"},
    ])
    _marks(spec, REPORT)


def test_a_lock_only_spec_with_no_actions_keeps_the_recorded_words():
    report = copy.deepcopy(REPORT)
    report["actions"][0]["say"] = "refill"
    spec = {"name": "chlorine", "scenes": [{"title": "Log", "narrative": NARRATION}]}
    assert [m.get("say") for m in _marks(spec, report)] == ["refill"]


def test_explainer_from_capture_carries_the_new_anchors(tmp_path):
    """End to end on the CLI path the issue used (`explainer-from-capture`)."""
    spec = _spec([
        {"kind": "hover", "target": "css:.refill-cell", "say": "refill"},
        {"kind": "hover", "target": "css:.volume-cell", "say": "volume"},
    ])
    (tmp_path / "chlorine.yaml").write_text(yaml.safe_dump(spec))
    (tmp_path / "report.json").write_text(json.dumps(REPORT))
    emit_explainer_from_capture(
        str(tmp_path / "chlorine.yaml"), str(tmp_path / "report.json"),
        out_path=str(tmp_path / "explainer_spec.yaml"),
    )
    doc = yaml.safe_load((tmp_path / "explainer_spec.yaml").read_text())
    marks = [m for beat in doc["walkthrough"].values() for m in beat.get("action_marks") or []]
    assert [m["say"] for m in marks] == ["refill", "volume"]
