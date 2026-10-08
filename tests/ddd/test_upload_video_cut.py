"""`snippets upload-video --cut` — a recorded narrative's video, by cut.

A ``style: recorded`` narrative renders one mp4 per cut (#796). canopy-web
keeps one video per cut id on the narrative version and shows each beside its
narration on the review link (canopy-web#1288), so the upload has to say which
cut it is — and which scenes that cut plays, resolved from the recipe rather
than typed by hand.
"""
from __future__ import annotations

import pytest
import yaml

from scripts.ddd import snippets
from scripts.ddd.upload import publish_artifact, upload_narrative_video

from tests.ddd.test_recorded import _spec

DETAIL = {"current_version": {"review_id": "rev-1", "version": 2}}


def _capture():
    calls: list[dict] = []

    def fake_upload(data, **kw):
        calls.append(kw)
        return "https://canopy.test/walkthrough/w1?t=s"

    fake_upload.calls = calls
    return fake_upload


def test_a_cut_upload_names_its_cut_and_scenes(tmp_path):
    mp4 = tmp_path / "cut.mp4"
    mp4.write_bytes(b"\x00")
    up = _capture()
    cut = {"id": "assign", "title": "Assigning a dispenser",
           "scene_ids": ["dispenser-list", "assign"], "scene_indexes": [1, 2]}
    result = upload_narrative_video(
        "chlorine", str(mp4), cut=cut, _detail=lambda *a, **k: DETAIL, _upload=up,
    )
    kw = up.calls[0]
    assert kw["cut_id"] == "assign"
    assert kw["cut_scene_ids"] == ["dispenser-list", "assign"]
    assert kw["narrative_review_id"] == "rev-1"
    # A cut is a clip unless chosen as the hero, and is titled as the cut.
    assert kw["role"] == "clip"
    assert kw["title"] == "Assigning a dispenser"
    assert result["cut_id"] == "assign"


def test_a_cut_can_be_chosen_as_the_hero(tmp_path):
    mp4 = tmp_path / "cut.mp4"
    mp4.write_bytes(b"\x00")
    up = _capture()
    upload_narrative_video(
        "chlorine", str(mp4), cut={"id": "assign", "title": "", "scene_ids": ["a"]},
        role="hero_video", _detail=lambda *a, **k: DETAIL, _upload=up,
    )
    assert up.calls[0]["role"] == "hero_video"
    assert up.calls[0]["title"] == "assign"


def test_a_plain_upload_is_unchanged(tmp_path):
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"\x00")
    up = _capture()
    upload_narrative_video("chlorine", str(mp4), _detail=lambda *a, **k: DETAIL, _upload=up)
    kw = up.calls[0]
    assert kw["role"] == "hero_video"
    assert kw["title"] == "chlorine v2"
    assert kw["cut_id"] is None


def test_publish_artifact_sends_the_cut_fields(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
    sent: list[dict] = []

    def fake_post(url, pat, fields, filename, content_type, file_bytes):
        sent.append(fields)
        return {"id": "w1"}

    publish_artifact(
        b"v", kind="video", title="Cut", base_url="https://canopy.test",
        narrative_review_id="rev-1", cut_id="assign",
        cut_scene_ids=["dispenser-list", "assign"], _post=fake_post,
    )
    assert sent[0]["cut_id"] == "assign"
    assert sent[0]["cut_scene_ids"] == "dispenser-list,assign"

    publish_artifact(b"v", kind="video", title="Hero", base_url="https://canopy.test",
                     _post=fake_post)
    assert "cut_id" not in sent[1] and "cut_scene_ids" not in sent[1]


# --------------------------------------------------------------------------- #
# The CLI resolves --cut against the recipe
# --------------------------------------------------------------------------- #


def _run(monkeypatch, tmp_path, *extra, spec=None):
    spec_path = tmp_path / "chlorine.yaml"
    spec_path.write_text(yaml.safe_dump(spec if spec is not None else _spec()))
    mp4 = tmp_path / "cut.mp4"
    mp4.write_bytes(b"\x00")
    seen: dict = {}

    def fake(slug, video, **kw):
        seen.update(kw, slug=slug)
        return {"version": 2, "narrative_url": "u", "video_url": "v"}

    monkeypatch.setattr("scripts.ddd.upload.upload_narrative_video", fake)
    args = ["upload-video", "chlorine", str(mp4), *extra]
    args = [a.replace("{spec}", str(spec_path)) for a in args]
    snippets.main(args)
    return seen


def test_cli_cut_resolves_title_and_scenes_from_the_recipe(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, "--cut", "verify", "--spec", "{spec}")
    assert seen["cut"]["id"] == "verify"
    assert seen["cut"]["title"] == "Verifying a refill"
    assert seen["cut"]["scene_ids"] == ["refill-check"]
    assert seen["role"] is None


def test_cli_hero_cut(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, "--cut", "assign", "--spec", "{spec}", "--hero")
    assert seen["role"] == "hero_video"


def test_cli_without_cut_uploads_the_hero_as_before(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path)
    assert seen["cut"] is None and seen["role"] is None


@pytest.mark.parametrize(
    "extra, message",
    [
        (("--cut", "nope", "--spec", "{spec}"), "no cut 'nope'"),
        (("--cut", "assign"), "--cut needs --spec"),
        (("--hero",), "--hero only applies to a cut"),
    ],
)
def test_cli_refuses_a_cut_it_cannot_place(monkeypatch, tmp_path, extra, message):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, *extra)
    assert message in str(exc.value)


def test_cli_refuses_a_cut_of_an_explainer_spec(monkeypatch, tmp_path):
    explainer = {k: v for k, v in _spec().items() if k not in ("style", "cuts")}
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, "--cut", "assign", "--spec", "{spec}", spec=explainer)
    assert "not a `style: recorded` spec" in str(exc.value)
