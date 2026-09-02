"""A scene that captured the picture the last one already captured.

Drawn from two consecutive iterations of oes-partner-pipeline. Scene 2's
`scroll_to` targeted an element that was already in the viewport, so it scrolled
zero pixels and captured scene 1's frame again. The "fix" in the next iteration
retargeted it at the card heading — also already in the viewport — and was the
same no-op. Both times an LLM arc judge found it by pixel-diffing screenshots by
hand; nothing mechanical looked.
"""
from __future__ import annotations

import pytest

from scripts.ddd.duplicate_frames import check

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


def _write(run_dir, index, fill=0, band=None):
    snaps = run_dir / "snapshots"
    snaps.mkdir(exist_ok=True)
    arr = np.full((180, 320, 3), fill, dtype=np.uint8)
    if band is not None:
        arr[band[0] : band[1], :, :] = 255
    Image.fromarray(arr).save(snaps / f"scene_{index}.png")


def test_two_identical_frames_in_a_row_fail(tmp_path):
    _write(tmp_path, 1, fill=40)
    _write(tmp_path, 2, fill=40)
    result = check(tmp_path)
    assert result["verdict"] == "fail"
    assert result["pairs"][0]["scenes"] == [1, 2]
    assert "same picture" in result["reason"]


def test_a_moved_cursor_is_not_a_duplicate_scene_but_is_still_the_same_picture(tmp_path):
    """~0.4% of pixels — a cursor. Below threshold, so still flagged."""
    _write(tmp_path, 1, fill=40)
    _write(tmp_path, 2, fill=40, band=(0, 1))  # ~0.55% of rows
    assert check(tmp_path)["verdict"] == "fail"


def test_a_real_scroll_passes(tmp_path):
    _write(tmp_path, 1, fill=40)
    _write(tmp_path, 2, fill=40, band=(0, 90))  # half the frame changed
    result = check(tmp_path)
    assert result["verdict"] == "pass"
    assert result["pairs"][0]["difference"] > 0.02


def test_a_non_adjacent_duplicate_is_caught(tmp_path):
    """The regression this module was widened for.

    This assertion used to read the other way — "two similar frames far apart is
    a callback; two in a row is a stall" — and that premise is what let two real
    runs ship. `hh-poverty-targeting-answer-quality-2026-08-29-001` has scenes 3
    and 5 byte-identical while every ADJACENT pair in it differs by more than
    27%, so the consecutive-only gate reported `pass` on a run with two
    identical pictures in it. A camera that cannot move films the screen it was
    last left on, and nothing constrains that screen to be the previous scene's.
    """
    _write(tmp_path, 1, fill=40)
    _write(tmp_path, 2, fill=200)
    _write(tmp_path, 3, fill=40)  # same as scene 1, two apart
    result = check(tmp_path)
    assert result["verdict"] == "fail"
    assert [1, 3] in [p["scenes"] for p in result["pairs"] if p["duplicate"]]
    assert "1 and 3" in result["reason"]


def test_every_pair_is_compared_not_just_neighbours(tmp_path):
    for i, fill in ((1, 10), (2, 90), (3, 170), (4, 250)):
        _write(tmp_path, i, fill=fill)
    result = check(tmp_path)
    assert [p["scenes"] for p in result["pairs"]] == [
        [1, 2], [1, 3], [1, 4], [2, 3], [2, 4], [3, 4]
    ]


def test_a_pair_reports_whether_it_is_adjacent(tmp_path):
    """Adjacency stops gating the comparison but still shapes the message: two
    in a row is a stall the viewer sits through, a distant pair is a camera that
    never left an earlier screen. The author needs to tell them apart."""
    _write(tmp_path, 1, fill=40)
    _write(tmp_path, 2, fill=200)
    _write(tmp_path, 3, fill=40)
    by_pair = {tuple(p["scenes"]): p for p in check(tmp_path)["pairs"]}
    assert by_pair[(1, 2)]["adjacent"] is True
    assert by_pair[(1, 3)]["adjacent"] is False


def test_an_adjacent_duplicate_still_reads_as_a_stall(tmp_path):
    _write(tmp_path, 1, fill=40)
    _write(tmp_path, 2, fill=40)
    _write(tmp_path, 3, fill=200)
    result = check(tmp_path)
    assert result["verdict"] == "fail"
    assert "back to back" in result["reason"]


def test_a_distant_duplicate_says_how_far_back_the_camera_stalled(tmp_path):
    _write(tmp_path, 1, fill=40)
    _write(tmp_path, 2, fill=200)
    _write(tmp_path, 3, fill=90)
    _write(tmp_path, 4, fill=40)  # same as scene 1, three apart
    result = check(tmp_path)
    assert result["verdict"] == "fail"
    assert "3 scenes apart" in result["reason"]
    assert "scene 1" in result["reason"]


def test_before_frames_are_ignored(tmp_path):
    _write(tmp_path, 1, fill=40)
    _write(tmp_path, 2, fill=200)
    snaps = tmp_path / "snapshots"
    Image.fromarray(np.full((180, 320, 3), 40, dtype=np.uint8)).save(snaps / "scene_2_before.png")
    result = check(tmp_path)
    assert [p["scenes"] for p in result["pairs"]] == [[1, 2]]


def test_scenes_order_numerically_not_lexically(tmp_path):
    for i, fill in ((1, 10), (2, 90), (10, 170)):
        _write(tmp_path, i, fill=fill)
    result = check(tmp_path)
    assert [p["scenes"] for p in result["pairs"]] == [[1, 2], [1, 10], [2, 10]]


def test_a_single_scene_run_passes(tmp_path):
    _write(tmp_path, 1, fill=40)
    assert check(tmp_path)["verdict"] == "pass"


def test_missing_snapshots_dir_fails(tmp_path):
    assert check(tmp_path)["verdict"] == "fail"


def test_a_different_viewport_is_a_different_picture(tmp_path):
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    Image.fromarray(np.full((180, 320, 3), 40, dtype=np.uint8)).save(snaps / "scene_1.png")
    Image.fromarray(np.full((360, 640, 3), 40, dtype=np.uint8)).save(snaps / "scene_2.png")
    assert check(tmp_path)["verdict"] == "pass"
