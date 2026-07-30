"""Tests for scripts/ddd/upload.py.

All HTTP calls, file uploads, and review gates are mocked — no network, no
real sleeping, no canopy-web dependency.
"""
from __future__ import annotations

import html
from pathlib import Path

import pytest
import yaml

from scripts.ddd.schemas.models import RunState, Scene, UnifiedSpec, WhyBrief, SpineItem, Persona
from scripts.ddd.upload import (
    DeckMissingError,
    build_docs_page,
    publish_artifact,
    upload_run,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_spec() -> UnifiedSpec:
    return UnifiedSpec(
        name="Smart Routing",
        narrative="Route smarter, not harder.",
        base_url="https://example.com",
        personas={
            "alice": Persona(
                name="Alice",
                role="Field worker",
                color="#2563eb",
                intro="Alice manages daily visits.",
            )
        },
        scenes=[
            Scene(
                persona="alice",
                title="Scene 1",
                show="Open the app and tap Routes.",
                concept_claim="Instantly see your optimised route for the day.",
                provenance="SP1",
            ),
            Scene(
                persona="alice",
                title="Scene 2",
                show="Tap Start to begin navigation.",
                concept_claim="Turn-by-turn navigation with live traffic.",
                provenance="SP2",
            ),
        ],
    )


def _make_why_brief() -> WhyBrief:
    return WhyBrief(
        narrative_slug="Smart Routing",
        problem="Field workers spend 40% of their day navigating inefficiently.",
        spine=[
            SpineItem(
                id="s1",
                claim="Route optimisation saves 2 hours per worker per day.",
                rationale="Internal pilot with 20 workers showed 1.8h average saving.",
            ),
            SpineItem(
                id="s2",
                claim="Live traffic avoids delays at peak hours.",
                rationale="Traffic API integration reduces average trip time by 12%.",
            ),
        ],
        gaps=[],
    )


# NOTE: _external_links_from_spec was deleted in the manifest refactor — external
# links now derive from the render manifest (walkthrough-run-data.json). See
# tests/ddd/test_upload_links.py for _external_links_from_manifest coverage.


# ---------------------------------------------------------------------------
# build_docs_page — content tests
# ---------------------------------------------------------------------------


class TestBuildDocsPage:
    def test_contains_video_url(self):
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://example.com/hero.mp4")
        assert "https://example.com/hero.mp4" in result

    def test_contains_feature_name(self):
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        assert "Smart Routing" in result

    def test_contains_every_concept_claim(self):
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        for scene in spec.scenes:
            assert scene.concept_claim in result, (
                f"concept_claim {scene.concept_claim!r} missing from HTML"
            )

    def test_contains_why_brief_problem(self):
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        # May be HTML-escaped — check for escaped version
        assert "Field workers spend 40%" in result or html.escape(why.problem) in result

    def test_contains_section_headers(self):
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        assert "What you can do" in result
        assert "Why it works this way" in result

    def test_walkthrough_section_dropped(self):
        # The build-audience scene `show` walkthrough is NOT rendered on the user
        # docs page — the hero video is the walkthrough; the shows reference internal UI.
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        assert "What the demo walks through" not in result

    def test_contains_spine_claims(self):
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        for item in why.spine:
            assert item.claim in result or html.escape(item.claim) in result

    def test_xss_in_concept_claim_is_escaped(self):
        """Malicious concept_claim must not appear as raw HTML."""
        spec = _make_spec()
        spec.scenes[0].concept_claim = '<script>alert("xss")</script>'
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        assert "<script>" not in result
        # The escaped version must be present (or entirely absent, but never raw)
        assert "&lt;script&gt;" in result or "alert" not in result

    def test_xss_in_show_is_escaped(self):
        spec = _make_spec()
        spec.scenes[0].show = '<img src=x onerror=alert(1)>'
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        assert "<img src=x onerror=alert(1)>" not in result

    def test_xss_in_why_problem_is_escaped(self):
        spec = _make_spec()
        why = _make_why_brief()
        why.problem = '<script>bad()</script>'
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        assert "<script>" not in result

    def test_xss_in_spine_rationale_is_escaped(self):
        spec = _make_spec()
        why = _make_why_brief()
        why.spine[0].rationale = '<b onmouseover="evil()">text</b>'
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        assert 'onmouseover="evil()"' not in result

    def test_is_valid_html_structure(self):
        """Smoke-check: has DOCTYPE, html, head, body tags."""
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        assert "<!DOCTYPE html>" in result
        assert "<html" in result
        assert "<head>" in result
        assert "<body>" in result

    def test_canopy_viewer_url_embeds_content_stream_as_video(self):
        """A canopy-web /w/<id> viewer URL must embed the public /content byte
        stream as <video> — NOT an <iframe> to the viewer page, which is
        auth-gated (redirects to login) and X-Frame-Options: DENY, so it renders
        blank. The share token is preserved on the rewritten URL."""
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://canopy.example.com/w/abc123?t=tok")
        assert "<video" in result
        assert "https://canopy.example.com/w/abc123/content?t=tok" in result
        # The bare viewer page must not be framed.
        assert '<iframe src="https://canopy.example.com/w/abc123?t=tok"' not in result

    def test_canopy_walkthrough_url_embeds_content_stream_as_video(self):
        """The current /walkthrough/<id> viewer URL (what token-gated uploads
        return) rewrites to its /content stream with the token preserved."""
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(
            spec, why, "https://canopy.example.com/walkthrough/abc123?t=tok"
        )
        assert "<video" in result
        assert "https://canopy.example.com/walkthrough/abc123/content?t=tok" in result
        assert (
            '<iframe src="https://canopy.example.com/walkthrough/abc123?t=tok"'
            not in result
        )

    def test_canopy_content_url_is_not_double_rewritten(self):
        """An already-/content URL embeds as <video> unchanged (idempotent)."""
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://canopy.example.com/w/abc123/content?t=tok")
        assert "<video" in result
        assert "https://canopy.example.com/w/abc123/content?t=tok" in result
        assert "/content/content" not in result

    def test_external_embed_without_w_path_uses_iframe(self):
        """A genuinely external embed (no /w/ path, not .mp4) still uses an
        <iframe> — e.g. a Loom share page."""
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://www.loom.com/embed/xyz789")
        assert "<iframe" in result

    def test_plain_mp4_url_uses_video_tag(self):
        """Direct .mp4 URLs should embed as <video>."""
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://cdn.example.com/video.mp4")
        assert "<video" in result

    def test_data_uri_video_uses_video_tag(self):
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "data:video/mp4;base64,AAAA")
        assert "<video" in result

    def test_no_external_css_or_js_deps(self):
        """The page must be self-contained — no external stylesheet or script src."""
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        # Allow <script> tags (for inline JS) but not <script src="http...
        import re
        external_scripts = re.findall(r'<script[^>]+src=["\']https?://', result)
        external_css = re.findall(r'<link[^>]+href=["\']https?://', result)
        assert not external_scripts, f"External script srcs found: {external_scripts}"
        assert not external_css, f"External CSS links found: {external_css}"

    def test_scenes_in_order(self):
        """Scene concept_claims should appear in the same order as spec.scenes."""
        spec = _make_spec()
        why = _make_why_brief()
        result = build_docs_page(spec, why, "https://x.test/v.mp4")
        pos0 = result.index(spec.scenes[0].concept_claim)
        pos1 = result.index(spec.scenes[1].concept_claim)
        assert pos0 < pos1, "Scenes must appear in spec order"


# ---------------------------------------------------------------------------
# publish_artifact — HTTP injection tests
# ---------------------------------------------------------------------------


class TestPublishArtifact:
    def _mock_post(self, *, wid="art123", share_url=None):
        """Return a fake _post callable that records call args.

        Mirrors canopy-web's token-gated contract: the 201 body carries an
        owner-only ``share_url`` (never a raw ``share_token``).
        """
        calls: list[dict] = []

        def fake_post(url, pat, fields, filename, content_type, file_bytes):
            calls.append(
                dict(
                    url=url,
                    pat=pat,
                    fields=fields,
                    filename=filename,
                    content_type=content_type,
                    file_bytes=file_bytes,
                )
            )
            body = {"id": wid}
            if share_url is not None:
                body["share_url"] = share_url
            return body

        fake_post.calls = calls
        return fake_post

    def test_returns_share_url_from_response(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        mock = self._mock_post(
            wid="abc", share_url="https://canopy.test/walkthrough/abc?t=s1"
        )
        url = publish_artifact(
            "<html>hi</html>",
            kind="html",
            title="Test doc",
            base_url="https://canopy.test",
            _post=mock,
        )
        assert url == "https://canopy.test/walkthrough/abc?t=s1"

    def test_falls_back_to_bare_viewer_url_without_share_url(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        mock = self._mock_post(wid="abc")
        url = publish_artifact(
            "<html>hi</html>",
            kind="html",
            title="Test doc",
            base_url="https://canopy.test",
            _post=mock,
        )
        assert url == "https://canopy.test/walkthrough/abc"

    def test_posts_to_walkthroughs_endpoint(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        mock = self._mock_post()
        publish_artifact(
            b"video bytes",
            kind="video",
            title="Hero",
            base_url="https://canopy.test",
            _post=mock,
        )
        assert mock.calls[0]["url"] == "https://canopy.test/api/walkthroughs/"

    def test_html_kind_sets_correct_content_type(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        mock = self._mock_post()
        publish_artifact(
            "<html></html>",
            kind="html",
            title="Doc",
            base_url="https://canopy.test",
            _post=mock,
        )
        assert mock.calls[0]["content_type"] == "text/html"

    def test_video_kind_sets_correct_content_type(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        mock = self._mock_post()
        publish_artifact(
            b"\x00\x00",
            kind="video",
            title="Vid",
            base_url="https://canopy.test",
            _post=mock,
        )
        assert mock.calls[0]["content_type"] == "video/mp4"

    def test_invalid_kind_raises(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        with pytest.raises(ValueError, match="kind"):
            publish_artifact("x", kind="pdf", title="bad", _post=self._mock_post())

    def test_str_content_encoded_as_utf8(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        mock = self._mock_post()
        publish_artifact(
            "héllo",
            kind="html",
            title="enc",
            base_url="https://canopy.test",
            _post=mock,
        )
        assert mock.calls[0]["file_bytes"] == "héllo".encode("utf-8")

    # (share_url-present and share_url-absent behavior is covered above by
    # test_returns_share_url_from_response / test_falls_back_to_bare_viewer_url_…)


# ---------------------------------------------------------------------------
# upload_run — orchestration tests
# ---------------------------------------------------------------------------


def _write_run_fixtures(tmp_ddd: Path, run_id: str):
    """Write minimal run_state.yaml, unified_spec.yaml, why_brief.yaml."""
    run_dir = tmp_ddd / "runs" / run_id
    run_dir.mkdir(parents=True)

    spec = _make_spec()
    why = _make_why_brief()

    # run_state.yaml
    # narrative_slug is a slug in real use (matches the run_id prefix); the package URL
    # is built from it, so keep it URL-safe here.
    # A converged run that went through the narrative-agreement gate carries a
    # stamped narrative_review_id — that's what lets the upload guard pass
    # without a network check. Tests that exercise the guard's unstamped paths
    # clear this explicitly.
    state = RunState(
        run_id=run_id,
        narrative_slug="smart-routing",
        phase="converged",
        narrative_review_id="11111111-1111-1111-1111-111111111111",
    )
    (run_dir / "run_state.yaml").write_text(
        yaml.dump(state.model_dump(), default_flow_style=False, allow_unicode=True)
    )

    # unified_spec.yaml
    (run_dir / "unified_spec.yaml").write_text(
        yaml.dump(spec.model_dump(), default_flow_style=False, allow_unicode=True)
    )

    # why_brief.yaml
    (run_dir / "why_brief.yaml").write_text(
        yaml.dump(why.model_dump(), default_flow_style=False, allow_unicode=True)
    )

    # walkthrough-run-data.json — the render manifest. The deck refactor reads
    # this directly (no spec rebuild), so a run that uploads MUST carry it. One
    # scene slide is enough to produce a role=deck and external links.
    _write_manifest(run_dir, spec)

    return run_dir


def _write_manifest(run_dir: Path, spec: UnifiedSpec) -> None:
    """Write a minimal render manifest (walkthrough-run-data.json) into *run_dir*
    matching scripts.walkthrough.manifest.build_manifest's shape."""
    import json

    base = (spec.base_url or "").rstrip("/")
    slides = [
        {
            "type": "scene",
            "scene_index": idx,
            "scene_total": len(spec.scenes),
            "title": sc.title,
            "narration": sc.show or "",
            "persona_key": sc.persona,
            "url": f"{base}/scene-{idx}",
            "urls_visited": [f"{base}/scene-{idx}"],
            "screenshot_path": None,
            "page_text_path": None,
            "screenshot_b64": None,
            "mp4_start_offset": 0.0,
            "ok": True,
            "ai_evaluation": None,
        }
        for idx, sc in enumerate(spec.scenes, start=1)
    ]
    manifest = {
        "name": spec.name,
        "narrative": spec.narrative,
        "generated_at": "2026-06-14T00:00:00Z",
        "duration_seconds": 0.0,
        "base_url": base,
        "scenes_run": list(range(1, len(spec.scenes) + 1)),
        "scene_filter": None,
        "substitution_vars": {},
        "personas": {
            k: {"name": v.name, "role": v.role, "color": v.color, "intro": v.intro}
            for k, v in spec.personas.items()
        },
        "slides": slides,
    }
    (run_dir / "walkthrough-run-data.json").write_text(json.dumps(manifest))


class TestUploadRun:
    @pytest.fixture()
    def tmp_run(self, tmp_path, monkeypatch):
        """Set up a tmp DDD dir, a run, and a dummy video file."""
        import scripts.ddd.runstate as rs
        import scripts.ddd.upload as pm

        # Point _resolve_ddd_dir to tmp_path. Patching it on runstate is enough:
        # upload resolves through runstate.run_dir_for, which looks the resolver
        # up at call time — it no longer joins "runs"/run_id for itself.
        monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: tmp_path)
        assert pm.run_dir_for is rs.run_dir_for

        run_id = "smart-routing-2026-01-01-001"
        run_dir = _write_run_fixtures(tmp_path, run_id)

        # Dummy video file
        video_file = tmp_path / "hero.mp4"
        video_file.write_bytes(b"\x00\x01\x02\x03")

        return {"run_id": run_id, "run_dir": run_dir, "video_path": str(video_file), "ddd": tmp_path}

    def _make_uploader(self, calls_store: list) -> object:
        """Return a fake _upload callable that records calls and returns fake URLs."""
        counter = {"n": 0}

        def fake_upload(
            content, *, kind, title, base_url=None, token=None,
            run_id=None, narrative_slug=None, role=None, narrative_review_id=None,
            links=None,
        ):
            counter["n"] += 1
            url = f"https://canopy.test/w/fake-{kind}-{counter['n']}"
            calls_store.append({
                "kind": kind, "title": title, "content_len": len(content), "url": url,
                "run_id": run_id, "narrative_slug": narrative_slug, "role": role,
                "narrative_review_id": narrative_review_id, "links": links,
            })
            return url

        return fake_upload

    def _make_gate(self, choice: str):
        """Return a gate callable that returns *choice* immediately."""
        gate_calls: list[dict] = []

        def fake_gate(review_request, base_url, token):
            gate_calls.append({"gate": review_request.gate, "run_id": review_request.run_id})
            return choice

        fake_gate.calls = gate_calls
        return fake_gate

    def test_uploads_slideshow_deck_from_manifest(self, tmp_run, monkeypatch):
        """A run's role=deck slideshow is built directly from the render manifest
        (walkthrough-run-data.json), which _write_run_fixtures writes. The deck
        refactor removed the spec-rebuild-from-captured-frames path."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")

        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)

        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            release=False,  # stuck/review upload — no external_release gate
        )

        deck_calls = [c for c in upload_calls if c["role"] == "deck"]
        assert len(deck_calls) == 1, "a role=deck slideshow must be uploaded"
        deck = deck_calls[0]
        assert deck["kind"] == "html"
        assert deck["run_id"] == tmp_run["run_id"]
        assert deck["narrative_slug"] == "smart-routing"
        assert deck["content_len"] > 0
        # video + docs are still uploaded alongside the deck
        assert {"video", "html"} <= {c["kind"] for c in upload_calls}

    def test_missing_manifest_raises_deck_missing(self, tmp_run, monkeypatch):
        """A run with NO render manifest is a render gap — DeckMissingError,
        never a silent skip. (The deck must come from walkthrough-run-data.json.)"""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        (tmp_run["run_dir"] / "walkthrough-run-data.json").unlink()
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)

        with pytest.raises(DeckMissingError):
            upload_run(
                tmp_run["run_id"],
                video_path=tmp_run["video_path"],
                base_url="https://canopy.test",
                _upload=uploader,
                release=False,
            )

    def test_publish_returns_package_url(self, tmp_run, monkeypatch):
        """On publish we return the navigable run PACKAGE URL
        (/ddd/<narrative_slug>/<run_id>), NOT a loose /w/<artifact-id> link."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("publish")

        url = upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
        )

        assert url == "https://canopy.test/ddd/smart-routing/smart-routing-2026-01-01-001"
        assert "/w/" not in url, "must return the package URL, not a loose artifact link"
        # Both video and html must have been uploaded
        kinds = [c["kind"] for c in upload_calls]
        assert "video" in kinds
        assert "html" in kinds
        # The docs upload carries the external-systems links built from the
        # render manifest: the base app plus each scene's resolved url (the
        # fixture manifest gives one resolved url per scene).
        docs_call = next(c for c in upload_calls if c["role"] == "docs")
        assert docs_call["links"] == [
            {"label": "App", "url": "https://example.com", "kind": "reference"},
            {"label": "Scene 1", "url": "https://example.com/scene-1", "kind": "reference"},
            {"label": "Scene 2", "url": "https://example.com/scene-2", "kind": "reference"},
        ]

    def test_already_uploaded_is_noop(self, tmp_run, monkeypatch):
        """An uploaded run is immutable — re-calling upload_run returns the
        existing package URL without re-uploading or re-running the gate."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        from scripts.ddd.runstate import load, save

        state = load(tmp_run["run_id"])
        state.phase = "uploaded"
        save(state)

        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("publish")

        url = upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
        )

        assert url == "https://canopy.test/ddd/smart-routing/smart-routing-2026-01-01-001"
        assert upload_calls == [], "must not re-upload an already-uploaded run"
        assert gate.calls == [], "must not re-run the gate for an uploaded run"

    def test_publish_sets_phase_uploaded(self, tmp_run, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("publish")

        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
        )

        # Reload from disk and check phase
        import scripts.ddd.runstate as rs
        state = rs.load(tmp_run["run_id"])
        assert state.phase == "uploaded"

    def test_stuck_uploads_package_without_gate(self, tmp_run, monkeypatch):
        """A stuck/review upload (release=False) uploads the navigable package
        (video + docs) WITHOUT running the external_release gate."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("hold")  # would block a release; must be ignored

        url = upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
            release=False,
        )

        assert url == "https://canopy.test/ddd/smart-routing/smart-routing-2026-01-01-001"
        assert gate.calls == [], "stuck upload must NOT run the external_release gate"
        kinds = [c["kind"] for c in upload_calls]
        assert "video" in kinds and "html" in kinds, "package (video + docs) must still upload"

    def test_stuck_leaves_phase_iterable(self, tmp_run, monkeypatch):
        """A stuck/review upload must NOT mark the run terminal — phase stays
        unchanged so the run can keep iterating toward convergence."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        uploader = self._make_uploader([])
        gate = self._make_gate("hold")

        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
            release=False,
        )

        import scripts.ddd.runstate as rs
        assert rs.load(tmp_run["run_id"]).phase == "converged", "stuck upload must not set phase=uploaded"

    def test_hold_does_not_upload_html(self, tmp_run, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("hold")

        result = upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
        )

        assert result == ""
        # Video upload may still happen (gate is post-video), but HTML must NOT be uploaded
        html_uploads = [c for c in upload_calls if c["kind"] == "html"]
        assert html_uploads == [], "HTML must not be uploaded when gate returns 'hold'"

    def test_hold_leaves_phase_unchanged(self, tmp_run, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("hold")

        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
        )

        import scripts.ddd.runstate as rs
        state = rs.load(tmp_run["run_id"])
        assert state.phase == "converged", "Phase must stay 'converged' when gate returns 'hold'"

    def test_gate_receives_external_release_gate(self, tmp_run, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("publish")

        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
        )

        assert len(gate.calls) == 1
        assert gate.calls[0]["gate"] == "external_release"

    def test_video_url_embedded_in_docs_html(self, tmp_run, monkeypatch):
        """The HTML uploaded must contain the video URL returned by the video upload."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("publish")

        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
        )

        video_upload = next(c for c in upload_calls if c["kind"] == "video")
        html_upload = next(c for c in upload_calls if c["kind"] == "html")

        # DDD-run grouping: the run's artifacts carry run_id/narrative_slug + their role.
        assert video_upload["role"] == "hero_video"
        assert html_upload["role"] == "docs"
        assert video_upload["run_id"] == tmp_run["run_id"]
        assert html_upload["run_id"] == tmp_run["run_id"]
        assert video_upload["narrative_slug"] and html_upload["narrative_slug"]

        # The HTML content (bytes) should contain the video URL
        # upload content_len > 0 is checked; to check URL we need to capture bytes
        # Re-run with a more detailed mock to capture the HTML bytes. The first
        # call flipped phase→uploaded (now immutable), so reset it for this
        # capture re-run, which is a test mechanism, not real re-upload.
        from scripts.ddd.runstate import load, save

        st = load(tmp_run["run_id"])
        st.phase = "converged"
        save(st)

        html_bytes_store: list[bytes] = []

        def capturing_upload(
            content, *, kind, title, base_url=None, token=None,
            run_id=None, narrative_slug=None, role=None, narrative_review_id=None,
            links=None,
        ):
            if kind == "html":
                html_bytes_store.append(content if isinstance(content, bytes) else content.encode("utf-8"))
                return "https://canopy.test/w/html-99"
            return "https://canopy.test/w/video-88"

        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=capturing_upload,
            _gate=self._make_gate("publish"),
        )

        assert html_bytes_store, "HTML was not uploaded"
        html_content = html_bytes_store[0].decode("utf-8")
        # The video URL embedded in the HTML is whatever video upload returned
        assert "https://canopy.test/w/video-88" in html_content

    def test_auto_approve_bypasses_gate(self, tmp_run, monkeypatch):
        """auto_approve_for_test=True should skip _gate entirely."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)

        gate_called = []

        def should_not_be_called(*args, **kwargs):
            gate_called.append(True)
            return "hold"

        url = upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=should_not_be_called,
            auto_approve_for_test=True,
        )

        assert not gate_called, "Gate must not be called when auto_approve_for_test=True"
        assert url  # Should return the package URL

    def test_gate_run_id_matches(self, tmp_run, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("publish")

        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=uploader,
            _gate=gate,
        )

        assert gate.calls[0]["run_id"] == tmp_run["run_id"]

    # ------------------------------------------------------------------
    # Narrative guard — refuse to publish a run that has no narrative.
    # ------------------------------------------------------------------
    def _clear_narrative_stamp(self, run_id):
        from scripts.ddd.runstate import load, save

        st = load(run_id)
        st.narrative_review_id = None
        st.narrative_review_url = None
        save(st)

    def test_refuses_when_unstamped_and_no_server_narrative(self, tmp_run, monkeypatch):
        """The core guard: an unstamped run whose narrative_slug has no narrative on
        canopy-web must NOT publish — it would render as 'no narrative'."""
        from scripts.ddd.upload import NarrativeMissingError

        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        monkeypatch.delenv("DDD_ALLOW_NO_NARRATIVE", raising=False)
        self._clear_narrative_stamp(tmp_run["run_id"])

        upload_calls: list[dict] = []
        uploader = self._make_uploader(upload_calls)
        gate = self._make_gate("publish")

        with pytest.raises(NarrativeMissingError):
            upload_run(
                tmp_run["run_id"],
                video_path=tmp_run["video_path"],
                base_url="https://canopy.test",
                _upload=uploader,
                _gate=gate,
                _narrative_check=lambda *a, **k: False,
            )

        assert upload_calls == [], "must not upload anything when refusing"
        assert gate.calls == [], "must refuse before the external_release gate"

        # Phase must stay converged — a refused upload is not an upload.
        import scripts.ddd.runstate as rs
        assert rs.load(tmp_run["run_id"]).phase == "converged"

    def test_allows_when_unstamped_but_server_has_narrative(self, tmp_run, monkeypatch):
        """An unstamped run still publishes if canopy-web already has a
        narrative version for its narrative_slug (e.g. a legacy run, stamp lost)."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        self._clear_narrative_stamp(tmp_run["run_id"])

        upload_calls: list[dict] = []
        url = upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=self._make_uploader(upload_calls),
            _gate=self._make_gate("publish"),
            _narrative_check=lambda *a, **k: True,
        )

        assert url.endswith(f"/ddd/smart-routing/{tmp_run['run_id']}")
        assert any(c["kind"] == "html" for c in upload_calls)

    def test_stamped_run_skips_server_check(self, tmp_run, monkeypatch):
        """A stamped narrative_review_id is sufficient proof — the network check
        must not run, and the stamp flows through to the uploaded artifacts."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")

        def boom(*a, **k):
            raise AssertionError("narrative_check must not run when stamped")

        upload_calls: list[dict] = []
        url = upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=self._make_uploader(upload_calls),
            _gate=self._make_gate("publish"),
            _narrative_check=boom,
        )

        assert url
        stamp = "11111111-1111-1111-1111-111111111111"
        assert upload_calls and all(
            c["narrative_review_id"] == stamp for c in upload_calls
        ), "the stamped review id must be sent on every artifact"

    def test_env_override_bypasses_guard(self, tmp_run, monkeypatch):
        """DDD_ALLOW_NO_NARRATIVE=1 is the emergency escape hatch — it skips the
        check entirely (not even called) and publishes."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")
        monkeypatch.setenv("DDD_ALLOW_NO_NARRATIVE", "1")
        self._clear_narrative_stamp(tmp_run["run_id"])

        def boom(*a, **k):
            raise AssertionError("override must skip the check entirely")

        upload_calls: list[dict] = []
        url = upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=self._make_uploader(upload_calls),
            _gate=self._make_gate("publish"),
            _narrative_check=boom,
        )
        assert url


class TestReviewIdFromUrl:
    def test_extracts_uuid_from_review_url(self):
        from scripts.ddd.upload import _review_id_from_url

        uid = "09b3bd2a-1c51-4e8a-9430-ad991289539e"
        assert _review_id_from_url(f"https://canopy.test/review/{uid}/?t=abc") == uid
        assert _review_id_from_url(f"/review/{uid}/") == uid

    def test_none_and_unmatched(self):
        from scripts.ddd.upload import _review_id_from_url

        assert _review_id_from_url(None) is None
        assert _review_id_from_url("https://canopy.test/w/not-a-review") is None
