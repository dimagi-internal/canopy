"""Unit tests for the per-scene action trace the DDD dual-judge consumes.

The DDD dual-judge scores ONE still screenshot per scene. A scene that
actually filled+submitted a form and one that only HOVERED produce the same
end-frame — so without the action trace the judge cannot tell "the task was
performed" from "the task was merely claimed". These helpers extract that
trace from the recorder's run-report so the judge can apply action-fidelity
deductions:

  - ``action_trace_by_scene(report)`` groups ``actions`` by 1-based scene_index,
    keeping only the fields a judge reasons over (kind/target/ok/must_succeed/note)
  - ``scene_effecting_summary(trace)`` flags has_effecting / only_non_effecting /
    any_failed / any_required_failed for one scene's trace
  - both tolerate old reports (no actions / no scene_index) and never crash
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.results import (  # noqa: E402
    EFFECTING_ACTION_KINDS,
    ActionResult,
    RunReport,
    action_trace_by_scene,
    scene_effecting_summary,
)


def _report_with(actions: list[dict]) -> dict:
    """Build a run-report dict shaped like RunReport.as_dict()."""
    return {
        "total": len(actions),
        "ok": sum(1 for a in actions if a.get("ok", True)),
        "failed": sum(1 for a in actions if not a.get("ok", True)),
        "actions": actions,
        "scenes": [],
    }


# ---- action_trace_by_scene -------------------------------------------------


def test_groups_actions_by_scene_index():
    report = _report_with(
        [
            {"kind": "fill", "target": "Name", "ok": True, "scene_index": 1},
            {"kind": "click", "target": "Submit", "ok": True, "scene_index": 1},
            {"kind": "hover", "target": "Award", "ok": True, "scene_index": 2},
        ]
    )
    trace = action_trace_by_scene(report)
    assert set(trace) == {1, 2}
    assert [a["kind"] for a in trace[1]] == ["fill", "click"]
    assert [a["kind"] for a in trace[2]] == ["hover"]


def test_keeps_only_judge_relevant_fields():
    report = _report_with(
        [
            {
                "kind": "click",
                "target": "Award",
                "value": "secret",
                "note": "award the response",
                "ok": False,
                "must_succeed": True,
                "elapsed_ms": 9999,
                "error_message": "timed out waiting for selector",
                "scene_index": 3,
            }
        ]
    )
    entry = action_trace_by_scene(report)[3][0]
    assert entry == {
        "kind": "click",
        "target": "Award",
        "ok": False,
        "must_succeed": True,
        "note": "award the response",
        # `warning` is judge-relevant by construction: it is the ONLY channel
        # that reports an action which succeeded while doing less than the spec
        # asked (a scroll_to the browser clamped to a standstill). `ok` is true
        # in that case, so dropping it here would hide the one fact that
        # explains a duplicate frame. None when the action has nothing to add.
        "warning": None,
    }
    # noise dropped
    assert "value" not in entry
    assert "elapsed_ms" not in entry
    assert "error_message" not in entry


def test_skips_actions_without_scene_index():
    report = _report_with(
        [
            {"kind": "fill", "ok": True},  # direct execute_action test call — no scene
            {"kind": "fill", "ok": True, "scene_index": 1},
        ]
    )
    trace = action_trace_by_scene(report)
    assert set(trace) == {1}


def test_tolerates_old_or_empty_reports():
    assert action_trace_by_scene({}) == {}
    assert action_trace_by_scene({"total": 0, "actions": []}) == {}
    # malformed entries are skipped, not fatal
    assert action_trace_by_scene(
        {"actions": [{"kind": "fill", "scene_index": "bad"}, "junk", {"no": "idx"}]}
    ) == {}


def test_string_scene_index_coerced():
    report = _report_with([{"kind": "fill", "ok": True, "scene_index": "2"}])
    assert set(action_trace_by_scene(report)) == {2}


def test_roundtrips_through_real_run_report():
    rep = RunReport()
    rep.record(ActionResult(kind="hover", ok=True, target="Award", scene_index=1))
    rep.record(ActionResult(kind="click", ok=False, target="Submit",
                            must_succeed=True, scene_index=1))
    parsed = json.loads(rep.to_json())
    trace = action_trace_by_scene(parsed)
    assert [a["kind"] for a in trace[1]] == ["hover", "click"]
    assert trace[1][1]["ok"] is False
    assert trace[1][1]["must_succeed"] is True


# ---- scene_effecting_summary -----------------------------------------------


def test_summary_flags_effecting_scene():
    trace = [
        {"kind": "fill", "ok": True, "must_succeed": False},
        {"kind": "click", "ok": True, "must_succeed": False},
    ]
    s = scene_effecting_summary(trace)
    assert s["has_effecting"] is True
    assert s["only_non_effecting"] is False
    assert s["any_failed"] is False
    assert s["any_required_failed"] is False
    assert s["kinds"] == ["click", "fill"]


def test_summary_flags_hover_only_scene():
    """The core diagnosis: a scene that only hovers/scrolls effected nothing."""
    trace = [
        {"kind": "hover", "ok": True, "must_succeed": False},
        {"kind": "scroll_to", "ok": True, "must_succeed": False},
        {"kind": "wait_for", "ok": True, "must_succeed": False},
    ]
    s = scene_effecting_summary(trace)
    assert s["has_effecting"] is False
    assert s["only_non_effecting"] is True


def test_summary_flags_failed_action():
    trace = [{"kind": "click", "ok": False, "must_succeed": False}]
    s = scene_effecting_summary(trace)
    assert s["any_failed"] is True
    assert s["any_required_failed"] is False


def test_summary_flags_required_failure():
    """The award click that silently timed out with must_succeed."""
    trace = [{"kind": "click", "ok": False, "must_succeed": True}]
    s = scene_effecting_summary(trace)
    assert s["any_failed"] is True
    assert s["any_required_failed"] is True


def test_summary_empty_trace_is_all_false():
    """A narrative-only scene (no actions) carries no action claim to deduct."""
    s = scene_effecting_summary([])
    assert s == {
        "has_effecting": False,
        "only_non_effecting": False,
        "any_failed": False,
        "any_required_failed": False,
        "kinds": [],
    }


def test_goto_alone_is_not_effecting():
    """goto navigates but does not, on its own, effect a form action."""
    trace = [{"kind": "goto", "ok": True, "must_succeed": False}]
    s = scene_effecting_summary(trace)
    assert s["has_effecting"] is False
    assert s["only_non_effecting"] is True


def test_effecting_kinds_membership():
    assert "fill" in EFFECTING_ACTION_KINDS
    assert "click" in EFFECTING_ACTION_KINDS
    assert "select" in EFFECTING_ACTION_KINDS
    assert "type" in EFFECTING_ACTION_KINDS
    assert "hover" not in EFFECTING_ACTION_KINDS
    assert "scroll_to" not in EFFECTING_ACTION_KINDS
    assert "goto" not in EFFECTING_ACTION_KINDS
