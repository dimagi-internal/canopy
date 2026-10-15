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


def _sources(tmp_path, *, cut=True):
    """The iteration sources an upload must carry: the recipe, and for a cut the
    master and capture report it was cut from."""
    out = {"recipe": tmp_path / "chlorine.recipe.yaml"}
    out["recipe"].write_text("style: recorded\n")
    if cut:
        out["master"] = tmp_path / "master.mp4"
        out["master"].write_bytes(b"\x00m")
        out["capture_report"] = tmp_path / "report.json"
        out["capture_report"].write_text("{}")
    return out


def _pushes():
    calls: list = []

    def push(slug, review_id, files, **kw):
        calls.append((slug, review_id, dict(files)))
        return {r: {"reused": False} for r in files}

    push.calls = calls
    return push


@pytest.fixture(autouse=True)
def _workspace(monkeypatch):
    """The narrative link is /w/<workspace>/ddd/<slug> — never flat (canopy-web#1337)."""
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")


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
    push = _pushes()
    result = upload_narrative_video(
        "chlorine", str(mp4), cut=cut, sources=_sources(tmp_path),
        _detail=lambda *a, **k: DETAIL, _upload=up, _push_sources=push,
    )
    kw = up.calls[0]
    assert kw["cut_id"] == "assign"
    assert kw["cut_scene_ids"] == ["dispenser-list", "assign"]
    assert kw["narrative_review_id"] == "rev-1"
    # A cut is a clip unless chosen as the hero, and is titled as the cut.
    assert kw["role"] == "clip"
    assert kw["title"] == "Assigning a dispenser"
    assert result["cut_id"] == "assign"
    # The sources were pinned to the SAME version, before the video went up.
    assert push.calls[0][:2] == ("chlorine", "rev-1")
    assert set(push.calls[0][2]) == {"recipe", "master", "capture_report"}


def test_a_cut_can_be_chosen_as_the_hero(tmp_path):
    mp4 = tmp_path / "cut.mp4"
    mp4.write_bytes(b"\x00")
    up = _capture()
    upload_narrative_video(
        "chlorine", str(mp4), cut={"id": "assign", "title": "", "scene_ids": ["a"]},
        role="hero_video", sources=_sources(tmp_path),
        _detail=lambda *a, **k: DETAIL, _upload=up, _push_sources=_pushes(),
    )
    assert up.calls[0]["role"] == "hero_video"
    assert up.calls[0]["title"] == "assign"


def test_a_plain_upload_is_unchanged(tmp_path):
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"\x00")
    up = _capture()
    upload_narrative_video(
        "chlorine", str(mp4), sources=_sources(tmp_path, cut=False),
        _detail=lambda *a, **k: DETAIL, _upload=up, _push_sources=_pushes(),
    )
    kw = up.calls[0]
    assert kw["role"] == "hero_video"
    assert kw["title"] == "chlorine v2"
    assert kw["cut_id"] is None


@pytest.mark.parametrize(
    "cut, given, missing",
    [
        (None, {}, "recipe"),
        ({"id": "assign", "title": "", "scene_ids": ["a"]}, {"recipe"}, "master, capture_report"),
        ({"id": "assign", "title": "", "scene_ids": ["a"]}, {"recipe", "master"}, "capture_report"),
    ],
)
def test_a_video_is_refused_without_what_it_takes_to_rerender(tmp_path, cut, given, missing):
    """The chlorine demo (2026-10-14): eight cuts were published to canopy-web
    while the recipe and master lived only in one machine's temp dir, and the
    next turn could not re-record. A video without its sources is refused, and
    nothing is uploaded."""
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"\x00")
    up = _capture()
    push = _pushes()
    sources = {k: v for k, v in _sources(tmp_path).items() if k in given}
    with pytest.raises(SystemExit) as exc:
        upload_narrative_video(
            "chlorine", str(mp4), cut=cut, sources=sources,
            _detail=lambda *a, **k: DETAIL, _upload=up, _push_sources=push,
        )
    assert f"without its iteration sources ({missing})" in str(exc.value)
    assert up.calls == [] and push.calls == []


def test_publish_artifact_sends_the_cut_fields(monkeypatch, pinned_write_workspace):
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
        return {"version": 2, "narrative_url": "u", "video_url": "v",
                "sources": {r: False for r, p in kw["sources"].items() if p}}

    monkeypatch.setattr("scripts.ddd.upload.upload_narrative_video", fake)
    args = ["upload-video", "chlorine", str(mp4), *extra]
    args = [a.replace("{spec}", str(spec_path)) for a in args]
    snippets.main(args)
    return seen


CUT_SOURCES = ("--master", "m.mp4", "--report", "r.json")


def test_cli_cut_resolves_title_and_scenes_from_the_recipe(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, "--cut", "verify", "--spec", "{spec}", *CUT_SOURCES)
    assert seen["cut"]["id"] == "verify"
    assert seen["cut"]["title"] == "Verifying a refill"
    assert seen["cut"]["scene_ids"] == ["refill-check"]
    assert seen["role"] is None
    # The CLI hands the recipe, master and report on, to be pinned with the video.
    assert str(seen["sources"]["recipe"]).endswith("chlorine.yaml")
    assert seen["sources"]["master"] == "m.mp4"
    assert seen["sources"]["capture_report"] == "r.json"


def test_cli_hero_cut(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, "--cut", "assign", "--spec", "{spec}", "--hero",
                *CUT_SOURCES)
    assert seen["role"] == "hero_video"


def test_cli_without_cut_uploads_the_hero_as_before(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, "--spec", "{spec}")
    assert seen["cut"] is None and seen["role"] is None


@pytest.mark.parametrize(
    "extra, message",
    [
        (("--cut", "nope", "--spec", "{spec}"), "no cut 'nope'"),
        (("--spec", "{spec}", "--hero"), "--hero only applies to a cut"),
    ],
)
def test_cli_refuses_a_cut_it_cannot_place(monkeypatch, tmp_path, extra, message):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, *extra)
    assert message in str(exc.value)


def test_cli_requires_the_recipe(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, "--cut", "assign")
    assert "the following arguments are required: --spec" in capsys.readouterr().err


def test_cli_refuses_a_cut_of_an_explainer_spec(monkeypatch, tmp_path):
    explainer = {k: v for k, v in _spec().items() if k not in ("style", "cuts")}
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, "--cut", "assign", "--spec", "{spec}", spec=explainer)
    assert "not a `style: recorded` spec" in str(exc.value)


# ---------------------------------------------------------------------------
# The product screens a cut films travel with it as `reference` links, so the
# review link and the storyboard can send a reviewer to the live page
# (canopy-web#1512). The RUTF cuts went up with none because this path never
# built them.
# ---------------------------------------------------------------------------

RECIPE = """\
name: rutf
base_url: https://labs.example
style: recorded
scenes:
  - {id: overview, title: Every procurement, url: "/supply/overview/?program_id=1"}
  - {id: suppliers, title: Suppliers, url: "/supply/tenders/356/"}
  - {id: compare, title: Compare, url: "/supply/tenders/356/compare/"}
  - {id: made, title: Made on camera, url: "/supply/audits/${audit_id}/"}
"""


def _recipe_sources(tmp_path, manifest=None):
    files = _sources(tmp_path)
    files["recipe"].write_text(RECIPE)
    if manifest is not None:
        files["capture_manifest"] = tmp_path / "manifest.json"
        files["capture_manifest"].write_text(__import__("json").dumps(manifest))
    return files


def _upload_cut(tmp_path, cut, files):
    mp4 = tmp_path / "cut.mp4"
    mp4.write_bytes(b"\x00")
    up = _capture()
    upload_narrative_video(
        "rutf", str(mp4), cut=cut, sources=files,
        _detail=lambda *a, **k: DETAIL, _upload=up, _push_sources=_pushes(),
    )
    return up.calls[0]["links"]


def test_a_cut_carries_only_its_own_scenes_screens_from_the_recipe(tmp_path):
    cut = {"id": "buying", "title": "Buying", "scene_ids": ["suppliers", "compare"],
           "scene_indexes": [2, 3]}
    assert _upload_cut(tmp_path, cut, _recipe_sources(tmp_path)) == [
        {"label": "Suppliers", "url": "https://labs.example/supply/tenders/356/", "kind": "reference"},
        {"label": "Compare", "url": "https://labs.example/supply/tenders/356/compare/", "kind": "reference"},
    ]


def test_the_recorders_manifest_wins_and_adds_pages_visited_mid_scene(tmp_path):
    manifest = {"base_url": "https://labs.example", "slides": [
        {"type": "scene", "scene_index": 4, "title": "Made on camera",
         "url": "/supply/audits/77/", "urls_visited": ["https://labs.example/supply/audits/77/done/"]},
        {"type": "scene", "scene_index": 1, "title": "Every procurement", "url": "/supply/overview/"},
    ]}
    cut = {"id": "audit", "title": "Audit", "scene_ids": ["made"], "scene_indexes": [4]}
    links = _upload_cut(tmp_path, cut, _recipe_sources(tmp_path, manifest))
    assert [l["url"] for l in links] == [
        "https://labs.example/supply/audits/77/",
        "https://labs.example/supply/audits/77/done/",
    ]


def test_an_unresolved_or_bare_url_is_never_sent(tmp_path):
    from scripts.ddd.upload import cut_product_links

    spec = {"base_url": "https://labs.example", "scenes": [
        {"id": "a", "url": "/supply/audits/${audit_id}/"},
        {"id": "b", "url": "https://labs.example/"},
        {"id": "c", "url": "/"},
    ]}
    assert cut_product_links(spec=spec, manifest={}, cut=None) == []


def test_a_recipe_without_urls_uploads_without_links(tmp_path):
    cut = {"id": "assign", "title": "Assigning", "scene_ids": ["x"], "scene_indexes": [1]}
    assert _upload_cut(tmp_path, cut, _sources(tmp_path)) is None
