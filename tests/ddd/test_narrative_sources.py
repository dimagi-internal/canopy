"""A narrative's iteration sources live on canopy-web (scripts/ddd/sources.py).

The chlorine demo (2026-10-14): eight recorded cuts were on canopy-web, but the
recipe, the master recording and the capture report they were cut from lived
in one machine's temp dir. When that dir was cleaned, no machine could
re-record the narrative, and the agent asked to re-cut it could only reply
that it did not know where the files were. Everything it takes to keep
iterating is now pinned to the narrative version and restored by `pull`.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from scripts.ddd import narrative as nar
from scripts.ddd import review as rv
from scripts.ddd import sources

API = "https://canopy.test"


class FakeCanopyWeb:
    """The sources routes, in memory: POST pins, GET lists / downloads."""

    def __init__(self):
        self.rows: list[dict] = []
        self.blobs: dict[str, bytes] = {}
        self.posts: list[str] = []

    def post(self, url, token, fields, filename, content_type, data):
        self.posts.append(url)
        sid = f"s{len(self.rows) + 1}"
        row = {
            "id": sid, "role": fields["role"], "review_id": fields["narrative_review_id"],
            "sha256": hashlib.sha256(data).hexdigest(), "reused": False,
            "content_url": f"/api/w/connect/ddd/narratives/chlorine/sources/{sid}/content",
        }
        self.rows.insert(0, row)  # newest first, as the server lists them
        self.blobs[row["content_url"]] = data
        return row

    def get(self, url, token):
        path = url[len(API):]
        if path.endswith("/content"):
            return self.blobs[path]
        return json.dumps(self.rows).encode()


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    monkeypatch.setenv("CANOPY_WEB_PAT", "pat")
    monkeypatch.setenv("CANOPY_WEB_API_URL", API)
    return FakeCanopyWeb()


@pytest.fixture
def capture_root(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    monkeypatch.setenv("CANOPY_DDD_SOURCES_DIR", str(root))
    return root


def _files(d):
    d.mkdir(parents=True, exist_ok=True)
    (d / "chlorine.recipe.yaml").write_text("style: recorded\ncuts: []\n")
    (d / "chlorine.intent.yaml").write_text("intent: []\n")
    (d / "master.mp4").write_bytes(b"\x00master")
    (d / "report.json").write_text('{"scenes": []}')
    return {
        **sources.recipe_files(d / "chlorine.recipe.yaml"),
        "master": d / "master.mp4",
        "capture_report": d / "report.json",
    }


@pytest.mark.real_sources
def test_push_then_pull_restores_every_file_on_another_machine(web, tmp_path, capture_root):
    made_here = _files(tmp_path / "laptop")
    sources.push_sources("chlorine", "rev-4", made_here, _post=web.post)
    assert all("/api/w/connect/ddd/narratives/chlorine/sources/" in u for u in web.posts)

    # A different machine: an empty checkout, nothing local.
    elsewhere = tmp_path / "cloud" / "docs" / "walkthroughs"
    elsewhere.mkdir(parents=True)
    written = sources.pull_sources("chlorine", elsewhere, review_id="rev-4", _get=web.get)

    assert written["recipe"].read_bytes() == made_here["recipe"].read_bytes()
    assert written["intent"].read_bytes() == made_here["intent"].read_bytes()
    assert written["master"] == capture_root / "chlorine" / "master.mp4"
    assert written["master"].read_bytes() == b"\x00master"
    assert written["capture_report"].read_text() == '{"scenes": []}'


@pytest.mark.real_sources
def test_pull_prefers_the_versions_own_files_else_the_newest(web, tmp_path, capture_root):
    old = _files(tmp_path / "v3")
    sources.push_sources("chlorine", "rev-3", old, _post=web.post)
    newer_recipe = tmp_path / "v4" / "chlorine.recipe.yaml"
    newer_recipe.parent.mkdir()
    newer_recipe.write_text("style: recorded\n# v4\n")
    # A new version re-pins its recipe on post; its master is still v3's.
    sources.push_sources("chlorine", "rev-4", {"recipe": newer_recipe}, _post=web.post)

    out = tmp_path / "out"
    out.mkdir()
    written = sources.pull_sources("chlorine", out, review_id="rev-4", _get=web.get)
    assert written["recipe"].read_text().endswith("# v4\n")
    assert written["master"].read_bytes() == b"\x00master"


@pytest.mark.real_sources
def test_a_corrupted_download_is_refused(web, tmp_path, capture_root):
    sources.push_sources("chlorine", "rev-4", _files(tmp_path / "a"), _post=web.post)
    for k in web.blobs:
        web.blobs[k] = b"tampered"
    with pytest.raises(sources.SourcesError, match="sha256"):
        sources.pull_sources("chlorine", tmp_path, _get=web.get)


@pytest.mark.real_sources
def test_a_push_that_did_not_land_intact_fails(web, tmp_path):
    def bad_post(*a, **k):
        return {"sha256": "0" * 64}

    with pytest.raises(sources.SourcesError, match="did not land intact"):
        sources.push_sources("chlorine", "rev-4", _files(tmp_path), _post=bad_post)


@pytest.mark.real_sources
def test_unknown_roles_are_refused(web, tmp_path):
    with pytest.raises(sources.SourcesError, match="unknown source role"):
        sources.push_sources("chlorine", "rev-4", {"cookies": tmp_path}, _post=web.post)


@pytest.mark.real_sources
def test_an_older_canopy_web_without_the_route_restores_nothing(web, monkeypatch):
    def not_found(url, token):
        raise sources.SourcesError("nope", 404)

    assert sources.list_sources("chlorine", _get=not_found) == []


@pytest.mark.real_sources
def test_pull_restores_the_recipe_and_keeps_a_differing_local_one(
    web, tmp_path, capture_root, monkeypatch, capsys
):
    sources.push_sources("chlorine", "rev-4", _files(tmp_path / "laptop"), _post=web.post)
    monkeypatch.setattr(sources, "_default_get", web.get)
    payload = {
        "narration": [{"id": "s1", "title": "T", "persona": "p", "provenance": "S0", "text": "x"}],
        "personas": {}, "narrative": "x", "why_brief": {},
    }
    monkeypatch.setattr(rv, "get_narrative",
                        lambda slug: {"current_version": {"version": 4, "review_id": "rev-4"}})
    monkeypatch.setattr(rv, "get_review", lambda rid: {"request_json": payload})

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    nar._cmd_pull("chlorine", str(fresh))
    out = capsys.readouterr()
    assert json.loads(out.out)["recipe"]  # renderable from canopy-web alone
    assert (fresh / "chlorine.recipe.yaml").read_text().startswith("style: recorded")
    assert "no render recipe" not in out.err

    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "chlorine.recipe.yaml").write_text("style: recorded\n# my edit\n")
    nar._cmd_pull("chlorine", str(mine))
    err = capsys.readouterr().err
    assert (mine / "chlorine.recipe.yaml").read_text().endswith("# my edit\n")
    assert (mine / "chlorine.recipe.yaml.web").read_text().startswith("style: recorded\ncuts")
    assert "differs from the one pinned on canopy-web" in err


def test_posting_a_version_pins_its_recipe(tmp_path, monkeypatch, request):
    """`narrative post` pins the recipe to the version it just created."""
    from scripts.ddd import runstate as rs

    monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: tmp_path)
    spec = tmp_path / "chlorine.recipe.yaml"
    monkeypatch.setattr(nar, "load_spec", lambda p: type("S", (), {"name": "chlorine"})())
    monkeypatch.setattr(nar, "load_why_brief", lambda p, s: None)
    monkeypatch.setattr(nar, "build_narrative_review_request",
                        lambda *a, **k: type("R", (), {"narrative_slug": "chlorine"})())
    monkeypatch.setattr(nar, "_stamp_run_state", lambda *a: None)
    spec.write_text("style: recorded\n")

    class FakeRv:
        @staticmethod
        def post_review_request(req):
            return {"id": "rev-9", "url": "u", "share_token": "t"}

    nar.post_narrative_version(str(spec), "chlorine-2026-10-14-001", rv=FakeRv)
    assert request.node.pushed_sources == [
        ("chlorine", "rev-9", {"recipe": spec}),
    ]
