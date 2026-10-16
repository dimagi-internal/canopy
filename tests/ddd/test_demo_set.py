from unittest.mock import patch
import urllib.error

import pytest

from scripts.ddd import demo_set

BOARD = {"slug": "connect-supply-rutf", "title": "Connect Supply", "decisions": [
    {"id": "banner", "question": "Rename?", "options": ["Rename", "Keep"], "recommended": "Rename"}]}


def test_publish_creates_then_patches_when_slug_exists():
    seen = []

    def fake(method, path, body=None):
        seen.append((method, path))
        if method == "POST":
            raise urllib.error.HTTPError(path, 409, "exists", {}, None)
        return {"slug": "connect-supply-rutf", "decisions": body["decisions"]}

    with patch.object(demo_set, "_request", side_effect=fake):
        out = demo_set.publish(BOARD)
    assert seen == [("POST", "/api/storyboards/"), ("PATCH", "/api/storyboards/connect-supply-rutf")]
    assert out["decisions"][0]["id"] == "banner"


def test_publish_refuses_legacy_brief_lists():
    with pytest.raises(ValueError, match="decisions"):
        demo_set.publish({**BOARD, "brief": {"assumptions": ["x"]}})
