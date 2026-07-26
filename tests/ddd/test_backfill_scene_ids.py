"""Backfill of stable scene ids onto pre-L0 specs."""
from __future__ import annotations

import yaml

from scripts.ddd.backfill_scene_ids import backfill


def _write(tmp_path, raw):
    p = tmp_path / "demo.yaml"
    p.write_text(yaml.dump(raw))
    return p


def _base(**over):
    raw = {
        "name": "demo", "narrative": "A story.", "base_url": "http://x",
        "personas": {}, "scenes": [{"title": "Area Selection", "show": "x"}],
    }
    raw.update(over)
    return raw


def test_backfill_writes_the_title_slug_as_the_id(tmp_path):
    p = _write(tmp_path, _base())
    result = backfill(p)
    assert result["added"] == 1
    assert yaml.safe_load(p.read_text())["scenes"][0]["id"] == "area-selection"


def test_backfill_is_idempotent(tmp_path):
    p = _write(tmp_path, _base(scenes=[{"id": "kept", "title": "Area Selection", "show": "x"}]))
    result = backfill(p)
    assert result["added"] == 0
    assert yaml.safe_load(p.read_text())["scenes"][0]["id"] == "kept"


def test_backfill_preserves_the_render_recipe(tmp_path):
    p = _write(tmp_path, _base(scenes=[{
        "title": "Area Selection",
        "show": "css:text=/^Hyperzoomed$/",
        "url": "/plans/3536/review/",
        "viewport": {"width": 1440, "height": 900},
    }]))
    backfill(p)
    scene = yaml.safe_load(p.read_text())["scenes"][0]
    assert scene["show"] == "css:text=/^Hyperzoomed$/"
    assert scene["url"] == "/plans/3536/review/"
    assert scene["viewport"] == {"width": 1440, "height": 900}


def test_backfill_skips_a_file_with_no_scenes(tmp_path):
    p = tmp_path / "brief.yaml"
    p.write_text(yaml.dump({"narrative_slug": "demo", "spine": []}))
    result = backfill(p)
    assert result["skipped"] is True
    assert result["added"] == 0
