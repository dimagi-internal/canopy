"""Tests for `canopy gslides publish` (agent_gslides.py) — .pptx → Google Slides as the agent."""
import json
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from orchestrator.agent_gdoc import AgentGdocError, GdocIdentity
from orchestrator.agent_gslides import (
    SLIDES_MIME,
    parse_slide_count,
    pptx_slide_count,
    publish_slides,
)

IDENT = GdocIdentity(slug="eva", account="eva@dimagi-ai.com", client="canopy")


def _pptx(tmp_path, slides=3, name="deck.pptx"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("ppt/presentation.xml", "<p/>")
        for i in range(1, slides + 1):
            z.writestr(f"ppt/slides/slide{i}.xml", "<s/>")
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", "<r/>")  # must NOT count
        z.writestr("ppt/notesSlides/notesSlide1.xml", "<n/>")
    return p


class FakeGog:
    """Answers gog by verb; records every call."""

    def __init__(self, *, deck_slides=3, mime=SLIDES_MIME, upload_rc=0, perms=None):
        self.calls = []
        self.deck_slides, self.mime, self.upload_rc = deck_slides, mime, upload_rc
        self.perms = perms if perms is not None else [{"type": "domain", "domain": "dimagi.com"}]

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        verb = " ".join(cmd[1:3])
        out, rc = "{}", 0
        if verb == "drive upload":
            rc = self.upload_rc
            out = json.dumps({"file": {"id": "NEW", "mimeType": self.mime}})
        elif verb == "api call":
            out = json.dumps({"presentationId": "NEW",
                              "slides": [{"objectId": f"s{i}"} for i in range(self.deck_slides)]})
        elif verb == "drive permissions":
            out = json.dumps({"permissions": self.perms})
        return SimpleNamespace(returncode=rc, stdout=out, stderr="boom" if rc else "")

    def verbs(self):
        return [" ".join(c[1:3]) for c in self.calls]


def test_pptx_slide_count_ignores_rels_and_notes(tmp_path):
    assert pptx_slide_count(str(_pptx(tmp_path, slides=54))) == 54


def test_truncated_download_is_a_clear_error(tmp_path):
    p = tmp_path / "half.pptx"
    p.write_bytes(b"PK\x03\x04 not finished")
    with pytest.raises(AgentGdocError, match="download finished"):
        pptx_slide_count(str(p))


def test_parse_slide_count_shapes():
    assert parse_slide_count(json.dumps({"presentationId": "x", "slides": [{}, {}]})) == 2
    assert parse_slide_count(json.dumps({"presentationId": "x"})) == 0
    assert parse_slide_count(json.dumps({"result": {"presentationId": "x", "slides": [{}]}})) == 1
    assert parse_slide_count("not json") is None
    assert parse_slide_count(json.dumps({"error": "nope"})) is None


def test_refuses_without_destination(tmp_path):
    with pytest.raises(AgentGdocError, match="My Drive root"):
        publish_slides(IDENT, pptx_path=str(_pptx(tmp_path)), name=None, parent=None,
                       share="domain", runner=FakeGog())


def test_refuses_a_pdf(tmp_path):
    p = tmp_path / "deck.pdf"
    p.write_bytes(b"%PDF")
    with pytest.raises(AgentGdocError, match="PDF cannot be converted"):
        publish_slides(IDENT, pptx_path=str(p), name=None, parent="F", share="none",
                       runner=FakeGog())


def test_happy_path_converts_files_verifies_and_shares(tmp_path):
    gog = FakeGog(deck_slides=3)
    r = publish_slides(IDENT, pptx_path=str(_pptx(tmp_path, 3)), name=None, parent="FOLDER",
                       share="domain", runner=gog)
    assert r["id"] == "NEW" and r["slides"] == 3 and r["pptx_slides"] == 3
    assert r["degraded"] == [] and r["verified"] is True
    assert r["name"] == "deck"  # defaults to the file stem
    up = gog.calls[0]
    assert up[up.index("--convert-to") + 1] == "slides"
    assert up[up.index("--parent") + 1] == "FOLDER"
    assert gog.verbs() == ["drive upload", "api call", "drive share", "drive permissions"]


def test_dropped_slides_are_a_degradation(tmp_path):
    r = publish_slides(IDENT, pptx_path=str(_pptx(tmp_path, 54)), name="D", parent="F",
                       share="none", runner=FakeGog(deck_slides=50))
    assert r["degraded"] == ["the pptx has 54 slides but the converted deck has 50"]


def test_unconverted_upload_fails_loudly(tmp_path):
    gog = FakeGog(mime="application/vnd.openxmlformats-officedocument.presentationml.presentation")
    with pytest.raises(AgentGdocError, match="did not convert"):
        publish_slides(IDENT, pptx_path=str(_pptx(tmp_path)), name="D", parent="F",
                       share="none", runner=gog)


def test_supersede_trashes_old_copy_only_after_verify(tmp_path):
    gog = FakeGog(deck_slides=3)
    r = publish_slides(IDENT, pptx_path=str(_pptx(tmp_path, 3)), name="D", parent="F",
                       share="none", supersede="OLD", runner=gog)
    assert r["superseded"] == "OLD"
    trash = gog.calls[-1]
    assert trash[1:4] == ["drive", "delete", "OLD"] and "--permanent" not in trash


def test_supersede_keeps_old_copy_when_new_one_is_degraded(tmp_path):
    gog = FakeGog(deck_slides=2)
    r = publish_slides(IDENT, pptx_path=str(_pptx(tmp_path, 3)), name="D", parent="F",
                       share="none", supersede="OLD", runner=gog)
    assert r["superseded"] == "" and "old copy kept" in r["supersede_skipped"]
    assert "drive delete" not in gog.verbs()


def test_unverified_share_keeps_old_copy(tmp_path):
    gog = FakeGog(deck_slides=3, perms=[])
    r = publish_slides(IDENT, pptx_path=str(_pptx(tmp_path, 3)), name="D", parent="F",
                       share="domain", supersede="OLD", runner=gog)
    assert r["verified"] is False and r["superseded"] == ""


def test_dry_run_touches_nothing(tmp_path):
    gog = FakeGog()
    r = publish_slides(IDENT, pptx_path=str(_pptx(tmp_path, 4)), name="D", parent="F",
                       share="domain", dry_run=True, runner=gog)
    assert r["dry_run"] and r["pptx_slides"] == 4 and gog.calls == []
