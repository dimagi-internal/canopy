"""`say:` is the authored half of action↔word sync — and the schema forbade it.

`scripts.ddd.snippets` reads `say` (and the older `word`) in four places to
anchor a field's moment on the narration word it should land on. Neither was
declared on `_ActionBase`, which is `extra="forbid"` — so every VALIDATING path
(`load_spec`, `narrative post`, `recipe_preflight`, `spec_qa`) rejected specs
that used the feature, while the non-validating loader took them fine. 47 uses
across the live recipes; the failure only surfaced when posting a narrative.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from scripts.ddd.snippets import _mark_words
from scripts.narrative.models import ClickAction, ScrollToAction


def test_say_survives_validation():
    a = ClickAction(kind="click", target="css:#go", say="generate")
    assert a.say == "generate"


def test_word_the_older_spelling_survives_too():
    a = ScrollToAction(kind="scroll_to", target="css:#x", word="scale")
    assert a.word == "scale"


def test_a_validated_action_still_binds_its_word():
    """The point of declaring it: the hint has to reach the mark builder."""
    a = ClickAction(kind="click", target="css:#id_status", say="status")
    assert _mark_words(a.model_dump()) [0] == "status"


def test_a_genuine_typo_is_still_rejected():
    """extra=forbid must keep doing its job — this is not a blanket opening."""
    with pytest.raises(ValidationError):
        ClickAction(kind="click", target="css:#go", sey="generate")
