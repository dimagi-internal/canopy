"""upload_run builds the role=deck slideshow from the render manifest.

The deck is now read directly from walkthrough-run-data.json (the manifest the
engine emits) — there is no spec-rebuild fallback. A missing manifest or one
with zero scene slides is LOUD (DeckMissingError), never a silent skip.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml

from scripts.ddd.schemas.models import (
    Persona,
    RunState,
    Scene,
    SpineItem,
    UnifiedSpec,
    WhyBrief,
)
from scripts.ddd.upload import DeckMissingError, upload_run


def _make_spec() -> UnifiedSpec:
    return UnifiedSpec(
        name="Smart Routing",
        narrative="Route smarter, not harder.",
        base_url="https://labs",
        personas={
            "alice": Persona(
                name="Alice", role="Field worker", color="#2563eb",
                intro="Alice manages daily visits.",
            )
        },
        scenes=[
            Scene(
                persona="alice", title="Scene 1", show="Open Routes.",
                concept_claim="Instantly see your optimised route for the day.",
                provenance="SP1",
            ),
        ],
    )


def _make_why() -> WhyBrief:
    return WhyBrief(
        narrative_slug="Smart Routing",
        problem="Field workers navigate inefficiently for 40% of the day.",
        spine=[
            SpineItem(id="s1", claim="Routing saves 2h/day.",
                      rationale="Pilot with 20 workers showed 1.8h average saving."),
        ],
        gaps=[],
    )


def _write_run(tmp_ddd: Path, run_id: str) -> Path:
    run_dir = tmp_ddd / "runs" / run_id
    run_dir.mkdir(parents=True)
    state = RunState(
        run_id=run_id,
        narrative_slug="smart-routing",
        phase="converged",
        narrative_review_id="11111111-1111-1111-1111-111111111111",
    )
    (run_dir / "run_state.yaml").write_text(
        yaml.dump(state.model_dump(), default_flow_style=False, allow_unicode=True)
    )
    (run_dir / "unified_spec.yaml").write_text(
        yaml.dump(_make_spec().model_dump(), default_flow_style=False, allow_unicode=True)
    )
    (run_dir / "why_brief.yaml").write_text(
        yaml.dump(_make_why().model_dump(), default_flow_style=False, allow_unicode=True)
    )
    return run_dir


def _manifest(slides: list[dict]) -> dict:
    return {
        "name": "Smart Routing",
        "narrative": "Route smarter, not harder.",
        "generated_at": "2026-06-14T00:00:00Z",
        "duration_seconds": 5.0,
        "base_url": "https://labs",
        "scenes_run": [1],
        "scene_filter": None,
        "substitution_vars": {},
        "personas": {"alice": {"name": "Alice", "role": "Field worker", "color": "#2563eb", "intro": "x"}},
        "slides": slides,
    }


@pytest.fixture()
def tmp_run(tmp_path, monkeypatch):
    import scripts.ddd.runstate as rs
    import scripts.ddd.upload as pm

    # Patching runstate is enough — upload goes through runstate.run_dir_for.
    monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: tmp_path)
    assert pm.run_dir_for is rs.run_dir_for
    monkeypatch.setenv("CANOPY_WEB_PAT", "test-pat")

    run_id = "smart-routing-2026-01-01-001"
    run_dir = _write_run(tmp_path, run_id)
    video_file = tmp_path / "hero.mp4"
    video_file.write_bytes(b"\x00\x01\x02\x03")
    return {"run_id": run_id, "run_dir": run_dir, "video_path": str(video_file)}


def _uploader(calls: list):
    def fake_upload(content, *, kind, title, base_url=None, token=None,
                    run_id=None, narrative_slug=None, role=None,
                    narrative_review_id=None, links=None):
        calls.append({"kind": kind, "role": role, "run_id": run_id,
                      "narrative_slug": narrative_slug, "content_len": len(content)})
        return f"https://canopy.test/w/{role}-{kind}"
    return fake_upload


def test_deck_uploaded_from_manifest(tmp_run):
    """A run dir with walkthrough-run-data.json (one scene slide) yields a
    role=deck upload built from that manifest."""
    snaps = tmp_run["run_dir"] / "snapshots"
    snaps.mkdir()
    (snaps / "scene_1.png").write_bytes(b"\x89PNG-1")
    b64 = base64.b64encode(b"\x89PNG-1").decode()
    manifest = _manifest([
        {"type": "scene", "scene_index": 1, "scene_total": 1, "title": "Scene 1",
         "narration": "Open Routes.", "persona_key": "alice",
         "url": "https://labs/x", "urls_visited": ["https://labs/x"],
         "screenshot_path": "snapshots/scene_1.png", "page_text_path": None,
         "screenshot_b64": b64, "mp4_start_offset": 0.0, "ok": True, "ai_evaluation": None},
    ])
    (tmp_run["run_dir"] / "walkthrough-run-data.json").write_text(json.dumps(manifest))

    calls: list[dict] = []
    upload_run(
        tmp_run["run_id"],
        video_path=tmp_run["video_path"],
        base_url="https://canopy.test",
        _upload=_uploader(calls),
        release=False,
    )

    deck_calls = [c for c in calls if c["role"] == "deck"]
    assert len(deck_calls) == 1
    assert deck_calls[0]["kind"] == "html"
    assert deck_calls[0]["run_id"] == tmp_run["run_id"]
    assert deck_calls[0]["content_len"] > 0


def test_empty_manifest_slides_raises_deck_missing(tmp_run):
    """A manifest with no scene slides is a render gap — fail LOUD, not a
    silent skip."""
    (tmp_run["run_dir"] / "walkthrough-run-data.json").write_text(
        json.dumps(_manifest([]))
    )
    calls: list[dict] = []
    with pytest.raises(DeckMissingError):
        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=_uploader(calls),
            release=False,
        )


def test_absent_manifest_raises_deck_missing(tmp_run):
    """No walkthrough-run-data.json at all → DeckMissingError (re-render)."""
    calls: list[dict] = []
    with pytest.raises(DeckMissingError):
        upload_run(
            tmp_run["run_id"],
            video_path=tmp_run["video_path"],
            base_url="https://canopy.test",
            _upload=_uploader(calls),
            release=False,
        )
