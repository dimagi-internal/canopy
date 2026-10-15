"""ddd-video-judge must bind each scene to ITS OWN voiceover (canopy#910).

Recorded cuts share a required opener ("This is a quick overview of how we ..."),
so a prefix key handed every cut whichever sidecar was listed first.
"""

from __future__ import annotations

import json

import pytest

from scripts.ddd.video_judge import AmbiguousAlignmentError, find_alignment

OPENER = "This is a quick overview of how we "


def _sidecar(dirpath, name, text, start, voice="v1"):
    chars = list(text)
    payload = {
        "voice_id": voice,
        "text": text,
        "alignment": {
            "characters": chars,
            "character_start_times_seconds": [start + i * 0.05 for i in range(len(chars))],
            "character_end_times_seconds": [start + (i + 1) * 0.05 for i in range(len(chars))],
        },
    }
    (dirpath / name).write_text(json.dumps(payload))


def test_cuts_sharing_an_opener_each_get_their_own_audio(tmp_path):
    a = OPENER + "dispense chlorine at the water point."
    b = OPENER + "record a refill visit in the app."
    # "aaa" lists before "bbb", so a prefix key would hand B's scene A's audio.
    _sidecar(tmp_path, "aaa.json", a, start=0.0)
    _sidecar(tmp_path, "bbb.json", b, start=10.0)

    got_a = find_alignment(str(tmp_path), a)
    got_b = find_alignment(str(tmp_path), b)
    assert "".join(got_a["characters"]) == a
    assert "".join(got_b["characters"]) == b
    assert got_b["character_start_times_seconds"][0] == 10.0


def test_a_narration_with_no_clip_of_its_own_gets_none(tmp_path):
    _sidecar(tmp_path, "aaa.json", OPENER + "dispense chlorine.", start=0.0)
    assert find_alignment(str(tmp_path), OPENER + "something never synthesized.") is None


def test_whitespace_differences_still_match(tmp_path):
    text = OPENER + "dispense chlorine."
    _sidecar(tmp_path, "aaa.json", text, start=0.0)
    assert find_alignment(str(tmp_path), "  " + text.replace(" ", "  ") + "\n") is not None


def test_same_text_different_timings_fails_loudly(tmp_path):
    text = OPENER + "dispense chlorine."
    _sidecar(tmp_path, "aaa.json", text, start=0.0, voice="v1")
    _sidecar(tmp_path, "bbb.json", text, start=3.0, voice="v2")
    with pytest.raises(AmbiguousAlignmentError):
        find_alignment(str(tmp_path), text)


def test_identical_duplicates_are_not_ambiguous(tmp_path):
    text = OPENER + "dispense chlorine."
    _sidecar(tmp_path, "aaa.json", text, start=0.0)
    _sidecar(tmp_path, "bbb.json", text, start=0.0)
    assert find_alignment(str(tmp_path), text) is not None
