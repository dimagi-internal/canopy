"""A scene's narrative may be a LIST of beats, and prose readers see it joined.

Background: ``emit_explainer_from_capture`` has consumed a list since beats
existed — a list splits one scene into that many beats, which is how a scene
that legitimately demos more than one sentence's worth of work escapes the
action↔word warp's rate cap. Its own pacing lint recommended exactly that.

But ``Scene.narrative`` was typed ``str``, so any spec carrying a list failed
validation with ``string_type`` before it ever reached the emitter. The advice
was unreachable. These tests pin that a list now validates, that prose readers
get it joined, and that the plain-string shape is untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.narrative.models import Scene, scene_narration_text  # noqa: E402


BASE = dict(
    id="s3",
    persona="Maya",
    title="Maya builds the call",
    show="she fills the form",
    concept_claim="the call carries the study's coverage forward",
    provenance="live",
)


def test_plain_string_narrative_still_validates():
    assert Scene(**BASE, narrative="One sentence.").narrative == "One sentence."


def test_list_narrative_validates_and_is_preserved():
    scene = Scene(**BASE, narrative=["First beat.", "Second beat."])
    assert scene.narrative == ["First beat.", "Second beat."]


def test_default_is_still_empty_string():
    assert Scene(**BASE).narrative == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["First beat.", "Second beat."], "First beat. Second beat."),
        ("Just the one.", "Just the one."),
        (["  padded  ", "", "   ", "kept"], "padded kept"),
        ("", ""),
        (None, ""),
        ([], ""),
    ],
)
def test_narration_text_joins_either_shape(value, expected):
    assert scene_narration_text(value) == expected


def test_spec_qa_reads_a_list_as_prose_not_a_repr():
    """The QA verb-check f-string used to stringify a list as "['a', 'b']"."""
    text = scene_narration_text(["She clicks Generate.", "She approves it."])
    assert "[" not in text and "'" not in text
    assert text == "She clicks Generate. She approves it."
