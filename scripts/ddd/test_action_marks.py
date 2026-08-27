"""Tests for action↔word marks (onscreen_for_abs, _mark_words, build_action_marks).

Pure-logic, stdlib-only. Run: python3 -m pytest scripts/ddd/test_action_marks.py
or directly: python3 scripts/ddd/test_action_marks.py
"""

from scripts.ddd.snippets import _mark_words, build_action_marks, onscreen_for_abs


def test_onscreen_single_segment():
    segs = [(10.0, 20.0)]  # master [10,30] → on-screen [0,20]
    assert onscreen_for_abs(segs, 10.0) == 0.0
    assert onscreen_for_abs(segs, 15.0) == 5.0
    assert onscreen_for_abs(segs, 30.0) == 20.0
    assert onscreen_for_abs(segs, 5.0) == 0.0   # before start → clamp 0
    assert onscreen_for_abs(segs, 99.0) == 20.0  # past end → clamp total


def test_onscreen_multi_segment_with_excised_gap():
    # Two kept segments with master gap [25,40] excised (a collapsed load wait).
    segs = [(10.0, 15.0), (40.0, 10.0)]  # on-screen: seg1 [0,15], seg2 [15,25]
    assert onscreen_for_abs(segs, 20.0) == 10.0   # inside seg1
    assert onscreen_for_abs(segs, 25.0) == 15.0   # seg1 end
    assert onscreen_for_abs(segs, 32.0) == 15.0   # INSIDE the excised gap → jump-cut point
    assert onscreen_for_abs(segs, 45.0) == 20.0   # 5s into seg2 → 15+5
    assert onscreen_for_abs(segs, 50.0) == 25.0   # seg2 end


def test_mark_words_order_and_sources():
    # explicit word wins, then field-id tokens, then note tokens; deduped.
    a = {"target": "css:#id_contact_email", "note": "her contact", "word": "reach"}
    assert _mark_words(a) == ["reach", "contact", "email"]
    # no explicit; field id tokens then note (>=4 chars), 'id'/'css' dropped (len<=2 / not matched)
    b = {"target": "css:#id_description", "note": "survey description"}
    assert _mark_words(b) == ["description", "survey"]
    # short id tokens (<=2 chars) dropped
    c = {"target": "css:#id_is_public", "note": ""}
    assert _mark_words(c) == ["public"]


def test_build_action_marks_filters_and_maps():
    segs = [(0.0, 30.0)]  # on-screen == master here
    actions = [
        {"kind": "scroll_to", "target": "css:#id_description", "note": "", "start_seconds": 4.0, "scene_index": 3},
        {"kind": "fill", "target": "css:#id_description", "note": "", "start_seconds": 6.0, "scene_index": 3},
        {"kind": "wait_for", "target": "css:.spinner", "note": "", "start_seconds": 8.0, "scene_index": 3},  # not a field kind
        {"kind": "hold", "seconds": 2, "start_seconds": 9.0, "scene_index": 3},  # skipped
        {"kind": "select", "target": "css:#id_status", "value": "active", "note": "set Status to Active", "start_seconds": 12.0, "scene_index": 3},
        {"kind": "fill", "target": "css:#id_contact_email", "note": "her contact", "start_seconds": 20.0, "scene_index": 3},
        {"kind": "scroll_to", "target": "css:#id_x", "note": "", "start_seconds": 99.0, "scene_index": 3},  # no words (id_x → 'x' len<=2 dropped) → skipped
    ]
    marks = build_action_marks(actions, segs)
    kinds = [(m["kind"], m["on_seconds"], m["words"][0]) for m in marks]
    assert kinds == [
        ("scroll_to", 4.0, "description"),
        ("fill", 6.0, "description"),
        ("select", 12.0, "status"),
        ("fill", 20.0, "contact"),
    ]


def test_build_action_marks_skips_when_no_timestamp():
    segs = [(0.0, 10.0)]
    actions = [{"kind": "fill", "target": "css:#id_description", "scene_index": 1}]  # no start_seconds
    assert build_action_marks(actions, segs) == []


if __name__ == "__main__":
    import sys

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


def _act(kind, target, ts, note="", ms=0):
    return {"kind": kind, "target": target, "start_seconds": ts, "note": note, "elapsed_ms": ms}


def test_scroll_then_fill_on_one_field_is_one_mark():
    """The bug this collapse exists for: a spec that scrolls a field into view
    and then fills it emitted TWO marks with the same field-id words at the same
    instant, so the warp bound one and discarded its twin as an inversion."""
    segs = [(0.0, 60.0)]
    marks = build_action_marks(
        [
            _act("scroll_to", "css:#id_application_deadline", 21.0, "application deadline", ms=520),
            _act("fill", "css:#id_application_deadline", 21.52, "set the deadline"),
        ],
        segs,
    )
    assert len(marks) == 1
    assert marks[0]["kind"] == "fill", "the acting mark is when the value appears"
    assert "deadline" in marks[0]["words"]


def test_camera_move_yields_to_hover_on_the_same_target():
    segs = [(0.0, 60.0)]
    marks = build_action_marks(
        [
            _act("scroll_to", "css:#sol-coverage-map", 5.4, "the coverage map", ms=490),
            _act("hover", "css:#sol-coverage-map", 5.90, "glide over coverage"),
        ],
        segs,
    )
    assert [m["kind"] for m in marks] == ["hover"]


def test_two_acting_marks_on_one_target_are_both_kept():
    """Two fills on the same field are genuinely two moments — a correction, or a
    field revisited. Only a camera move is redundant."""
    segs = [(0.0, 60.0)]
    marks = build_action_marks(
        [
            _act("fill", "css:#id_scope_of_work", 12.0, "scope"),
            _act("fill", "css:#id_scope_of_work", 12.3, "scope, corrected"),
        ],
        segs,
    )
    assert len(marks) == 2


def test_same_target_far_apart_is_not_collapsed():
    """Add Question is scrolled to, then clicked over a second later — two real
    beats, not one."""
    segs = [(0.0, 60.0)]
    marks = build_action_marks(
        [
            _act("scroll_to", "text:Add Question", 28.4, "the application questions section", ms=280),
            _act("click", "text:Add Question", 29.92, "add an application question"),
        ],
        segs,
    )
    assert len(marks) == 2


def test_collapse_preserves_onscreen_order():
    segs = [(0.0, 60.0)]
    marks = build_action_marks(
        [
            _act("scroll_to", "css:#id_estimated_scale", 24.0, "estimated scale", ms=130),
            _act("fill", "css:#id_estimated_scale", 24.13, "the scale"),
            _act("fill", "css:#id_contact_email", 26.41, "her contact email"),
        ],
        segs,
    )
    assert [m["target"] for m in marks] == ["css:#id_estimated_scale", "css:#id_contact_email"]
    assert marks[0]["on_seconds"] <= marks[1]["on_seconds"]


def test_say_hint_wins_over_field_id_and_note_tokens():
    """The eval's remediation text tells authors to add a `say:` hint, so the
    hint has to beat the tokens scraped from the field id and the note — those
    are what collide across marks in the first place."""
    words = _mark_words(
        {
            "kind": "fill",
            "say": "deadline",
            "target": "css:#id_application_deadline",
            "note": "set the application deadline for the call",
        }
    )
    assert words[0] == "deadline"


def test_say_survives_the_recorder_into_the_report():
    """The report is the ONLY thing snippets sees. A `say:` that stops at the
    spec is a hint that silently does nothing."""
    from scripts.walkthrough._lib.results import ActionResult

    r = ActionResult(kind="fill", ok=True, target="css:#id_estimated_scale", say="scale")
    assert r.say == "scale"
    from dataclasses import asdict

    assert asdict(r)["say"] == "scale", "must serialize into the run report"


def _mk(say, word, at):
    return {"on_seconds": at, "words": [word], "target": f"#{word}", "kind": "fill", "say": say}


def test_lint_flags_a_say_hint_that_never_appears_in_the_narration():
    """The silent failure: the hint looks applied, the mark quietly falls back
    to field-id tokens, and you only find out after a full render."""
    from scripts.ddd.snippets import lint_narration_binding

    out = lint_narration_binding(3, "She sets the deadline and the scale.", [
        _mk("deadline", "deadline", 1.0), _mk("email", "email", 2.0),
    ])
    assert any("never appear" in line and "email" in line for line in out)


def test_lint_flags_narration_ordered_differently_than_the_footage():
    from scripts.ddd.snippets import lint_narration_binding

    out = lint_narration_binding(3, "She sets the scale, then the deadline.", [
        _mk("deadline", "deadline", 1.0), _mk("scale", "scale", 2.0),
    ])
    assert any("different order" in line for line in out)


def test_lint_flags_one_word_naming_two_fields():
    from scripts.ddd.snippets import lint_narration_binding

    out = lint_narration_binding(3, "She writes the date and the date.", [
        _mk("date", "date", 1.0), _mk("date", "date", 2.0),
    ])
    assert any("more than one" in line for line in out)


def test_lint_is_silent_when_the_binding_is_clean():
    from scripts.ddd.snippets import lint_narration_binding

    out = lint_narration_binding(3, "She sets the deadline, then the scale.", [
        _mk("deadline", "deadline", 1.0), _mk("scale", "scale", 2.0),
    ])
    assert out == []


def test_lint_says_nothing_without_hints():
    """Specs that predate `say:` must not start emitting noise."""
    from scripts.ddd.snippets import lint_narration_binding

    marks = [{"on_seconds": 1.0, "words": ["deadline"], "target": "#x", "kind": "fill"}]
    assert lint_narration_binding(3, "Anything at all.", marks) == []


def test_capture_health_names_the_failed_interactive_actions():
    """The three-week bug: a hero video built from a run where the form never
    filled and the call never published."""
    from scripts.ddd.snippets import lint_capture_health

    report = {"actions": [
        {"kind": "fill", "ok": False, "target": "css:#id_application_deadline",
         "error_message": "Malformed value"},
        {"kind": "fill", "ok": False, "target": 'css:input[x-model="question.text"]',
         "error_kind": "target_not_found"},
        {"kind": "hold", "ok": True},
    ]}
    out = lint_capture_health(report)
    assert any("2 of 3 actions failed" in line for line in out)
    assert any("id_application_deadline" in line for line in out)
    assert any("re-record" in line for line in out)


def test_capture_health_is_silent_on_a_clean_run():
    from scripts.ddd.snippets import lint_capture_health

    assert lint_capture_health({"actions": [{"kind": "click", "ok": True}]}) == []


def test_a_failed_camera_move_is_counted_but_not_shouted_about():
    """A missed scroll_to is a camera miss, not the app failing."""
    from scripts.ddd.snippets import lint_capture_health

    out = lint_capture_health({"actions": [
        {"kind": "scroll_to", "ok": False, "target": "css:th"}, {"kind": "click", "ok": True},
    ]})
    assert any("1 of 2 actions failed" in line for line in out)
    assert not any("re-record" in line for line in out)
