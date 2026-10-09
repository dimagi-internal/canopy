"""The DDD uploader sends the artifact's project (canopy-web T76)."""
from __future__ import annotations

import pytest

from scripts.ddd.upload import publish_artifact


@pytest.mark.usefixtures("pinned_write_workspace")
def test_ddd_publish_sends_the_project_fields(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
    monkeypatch.setenv("CANOPY_PROJECT_SLUG", "connect-labs")
    monkeypatch.setenv("CANOPY_AGENT_PROJECT", "hal/P5")
    sent = {}

    def fake_post(url, pat, fields, filename, content_type, file_bytes):
        sent.update(fields)
        return {"id": "abc"}

    publish_artifact("<html/>", kind="html", title="t", base_url="https://canopy.test",
                     _post=fake_post)
    assert sent["project_slug"] == "connect-labs"
    assert sent["agent_project"] == "hal/P5"
