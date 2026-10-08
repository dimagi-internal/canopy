"""Tests for scripts/ddd/findings_review.py (product-findings RUN-CHILD gate).

All HTTP calls are mocked — no real network traffic. The findings review is a
run-child (NOT a narrative version): the request carries no narrative_slug, and
each cluster's evidence carries an inline JPEG thumb + integer video_t.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
import yaml
from PIL import Image

from scripts.ddd.findings_review import (
    GATE,
    OVERALL_DECISION_ID,
    _clip_content_url,
    _format_ts,
    _summary_verdict,
    build_evidence,
    build_findings_review_request,
    cluster_findings,
    derive_severity,
    make_scene_resolver,
    make_thumb_resolver,
    parse_selection,
    resolve_review_mode,
    thumbnail_data_uri,
)
from scripts.ddd.schemas.models import ReviewRequest

DECK = "https://canopy-web.example/w/11111111-1111-1111-1111-111111111111?t=decktok"
CLIP = "https://canopy-web.example/w/22222222-2222-2222-2222-222222222222?t=cliptok"


def _finding(scene=2, dimension="visual_polish", severity="medium", detail="The hero chart is illegibly small.",
             fix="Make the chart the page hero.", fix_kind="mechanical", route="PRODUCT", **extra):
    f = {
        "scene": scene,
        "dimension": dimension,
        "severity": severity,
        "route": route,
        "detail": detail,
        "fix_recommendation": fix,
        "fix_kind": fix_kind,
    }
    f.update(extra)
    return f


SPEC = {
    "name": "verified-monitoring",
    "narrative": "x",
    "base_url": "https://labs.example",
    "personas": {},
    "scenes": [
        {"title": "Area Selection"},
        {"title": "Field Assignment"},
        {"title": "Review Dashboard"},
    ],
}


def _write_png(path: Path, *, size=(2, 2), color=(200, 100, 50)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# Scene resolution
# ---------------------------------------------------------------------------


def test_scene_resolver_handles_int_digit_title_and_scene_n():
    resolve = make_scene_resolver(SPEC)
    assert resolve(2) == 2
    assert resolve("3") == 3
    assert resolve("Field Assignment") == 2
    assert resolve("Scene 3") == 3
    assert resolve("Unknown Title") is None
    assert resolve(None) is None
    assert resolve("") is None


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def test_clusters_dedupe_by_scene_and_dimension():
    findings = [
        _finding(scene=2, dimension="visual_polish", detail="Chart too small."),
        _finding(scene=2, dimension="visual_polish", detail="Legend unreadable.", severity="high",
                 fix="Bigger legend.", fix_kind="options"),
        _finding(scene=3, dimension="visual_polish", detail="Table cramped."),
    ]
    clusters = cluster_findings(findings, scene_resolver=make_scene_resolver(SPEC))
    assert len(clusters) == 2
    merged = clusters[0]
    assert merged["cluster_id"] == "scene-2-visual-polish"
    assert merged["count"] == 2
    assert merged["scenes"] == [2]
    # Worst-of fix_kind merge: options beats mechanical.
    assert merged["fix_kind"] == "options"
    assert "Chart too small." in merged["detail"] and "Legend unreadable." in merged["detail"]
    assert "Bigger legend." in merged["suggested_fix"]


def test_explicit_cluster_key_groups_across_scenes():
    findings = [
        _finding(scene=1, cluster="picker UX"),
        _finding(scene=3, dimension="clarity", cluster="picker UX"),
    ]
    clusters = cluster_findings(findings, scene_resolver=make_scene_resolver(SPEC))
    assert len(clusters) == 1
    assert clusters[0]["cluster_id"] == "picker-ux"
    assert clusters[0]["scenes"] == [1, 3]


def test_non_product_routes_are_excluded():
    findings = [
        _finding(route="CONCEPT"),
        _finding(route="RESEARCH"),
        _finding(route="DEFER"),
        _finding(route="PRODUCT", scene=1),
    ]
    clusters = cluster_findings(findings, scene_resolver=make_scene_resolver(SPEC))
    assert len(clusters) == 1
    assert clusters[0]["scenes"] == [1]


def test_user_verdict_dimensions_become_clusters():
    user_verdict = {
        "dimensions": {
            "task_completion": {
                "score": 2,
                "weight": 0.4,
                "justification": "Ambiguous rows offer only Skip.",
                "fix_recommendation": "Add an inline picker per row.",
                "fix_kind": "mechanical",
            },
            "clarity": {"score": 5, "weight": 0.35, "justification": "fine"},
        }
    }
    clusters = cluster_findings([], user_verdict=user_verdict)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["cluster_id"] == "user-task-completion"
    assert c["fix_kind"] == "mechanical"
    assert c["scenes"] == []
    assert "inline picker" in c["suggested_fix"]


def test_title_resolved_scene_strings_cluster_with_indices():
    findings = [
        _finding(scene="Field Assignment", detail="Label confusing."),
        _finding(scene=2, detail="CTA hidden."),
    ]
    clusters = cluster_findings(findings, scene_resolver=make_scene_resolver(SPEC))
    assert len(clusters) == 1
    assert clusters[0]["scenes"] == [2]


# ---------------------------------------------------------------------------
# Severity derivation
# ---------------------------------------------------------------------------


def test_derive_severity_mapping():
    # redesign is always high.
    assert derive_severity(route="PRODUCT", fix_kind="redesign", score=5) == "high"
    # PRODUCT/CONCEPT on a failing iteration (score <= 2) is high.
    assert derive_severity(route="PRODUCT", fix_kind="mechanical", score=2) == "high"
    assert derive_severity(route="CONCEPT", fix_kind="options", score=1) == "high"
    # options on a non-failing iteration is medium.
    assert derive_severity(route="PRODUCT", fix_kind="options", score=4) == "medium"
    # mechanical on a non-failing iteration is low.
    assert derive_severity(route="PRODUCT", fix_kind="mechanical", score=4) == "low"
    # unknown score does not drag severity to high.
    assert derive_severity(route="PRODUCT", fix_kind="mechanical", score=None) == "low"


def test_finding_model_importable_from_both_homes():
    from scripts.ddd.schemas.models import Finding as FindingA
    from scripts.narrative.models import Finding as FindingB

    assert FindingA is FindingB
    f = FindingA(
        scene="2", dimension="visual_polish", route="PRODUCT",
        fix_kind="mechanical", severity="low", detail="x",
    )
    assert f.severity == "low" and f.fix_recommendation == ""


def test_judge_severity_is_authoritative_not_re_derived():
    """A finding's judge-set severity flows through unchanged.

    The concept judge owns severity (score<=1->high, ==2->medium, ==3->low).
    ``build_findings_review_request`` must NOT recompute it: a low-score PRODUCT
    finding the judge marked "low" stays "low" even though ``derive_severity``
    (mechanical on a failing score=2 iteration) would return "high".
    """
    findings = [
        _finding(scene=2, dimension="visual_polish", severity="low",
                 fix_kind="mechanical", route="PRODUCT"),
    ]
    req = build_findings_review_request(
        "verified-monitoring-2026-06-12-001",
        "verified-monitoring",
        3,
        SPEC,
        findings,
        None,
        DECK,
        CLIP,
        {2: 42.0},
        summary={"concept_score": 2, "user_score": 2, "verdict": "FAIL"},
    )
    by_id = {c["id"]: c for c in req.findings}
    # derive_severity would say "high" here (mechanical, failing); the judge said "low".
    assert by_id["scene-2-visual-polish"]["severity"] == "low"


def test_severity_falls_back_to_derive_when_finding_lacks_it():
    """When NO member of a cluster carried a severity, derive_severity fills in."""
    findings = [
        _finding(scene=2, dimension="visual_polish", fix_kind="options", route="PRODUCT"),
    ]
    # Strip the severity the helper sets by default — simulate a judge that
    # emitted a finding with no severity.
    findings[0].pop("severity")
    req = build_findings_review_request(
        "verified-monitoring-2026-06-12-001",
        "verified-monitoring",
        3,
        SPEC,
        findings,
        None,
        DECK,
        CLIP,
        {2: 42.0},
        summary={"concept_score": 5, "user_score": 5, "verdict": "PASS"},
    )
    by_id = {c["id"]: c for c in req.findings}
    # options on a non-failing iteration → derive_severity returns "medium".
    assert by_id["scene-2-visual-polish"]["severity"] == "medium"


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


def test_thumbnail_data_uri_from_png(tmp_path):
    png = _write_png(tmp_path / "scene_2.png", size=(960, 540))
    uri = thumbnail_data_uri(png, width=480, quality=70)
    assert uri.startswith("data:image/jpeg;base64,")
    # Decodes to a real JPEG no wider than the requested width.
    raw = base64.b64decode(uri.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as im:
        assert im.format == "JPEG"
        assert im.width <= 480


def test_thumbnail_missing_file_returns_none(tmp_path):
    assert thumbnail_data_uri(tmp_path / "nope.png") is None


def test_make_thumb_resolver_prefers_iter_dir_then_flat(tmp_path):
    run_dir = tmp_path / "run"
    # Contract path: snapshots_iter<N>/scene_<N>.png.
    _write_png(run_dir / "snapshots_iter3" / "scene_2.png")
    # Flat fallback path also exists for a different scene.
    _write_png(run_dir / "snapshots" / "scene_5.png")
    resolve = make_thumb_resolver(run_dir, 3)
    assert resolve(2).startswith("data:image/jpeg;base64,")  # from iter dir
    assert resolve(5).startswith("data:image/jpeg;base64,")  # from flat fallback
    assert resolve(9) is None  # neither exists


# ---------------------------------------------------------------------------
# Evidence (contract shape)
# ---------------------------------------------------------------------------


def test_build_evidence_contract_shape():
    cluster = {"scenes": [2]}
    resolver = lambda n: "data:image/jpeg;base64,AAAA"  # noqa: E731
    ev = build_evidence(cluster, scene_timestamps={2: 83.9}, thumb_resolver=resolver)
    assert ev == [
        {
            "scene": 2,
            "deck_anchor": "#scene-2",
            "thumb": "data:image/jpeg;base64,AAAA",
            "video_t": 83,  # int(start_seconds)
        }
    ]


def test_build_evidence_omits_video_t_for_untimed_scene():
    cluster = {"scenes": [4]}
    ev = build_evidence(cluster, scene_timestamps={2: 10.0}, thumb_resolver=lambda n: None)
    assert ev == [{"scene": 4, "deck_anchor": "#scene-4"}]


def test_build_evidence_empty_for_sceneless_cluster():
    assert build_evidence({"scenes": []}, scene_timestamps={}, thumb_resolver=None) == []


def test_format_ts():
    assert _format_ts(0) == "0:00"
    assert _format_ts(83.9) == "1:23"
    assert _format_ts(615) == "10:15"


def test_clip_content_url_derivation():
    assert (
        _clip_content_url(CLIP)
        == "https://canopy-web.example/w/22222222-2222-2222-2222-222222222222/content?t=cliptok"
    )
    assert _clip_content_url("https://elsewhere.example/video.mp4") is None
    assert _clip_content_url(None) is None


def test_summary_verdict_weakest_link():
    assert _summary_verdict(2, 5) == "FAIL"
    assert _summary_verdict(3, 4) == "WARN"
    assert _summary_verdict(4, 5) == "PASS"
    assert _summary_verdict(None, None) == "UNKNOWN"


# ---------------------------------------------------------------------------
# Request building (RUN-CHILD contract)
# ---------------------------------------------------------------------------


def _build_request(thumb_resolver=None) -> ReviewRequest:
    findings = [
        _finding(scene=2, dimension="visual_polish"),
        _finding(scene=3, dimension="clarity", detail="Jargon in the table header.",
                 fix="Rename the header.", fix_kind="options"),
    ]
    user_verdict = {
        "dimensions": {
            "trust": {
                "score": 3,
                "weight": 0.25,
                "justification": "Data freshness unclear.",
                "fix_recommendation": "Show a last-updated stamp.",
                "fix_kind": "mechanical",
            }
        }
    }
    return build_findings_review_request(
        "verified-monitoring-2026-06-12-001",
        "verified-monitoring",
        3,
        SPEC,
        findings,
        user_verdict,
        DECK,
        CLIP,
        {2: 42.0, 3: 81.5},
        summary={"concept_score": 2, "user_score": 2, "verdict": "FAIL"},
        thumb_resolver=thumb_resolver or (lambda n: f"data:image/jpeg;base64,SCENE{n}"),
    )


def test_request_is_run_child_no_narrative_slug():
    req = _build_request()
    payload = req.model_dump(by_alias=True)
    assert payload["gate"] == GATE
    # RUN-CHILD: narrative_slug must be empty (canopy-web pins it to None).
    assert payload.get("narrative_slug", "") == ""
    assert payload["feature"] == "verified-monitoring"
    assert payload["iteration"] == 3
    assert payload["deck_url"] == DECK
    assert payload["summary"] == {"concept_score": 2, "user_score": 2, "verdict": "FAIL"}
    # Embedded video points at the streamable content URL, not the page route.
    assert payload["video"]["url"].endswith("/content?t=cliptok")


def test_request_top_level_keys_match_contract():
    req = _build_request()
    payload = req.model_dump(by_alias=True)
    for key in ("run_id", "gate", "feature", "iteration", "video", "deck_url", "summary"):
        assert key in payload, key
    # Clusters in the contract shape (serialized under the contract key "clusters").
    assert "findings" not in payload and "clusters" in payload
    for cluster in payload["clusters"]:
        assert set(cluster) >= {
            "id", "title", "severity", "fix_kind", "route", "scenes",
            "suggested_fix", "count", "evidence",
        }
        assert cluster["route"] == "PRODUCT"
        assert cluster["severity"] in ("high", "medium", "low")


def test_request_evidence_has_thumb_anchor_and_video_t():
    req = _build_request()
    by_id = {c["id"]: c for c in req.findings}
    ev = by_id["scene-2-visual-polish"]["evidence"]
    assert ev[0]["scene"] == 2
    assert ev[0]["deck_anchor"] == "#scene-2"
    assert ev[0]["thumb"].startswith("data:image/jpeg;base64,")
    assert ev[0]["video_t"] == 42 and isinstance(ev[0]["video_t"], int)
    ev3 = by_id["scene-3-clarity"]["evidence"]
    assert ev3[0]["video_t"] == 81


def test_request_uses_judge_severity_not_re_derived():
    # The judge owns severity: the findings carried "medium", so the clusters
    # stay "medium" even on a failing iteration where derive_severity would
    # otherwise compute "high". (Severity source = judge, not re-derived.)
    req = _build_request()
    by_id = {c["id"]: c for c in req.findings}
    assert by_id["scene-2-visual-polish"]["severity"] == "medium"  # judge-set, preserved
    assert by_id["scene-3-clarity"]["severity"] == "medium"  # judge-set, preserved


def test_request_decisions_one_per_cluster_plus_overall():
    req = _build_request()
    ids = [d.id for d in req.decisions]
    assert ids[-1] == OVERALL_DECISION_ID
    assert set(ids[:-1]) == {"scene-2-visual-polish", "scene-3-clarity", "user-trust"}
    for d in req.decisions[:-1]:
        assert d.options == ["implement", "skip", "defer"]
        assert d.recommended == "implement"
        assert d.class_ == GATE
    overall = req.decisions[-1]
    assert overall.options == ["proceed with selected", "discuss"]


def test_request_serializes_decisions_with_class_alias():
    req = _build_request()
    payload = req.model_dump(by_alias=True)
    assert all("class" in d for d in payload["decisions"])


def test_no_product_findings_yields_empty_findings_list():
    req = build_findings_review_request(
        "x-2026-06-12-001", "x", 1, SPEC, [_finding(route="CONCEPT")], None, DECK, CLIP, {}
    )
    assert req.findings == []
    assert [d.id for d in req.decisions] == [OVERALL_DECISION_ID]


def test_request_from_tiny_fixture_design_findings_and_png(tmp_path):
    """End-to-end build from a tiny on-disk design_findings + a 2x2 png.

    Asserts the contract invariants: gate, no narrative_slug, thumb data-URI,
    int video_t.
    """
    run_dir = tmp_path / "run"
    _write_png(run_dir / "snapshots_iter1" / "scene_2.png")  # the 2x2 png
    design_findings = [
        {
            "scene": 2,
            "dimension": "visual_polish",
            "severity": "medium",
            "route": "PRODUCT",
            "detail": "The button overflows the card.",
            "fix_recommendation": "Constrain the button width.",
            "fix_kind": "mechanical",
        }
    ]
    (run_dir / "design_findings.json").write_text(json.dumps(design_findings))

    findings = json.loads((run_dir / "design_findings.json").read_text())
    req = build_findings_review_request(
        "verified-monitoring-2026-06-12-001",
        "verified-monitoring",
        1,
        SPEC,
        findings,
        None,
        DECK,
        CLIP,
        {2: 12.0},
        summary={"concept_score": 3, "user_score": 4, "verdict": "WARN"},
        thumb_resolver=make_thumb_resolver(run_dir, 1),
    )
    payload = req.model_dump(by_alias=True)
    assert payload["gate"] == "product_findings"
    assert "narrative_slug" not in payload or payload["narrative_slug"] == ""
    cluster = payload["clusters"][0]
    assert cluster["evidence"][0]["thumb"].startswith("data:image/jpeg;base64,")
    vt = cluster["evidence"][0]["video_t"]
    assert isinstance(vt, int) and vt == 12


# ---------------------------------------------------------------------------
# Selection parsing (apply) — contract response_json shape
# ---------------------------------------------------------------------------


def test_parse_selection_contract_shape():
    response = {
        "decisions": {
            "scene-2-visual-polish": {"decision": "implement", "comment": "hoist it"},
            "scene-3-clarity": {"decision": "skip", "comment": ""},
            "user-trust": {"decision": None, "comment": "look into this more"},
        }
    }
    sel = parse_selection(response)
    assert sel["implement"] == ["scene-2-visual-polish"]
    assert sel["skip"] == ["scene-3-clarity"]
    # decision=null + a comment is guidance to address, never an auto-skip.
    assert "user-trust" not in sel["implement"] + sel["skip"]
    assert sel["commented"] == ["scene-2-visual-polish", "user-trust"]
    assert sel["comments"]["user-trust"] == "look into this more"
    assert {"cluster_id": "scene-2-visual-polish", "decision": "implement", "comment": "hoist it"} in sel[
        "selections"
    ]


def test_parse_selection_legacy_flat_decision_still_bucketed():
    # A pre-redesign flat value (the decision string itself) still buckets.
    sel = parse_selection({"decisions": {"scene-2-visual-polish": "implement"}})
    assert sel["implement"] == ["scene-2-visual-polish"]
    assert sel["comments"] == {}


def test_parse_selection_empty_decisions_is_a_valid_no_touch():
    # The reviewer touched no finding: a present-but-empty decisions object.
    sel = parse_selection({"decisions": {}})
    assert sel == {
        "selections": [],
        "implement": [],
        "skip": [],
        "defer": [],
        "commented": [],
        "comments": {},
        "overall": None,
    }


@pytest.mark.parametrize(
    "response",
    [
        {},  # missing decisions entirely
        {"decision": "implement"},  # wrong shape: top-level decision
        {"decisions": None},
        {"decisions": ["scene-2-visual-polish"]},
        {"decisions": {"scene-2-visual-polish": {"decision": "approve"}}},  # unknown
        {"decisions": {"scene-2-visual-polish": "yes"}},  # unknown (legacy flat)
        {"decisions": {"scene-2-visual-polish": ["implement"]}},  # malformed entry
        {"decisions": {"scene-2-visual-polish": {"decision": "implement", "comment": 3}}},
        {"decisions": {"findings-verdict": "ship it"}},  # unknown overall
        "implement",
    ],
)
def test_parse_selection_fails_closed(response):
    from scripts.ddd.review import InvalidReviewResponse

    with pytest.raises(InvalidReviewResponse, match="'implement', 'skip', 'defer'"):
        parse_selection(response)


def test_parse_selection_defer_and_overall_are_reported():
    sel = parse_selection(
        {
            "decisions": {
                "scene-2-visual-polish": {"decision": "defer", "comment": ""},
                "findings-verdict": "discuss",
            }
        }
    )
    assert sel["defer"] == ["scene-2-visual-polish"]
    assert sel["implement"] == [] and sel["skip"] == []
    assert sel["overall"] == "discuss"
    # The overall verdict is not mistaken for a cluster.
    assert [x["cluster_id"] for x in sel["selections"]] == ["scene-2-visual-polish"]


@pytest.mark.parametrize(
    "response",
    [{}, {"decisions": {"scene-2-visual-polish": {"decision": "approve"}}}],
)
def test_cli_apply_refuses_without_a_selection(tmp_path, capsys, response):
    from scripts.ddd import findings_review as fr

    path = tmp_path / "response.json"
    path.write_text(json.dumps(response))
    with pytest.raises(SystemExit) as exc:
        fr.main(["apply", str(path)])
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert out.out == ""  # no selection JSON for a caller to act on
    assert "refusing to apply" in out.err
    assert "Expected shape" in out.err


# ---------------------------------------------------------------------------
# Review mode resolution
# ---------------------------------------------------------------------------


def test_review_mode_defaults_to_autonomous(tmp_path):
    assert resolve_review_mode({}) == "autonomous"
    assert resolve_review_mode({"review_mode": "bogus"}) == "autonomous"
    assert resolve_review_mode(tmp_path / "missing.yaml") == "autonomous"


def test_review_mode_human_from_spec_file(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.dump({**SPEC, "review_mode": "human"}))
    assert resolve_review_mode(spec_path) == "human"
    assert resolve_review_mode({"review_mode": "human"}) == "human"


def test_review_mode_validates_on_unified_spec():
    from scripts.narrative.models import UnifiedSpec

    spec = UnifiedSpec.model_validate({**SPEC, "review_mode": "human", "scenes": []})
    assert resolve_review_mode(spec) == "human"
    default = UnifiedSpec.model_validate({**SPEC, "scenes": []})
    assert default.review_mode == "autonomous"


# ---------------------------------------------------------------------------
# CLI post — stamps run_state (mocked HTTP), emits run-child payload
# ---------------------------------------------------------------------------


def _seed_run(tmp_path, monkeypatch, *, with_findings=True) -> tuple[Path, str]:
    """Create a judged run dir under a tmp ddd dir; returns (run_dir, run_id)."""
    ddd_dir = tmp_path / ".canopy" / "ddd"
    runs = ddd_dir / "runs"
    run_id = "verified-monitoring-2026-06-12-001"
    run_dir = runs / run_id
    run_dir.mkdir(parents=True)

    import scripts.ddd.runstate as rs

    monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: ddd_dir)

    state = {
        "schema_version": 1,
        "run_id": run_id,
        "narrative_slug": "verified-monitoring",
        "phase": "judged",
        "iteration": 1,
        "iteration_decks": {1: DECK},
        "iteration_clips": {1: CLIP},
    }
    (run_dir / "run_state.yaml").write_text(yaml.dump(state))

    findings = [_finding(scene=2)] if with_findings else []
    (run_dir / "design_findings.json").write_text(json.dumps(findings))
    (run_dir / "verdict-user.yaml").write_text(
        yaml.dump({"overall_score": 2, "dimensions": {"clarity": {"score": 2, "weight": 0.35}}})
    )
    (run_dir / "verdict-concept.yaml").write_text(yaml.dump({"overall_score": 2}))
    (run_dir / "run-report.json").write_text(
        json.dumps({"scenes": [{"scene_index": 2, "title": "S2", "start_seconds": 42.0,
                                "duration_seconds": 5.0}]})
    )
    # Snapshot for the thumbnail (flat fallback path).
    _write_png(run_dir / "snapshots" / "scene_2.png")
    return run_dir, run_id


def test_cli_post_stamps_run_state_and_emits_run_child(tmp_path, monkeypatch, capsys):
    run_dir, run_id = _seed_run(tmp_path, monkeypatch)

    import scripts.ddd.review as rv
    from scripts.ddd import findings_review as fr

    posted: dict = {}

    def fake_post(request, **kwargs):
        posted["request"] = request
        return {
            "id": "rev-123",
            "url": "/review/rev-123/?t=sharetok",
            "share_token": "sharetok",
            "workspace": "connect",
        }

    monkeypatch.setattr(rv, "post_review_request", fake_post)
    # Keep the spec resolution off the real repo's docs/walkthroughs.
    monkeypatch.setattr(fr, "_default_spec_path", lambda slug: None)

    fr.main(["post", run_id])

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["posted"] is True
    assert out["id"] == "rev-123"
    assert out["clusters"] == 1
    assert out["internal_url"].endswith("/w/connect/review/rev-123")
    assert "/w/connect/review/rev-123/" in out["share_url"]
    assert "t=sharetok" in out["share_url"]
    # The server's raw (flat, relative) url is replaced, never echoed.
    assert out["url"] == out["share_url"]

    # The posted request is a run-child carrying inline-thumb evidence.
    req = posted["request"]
    payload = req.model_dump(by_alias=True)
    assert payload["gate"] == GATE
    assert payload.get("narrative_slug", "") == ""
    assert payload["feature"] == "verified-monitoring"
    assert payload["iteration"] == 1
    assert payload["deck_url"] == DECK
    assert payload["summary"]["verdict"] == "FAIL"  # both judges scored 2
    ev = payload["clusters"][0]["evidence"][0]
    assert ev["deck_anchor"] == "#scene-2"
    assert ev["video_t"] == 42
    assert ev["thumb"].startswith("data:image/jpeg;base64,")

    # run_state stamped with the review id + tokenized URL.
    raw = yaml.safe_load((run_dir / "run_state.yaml").read_text())
    assert raw["findings_review_id"] == "rev-123"
    assert "t=sharetok" in raw["findings_review_url"]


def test_cli_post_no_product_findings_is_clean_noop(tmp_path, monkeypatch, capsys):
    run_dir, run_id = _seed_run(tmp_path, monkeypatch, with_findings=False)

    import scripts.ddd.review as rv
    from scripts.ddd import findings_review as fr

    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("post_review_request must not be called")

    monkeypatch.setattr(rv, "post_review_request", boom)
    monkeypatch.setattr(fr, "_default_spec_path", lambda slug: None)

    fr.main(["post", run_id])

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out == {
        "posted": False,
        "reason": "no PRODUCT findings to review",
        "run_id": run_id,
    }
    raw = yaml.safe_load((run_dir / "run_state.yaml").read_text())
    assert raw.get("findings_review_id") is None


def test_cli_apply_prints_selection(tmp_path, capsys):
    from scripts.ddd import findings_review as fr

    response = tmp_path / "response.json"
    response.write_text(
        json.dumps(
            {
                "decisions": {
                    "scene-2-visual-polish": {"decision": "implement", "comment": "hoist it"},
                }
            }
        )
    )
    fr.main(["apply", str(response)])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["implement"] == ["scene-2-visual-polish"]
    assert out["comments"]["scene-2-visual-polish"] == "hoist it"


def test_cli_mode_prints_review_mode(tmp_path, capsys):
    from scripts.ddd import findings_review as fr

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.dump({**SPEC, "review_mode": "human"}))
    fr.main(["mode", str(spec_path)])
    assert capsys.readouterr().out.strip() == "human"
