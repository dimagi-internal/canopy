"""`persona` must survive build_scenes_from_spec.

The off-camera-persona feature was a silent no-op the first time it ran against a
real app, because this function builds the recorder's scene dicts from an explicit
whitelist and `persona` was not on it. The failure mode is nasty: preflight reads
the spec directly, so it switched personas and PASSED, while the render never saw
the field and filmed every scene as whoever signed in first — 45 of 66 actions
failed against the wrong seat's app.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough.record_video import build_scenes_from_spec  # noqa: E402


def _spec(scenes):
    return {"name": "x", "base_url": "https://x.test", "scenes": scenes}


def test_persona_reaches_the_recorder():
    spec = _spec(
        [
            {"title": "one", "persona": "ada", "url": "/a", "actions": []},
            {"title": "two", "persona": "amina", "url": "/b", "actions": []},
        ]
    )
    built = build_scenes_from_spec(spec, "https://x.test", run_data=None)

    assert [s["persona"] for s in built] == ["ada", "amina"]


def test_absent_persona_is_none_not_missing():
    """The recorder reads scene.get("persona"); the key should exist and be None
    rather than be absent, so the shape is uniform across scenes."""
    spec = _spec([{"title": "one", "url": "/a", "actions": []}])
    built = build_scenes_from_spec(spec, "https://x.test", run_data=None)

    assert "persona" in built[0]
    assert built[0]["persona"] is None
