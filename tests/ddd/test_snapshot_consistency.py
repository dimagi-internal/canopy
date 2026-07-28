"""A judge must not score a run dir two renders wrote.

Drawn from a real loss: a concept-eval ran for fourteen minutes against
oes-supply-base while the next iteration re-rendered into the same run dir. It
came back describing a review queue that had been fixed two renders earlier.
Nothing in the artifacts said the inputs were mixed.
"""
from __future__ import annotations

import json

from scripts.ddd.snapshot_consistency import check


def _write(run_dir, scene_index, render_id=None, text="hello"):
    snapshots = run_dir / "snapshots"
    snapshots.mkdir(exist_ok=True)
    payload = {
        "scene_index": scene_index,
        "url": "http://localhost:8009/supply/",
        "title": f"Scene {scene_index}",
        "page_text": text,
    }
    if render_id is not None:
        payload["render_id"] = render_id
    (snapshots / f"scene_{scene_index}_page_text.json").write_text(json.dumps(payload))


def test_one_render_passes(tmp_path):
    for i in (1, 2, 3):
        _write(tmp_path, i, render_id="abc123")
    result = check(tmp_path)
    assert result["verdict"] == "pass"
    assert result["scenes"] == 3


def test_two_renders_in_one_dir_fails(tmp_path):
    """The exact shape of the loss: an overwrite caught mid-judge."""
    _write(tmp_path, 1, render_id="older")
    _write(tmp_path, 2, render_id="newer")
    _write(tmp_path, 3, render_id="newer")

    result = check(tmp_path)
    assert result["verdict"] == "fail"
    assert "different renders" in result["reason"]
    # The report has to say WHICH scenes came from where, or the reader cannot
    # tell whether the verdict they are holding is salvageable.
    assert result["renders"]["older"] == [1]
    assert result["renders"]["newer"] == [2, 3]


def test_a_fresh_render_landing_on_an_unstamped_one_fails(tmp_path):
    _write(tmp_path, 1)  # no stamp — an older render
    _write(tmp_path, 2, render_id="newer")
    assert check(tmp_path)["verdict"] == "fail"


def test_entirely_unstamped_run_passes_with_a_note(tmp_path):
    """Runs captured before stamping existed are not retroactively broken."""
    for i in (1, 2):
        _write(tmp_path, i)
    result = check(tmp_path)
    assert result["verdict"] == "pass"
    assert "before render stamping" in result["reason"]


def test_missing_snapshots_dir_fails(tmp_path):
    result = check(tmp_path)
    assert result["verdict"] == "fail"
    assert "no snapshots directory" in result["reason"]


def test_empty_snapshots_dir_fails(tmp_path):
    (tmp_path / "snapshots").mkdir()
    result = check(tmp_path)
    assert result["verdict"] == "fail"
    assert "nothing to judge" in result["reason"]


def test_a_truncated_page_text_fails_rather_than_being_skipped(tmp_path):
    """A half-written JSON means the render died; judging it measures the crash."""
    _write(tmp_path, 1, render_id="abc")
    (tmp_path / "snapshots" / "scene_2_page_text.json").write_text('{"scene_index": 2, "pa')
    result = check(tmp_path)
    assert result["verdict"] == "fail"
    assert "unreadable" in result["reason"]


def test_scenes_are_ordered_numerically_not_lexically(tmp_path):
    """scene_10 must not sort between scene_1 and scene_2."""
    for i in (1, 2, 10):
        _write(tmp_path, i, render_id="abc")
    result = check(tmp_path)
    assert result["renders"]["abc"] == [1, 2, 10]
