"""A video with no audio stream must not audit as 100% speech.

`silencedetect` emits nothing when there is no audio stream, which the caller
read as "no silence found" — so a video with NO narration scored 100% speech,
0.0s dead air, and zero viewing issues. `create-survey-solicitation v12`
shipped silent and this audit rated it the cleanest of its set, which is how a
silent video stayed on a shared page.
"""
from __future__ import annotations

import subprocess

import pytest

from scripts.ddd.render_pacing_audit import has_audio, silence_intervals


def _make(path, *, audio: bool, seconds: int = 3):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
           f"color=c=black:s=320x240:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    cmd += ["-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return str(path)


@pytest.fixture(scope="module")
def ffmpeg_available():
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        pytest.skip("ffmpeg not available")


def test_a_file_with_no_audio_stream_is_entirely_silent(tmp_path, ffmpeg_available):
    v = _make(tmp_path / "silent.mp4", audio=False)
    assert has_audio(v) is False
    assert silence_intervals(v, 3.0) == [(0.0, 3.0)]


def test_a_file_with_audio_is_probed_normally(tmp_path, ffmpeg_available):
    v = _make(tmp_path / "loud.mp4", audio=True)
    assert has_audio(v) is True
    # a continuous tone is never silent — the real point is that we did NOT
    # take the no-audio shortcut
    assert silence_intervals(v, 3.0) != [(0.0, 3.0)]
