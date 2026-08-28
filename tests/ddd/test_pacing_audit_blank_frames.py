"""A solid-colour frame is a broken frame, and nothing was looking for it.

Dead-air is silent AND frozen. A blank frame with narration over it is neither,
so every other detector here is structurally blind to it.
`microplans-study-groups` shipped 2.5s of solid dark purple at 34.0s and this
audit reported "VIEWING ISSUES: none over threshold."
"""
from __future__ import annotations

import subprocess

import pytest

from scripts.ddd.render_pacing_audit import blank_intervals


@pytest.fixture(scope="module")
def ffmpeg_available():
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        pytest.skip("ffmpeg not available")


def _clip(path, source: str, seconds: float) -> str:
    # `color=c=…` already carries an `=`, so options are appended with `:`;
    # a bare source name like `testsrc2` needs the first `=`.
    sep = ":" if "=" in source else "="
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"{source}{sep}s=320x240:d={seconds}", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)
    return str(path)


def test_a_solid_colour_video_reads_as_blank(tmp_path, ffmpeg_available):
    v = _clip(tmp_path / "flat.mp4", "color=c=0x0a0819", 3)
    spans = blank_intervals(v, 3.0)
    assert spans, "a wholly flat video must register as blank"
    s, e = spans[0]
    assert s < 0.5 and e > 2.0


def test_real_content_is_not_blank(tmp_path, ffmpeg_available):
    v = _clip(tmp_path / "busy.mp4", "testsrc2", 3)
    assert blank_intervals(v, 3.0) == []


def test_a_saturated_flat_colour_is_still_blank(tmp_path, ffmpeg_available):
    """The bug the first cut of this check had: measuring spread across
    INTERLEAVED rgb counts the R/G/B offsets of a flat colour as variance, so a
    coloured blank (not pure black or grey) scored clean. Purple is exactly
    that case — and purple is what actually shipped."""
    v = _clip(tmp_path / "purple.mp4", "color=c=0x2a0a4f", 2)
    assert blank_intervals(v, 2.0), "a saturated flat colour must still read as blank"


def test_a_brief_flash_is_below_threshold(tmp_path, ffmpeg_available):
    """A single dropped frame is not a viewing issue; the span floor keeps the
    signal honest."""
    v = _clip(tmp_path / "blip.mp4", "color=c=black", 0.2)
    assert blank_intervals(v, 0.2) == []
