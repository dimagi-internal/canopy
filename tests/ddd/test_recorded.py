"""`style: recorded` — N short standalone cuts, no gloss.

Pins the contract in scripts.ddd.recorded and its consumers:
  * cuts resolve to scenes (and a broken cut list refuses loudly);
  * each cut's first line is "This is a quick overview of how we <do X>.";
  * the 65–80 word warning band and the 30 s target / 40 s ceiling;
  * the emitter writes ONE connect-ddd-walkthrough spec per cut, with no title
    card, no end card, no lower-thirds, and `style: recorded`;
  * spec_qa waives the single-opening-overview rule for recorded specs but
    fails one whose cut opens wrong;
  * a spec that never mentions `style` behaves exactly as before.
"""
from __future__ import annotations

import json

import pytest
import yaml

from scripts.ddd import recorded as rec
from scripts.ddd.schemas.models import UnifiedSpec
from scripts.ddd.snippets import (
    build_explainer_spec,
    build_recorded_cut_specs,
    emit_explainer_from_capture,
)

# ~70 words each, first person, opener first.
CUT1_A = (
    "This is a quick overview of how we assign chlorine dispensers to water points. "
    "I start on the dispenser list for the district, where every row is a dispenser "
    "with its last refill date."
)
CUT1_B = (
    "I pick the one at Kibera Road, choose the water point from the map, and save. "
    "The row now shows the new water point and today's date, so the field team "
    "knows where to go."
)
CUT2 = (
    "This is a quick overview of how we check that a dispenser was refilled. "
    "I open the refill visits for this week. Each visit has a photo of the dispenser "
    "and the chlorine reading the worker took. I click the visit from Kibera Road, "
    "look at the photo, and the reading sits inside the safe range, so I mark it "
    "verified and it moves to the paid column."
)


def _scene(sid: str, narrative, *, role: str = "demo") -> dict:
    return {
        "id": sid,
        "persona": "amina",
        "title": sid.replace("-", " ").title(),
        "show": "the dispenser list",
        "concept_claim": "A program manager assigns a dispenser to a water point and sees the row update.",
        "provenance": "S1",
        "role": role,
        "narrative": narrative,
        "features": [],
    }


def _spec(**over) -> dict:
    spec = {
        "name": "chlorine-dispensers",
        "narrative": "How the chlorine dispenser program is run.",
        "base_url": "https://labs.connect.dimagi.com",
        "personas": {"amina": {"name": "Amina", "role": "Program manager", "color": "#0a6", "intro": "Amina runs the district program."}},
        "style": "recorded",
        "scenes": [
            _scene("dispenser-list", CUT1_A),
            _scene("assign", CUT1_B),
            _scene("refill-check", CUT2),
            _scene("unused", "A scene recorded but in no cut."),
        ],
        "cuts": [
            {"id": "assign", "title": "Assigning a dispenser", "scenes": ["dispenser-list", "assign"]},
            {"id": "verify", "title": "Verifying a refill", "scenes": ["refill-check"]},
        ],
    }
    spec.update(over)
    return spec


REPORT = {
    "scenes": [
        {"scene_index": 1, "start_seconds": 0.0, "duration_seconds": 12.0},
        {"scene_index": 2, "start_seconds": 12.0, "duration_seconds": 14.0},
        {"scene_index": 3, "start_seconds": 26.0, "duration_seconds": 25.0},
        {"scene_index": 4, "start_seconds": 51.0, "duration_seconds": 5.0},
    ]
}


# --------------------------------------------------------------------------- #
# style detection / model
# --------------------------------------------------------------------------- #


def test_absent_style_is_explainer():
    assert rec.spec_style({}) == "explainer"
    assert rec.is_recorded({"style": "recorded"})
    assert not rec.is_recorded({"style": "explainer"})
    assert rec.lint_recorded_spec({"scenes": []}) == []


def test_unified_spec_parses_style_and_cuts():
    spec = UnifiedSpec(**_spec())
    assert spec.style == "recorded"
    assert [c.id for c in spec.cuts] == ["assign", "verify"]
    assert spec.cuts[0].scenes == ["dispenser-list", "assign"]
    legacy = UnifiedSpec(**{k: v for k, v in _spec().items() if k not in ("style", "cuts")})
    assert legacy.style == "explainer" and legacy.cuts == []


def test_unknown_style_is_rejected_by_the_model():
    with pytest.raises(Exception):
        UnifiedSpec(**_spec(style="glossy"))


# --------------------------------------------------------------------------- #
# cuts
# --------------------------------------------------------------------------- #


def test_resolve_cuts_maps_scene_ids_to_report_indexes():
    cuts = rec.resolve_cuts(_spec())
    assert [c["id"] for c in cuts] == ["assign", "verify"]
    assert cuts[0]["scene_indexes"] == [1, 2]
    assert cuts[1]["scene_indexes"] == [3]


@pytest.mark.parametrize(
    "cuts, needle",
    [
        ([], "non-empty `cuts:`"),
        ([{"id": "a", "scenes": []}], "lists no scenes"),
        ([{"id": "a", "scenes": ["nope"]}], "unknown scene id 'nope'"),
        ([{"id": "a", "scenes": ["assign"]}, {"id": "a", "scenes": ["refill-check"]}], "used twice"),
        ([{"id": "a", "scenes": ["assign"]}, {"id": "b", "scenes": ["assign"]}], "each cut stands alone"),
    ],
)
def test_broken_cut_lists_refuse(cuts, needle):
    with pytest.raises(rec.RecordedSpecError, match=needle.replace("`", ".").replace("(", ".")):
        rec.resolve_cuts(_spec(cuts=cuts))


# --------------------------------------------------------------------------- #
# opener + word band + estimated length
# --------------------------------------------------------------------------- #


def test_opener_ok():
    assert rec.opener_ok(CUT2)
    assert rec.opener_ok("this is a quick  overview of how we pay workers. More.")
    assert rec.opener_ok("This is a quick overview of how we pay workers\u2019 stipends.")
    assert not rec.opener_ok("Here is how we pay workers.")
    assert not rec.opener_ok("This is a quick overview of how we.")
    assert not rec.opener_ok("Welcome! This is a quick overview of how we pay workers.")


def test_a_well_formed_spec_lints_clean():
    assert [i for i in rec.lint_recorded_spec(_spec()) if i["level"] == "error"] == []


def test_a_cut_that_does_not_open_with_the_line_is_an_error():
    spec = _spec()
    spec["scenes"][2]["narrative"] = "Now let's look at refills. " + CUT2
    errors = [i for i in rec.lint_recorded_spec(spec) if i["level"] == "error"]
    assert len(errors) == 1 and errors[0]["cut"] == "verify"
    assert "This is a quick overview of how we" in errors[0]["message"]


def test_word_band_is_a_warning_not_an_error():
    spec = _spec()
    spec["scenes"][2]["narrative"] = "This is a quick overview of how we verify refills. I open one and approve it."
    issues = [i for i in rec.lint_recorded_spec(spec) if i["cut"] == "verify"]
    assert [i["level"] for i in issues] == ["warn"]
    assert "aim for 65–80" in issues[0]["message"]


def test_voiceover_estimate_over_target_warns_and_over_ceiling_fails():
    spec = _spec()
    opener = "This is a quick overview of how we verify refills. "
    spec["scenes"][2]["narrative"] = opener + " ".join(["word"] * 80)  # 88 words, ~34s
    verify = [i for i in rec.lint_recorded_spec(spec) if i["cut"] == "verify"]
    assert {i["level"] for i in verify} == {"warn"}
    assert any("over the 30s target" in i["message"] for i in verify)

    spec["scenes"][2]["narrative"] = opener + " ".join(["word"] * 110)  # ~45s
    errors = [i for i in rec.lint_recorded_spec(spec) if i["level"] == "error"]
    assert any("over the 40s ceiling" in i["message"] for i in errors)


def test_list_narratives_count_every_part():
    spec = _spec()
    spec["scenes"][2]["narrative"] = [CUT2.split(". ", 1)[0] + ".", CUT2.split(". ", 1)[1]]
    cut = rec.resolve_cuts(spec)[1]
    assert rec.word_count(rec.cut_narration(spec, cut)) == rec.word_count(CUT2)


# --------------------------------------------------------------------------- #
# post-render timing gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "secs, verdict",
    [(12.0, "pass"), (30.0, "pass"), (30.01, "warn"), (40.0, "warn"), (40.1, "fail")],
)
def test_classify_duration(secs, verdict):
    assert rec.classify_duration(secs) == verdict


def test_gate_takes_the_worst_cut():
    r = rec.gate_cut_durations({"a": 28.0, "b": 35.0})
    assert r["verdict"] == "warn"
    assert [c["verdict"] for c in r["cuts"]] == ["pass", "warn"]
    r = rec.gate_cut_durations({"a": 28.0, "b": 41.5})
    assert r["verdict"] == "fail"
    assert any("41.5s is over the 40s ceiling" in f for f in r["findings"])


def test_gate_fails_an_unmeasured_cut_and_an_empty_set():
    assert rec.gate_cut_durations({"a": None})["verdict"] == "fail"
    assert rec.gate_cut_durations({})["verdict"] == "fail"


def test_gate_word_band_only_warns():
    r = rec.gate_cut_durations({"a": 25.0}, {"a": 50})
    assert r["verdict"] == "warn" and r["cuts"][0]["words"] == 50
    r = rec.gate_cut_durations({"a": 25.0}, {"a": 72})
    assert r["verdict"] == "pass"


def test_gate_cli_probes_and_writes_the_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "probe_duration", lambda p: {"a.mp4": 29.0, "b.mp4": 44.0}[str(p)])
    out = tmp_path / "verdict.json"
    code = rec.main(["gate", "--cut", "assign=a.mp4", "--cut", "verify=b.mp4", "--out", str(out)])
    assert code == 1
    data = json.loads(out.read_text())
    assert data["verdict"] == "fail"
    assert [c["cut"] for c in data["cuts"]] == ["assign", "verify"]


def test_lint_cli_exit_codes(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump(_spec()))
    assert rec.main(["lint", str(good)]) == 0
    bad_spec = _spec()
    bad_spec["scenes"][0]["narrative"] = "Hello there. " + CUT1_A
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(bad_spec))
    assert rec.main(["lint", str(bad)]) == 1


# --------------------------------------------------------------------------- #
# emitter: one spec per cut, no gloss
# --------------------------------------------------------------------------- #


def _manifest():
    from scripts.ddd.snippets import build_snippets

    spec = _spec()
    return {
        "narrative_slug": spec["name"],
        "run_id": None,
        "name": spec["name"],
        "snippets": build_snippets(
            narrative_slug=spec["name"], spec=spec, report=REPORT,
            source_clip_local=None, source_clip_hosted=None,
        ),
    }


def test_build_recorded_cut_specs_has_no_cards_no_overlays():
    specs = build_recorded_cut_specs(
        _manifest(), rec.resolve_cuts(_spec()), workspace="dimagi-team",
        master_ref=None, base_url="https://labs.connect.dimagi.com/", country_focus="Kenya",
    )
    assert list(specs) == ["assign", "verify"]
    for cid, s in specs.items():
        assert s["style"] == "recorded"
        assert s["slug"] == f"chlorine-dispensers-{cid}"
        kinds = {b["kind"] for b in s["beats"]}
        assert kinds == {"body_walkthrough"}, "no intro_title / outro_card in a recorded cut"
        assert "title" not in s["narration"]["by_beat"] and "outro" not in s["narration"]["by_beat"]
        assert all(w["lower_third"] == "" for w in s["walkthrough"].values())
    assign = specs["assign"]
    assert [b["id"] for b in assign["beats"]] == ["s1", "s2"]
    assert assign["narration"]["by_beat"]["s1"].startswith("This is a quick overview of how we")
    assert assign["manifest"]["master"] == "file:assets/programs/chlorine-dispensers-assign/walkthrough.mp4"
    # the unused scene (index 4) is in no cut
    assert all("s4" not in s["walkthrough"] for s in specs.values())


def test_overview_scene_plays_as_a_beat_not_a_title_card():
    spec = _spec()
    spec["scenes"][0]["role"] = "overview"
    manifest = {
        "narrative_slug": spec["name"], "run_id": None, "name": spec["name"],
        "snippets": __import__("scripts.ddd.snippets", fromlist=["build_snippets"]).build_snippets(
            narrative_slug=spec["name"], spec=spec, report=REPORT,
            source_clip_local=None, source_clip_hosted=None,
        ),
    }
    specs = build_recorded_cut_specs(
        manifest, rec.resolve_cuts(spec), workspace="w", master_ref="file:m.mp4",
        base_url="https://x/", country_focus="Kenya",
    )
    assert [b["id"] for b in specs["assign"]["beats"]] == ["s1", "s2"]


def test_a_cut_with_no_footage_refuses():
    manifest = _manifest()
    manifest["snippets"] = [s for s in manifest["snippets"] if s["scene_index"] != 3]
    with pytest.raises(ValueError, match="verify"):
        build_recorded_cut_specs(
            manifest, rec.resolve_cuts(_spec()), workspace="w", master_ref=None,
            base_url="https://x/", country_focus="Kenya",
        )


def test_explainer_from_capture_writes_one_spec_per_cut(tmp_path):
    (tmp_path / "chlorine.yaml").write_text(yaml.safe_dump(_spec()))
    (tmp_path / "report.json").write_text(json.dumps(REPORT))
    result = emit_explainer_from_capture(
        str(tmp_path / "chlorine.yaml"), str(tmp_path / "report.json"),
        out_path=str(tmp_path / "out" / "explainer_spec.yaml"),
    )
    assert result["style"] == "recorded"
    written = result["_written_to"]
    assert set(written) == {"assign", "verify"}
    for cid, path in written.items():
        doc = yaml.safe_load(open(path))
        assert doc["style"] == "recorded"
        assert path.endswith(f"out/explainer_spec.{cid}.yaml")
    assert not (tmp_path / "out" / "explainer_spec.yaml").exists()


def test_explainer_from_capture_refuses_a_recorded_spec_that_fails_lint(tmp_path):
    bad = _spec()
    bad["scenes"][2]["narrative"] = "Let me show you refills. " + CUT2
    (tmp_path / "chlorine.yaml").write_text(yaml.safe_dump(bad))
    (tmp_path / "report.json").write_text(json.dumps(REPORT))
    with pytest.raises(rec.RecordedSpecError, match="verify"):
        emit_explainer_from_capture(str(tmp_path / "chlorine.yaml"), str(tmp_path / "report.json"))


def test_explainer_style_is_unchanged():
    """A spec with no `style` still gets the title card, end card and one arc."""
    manifest = _manifest()
    s = build_explainer_spec(
        manifest, workspace="w", master_ref="file:m.mp4", base_url="https://x/",
        tagline="Chlorine", country_focus="Kenya",
    )
    assert "style" not in s
    assert s["beats"][0]["kind"] == "intro_title"
    assert s["beats"][-1]["kind"] == "outro_card"
    assert [b["id"] for b in s["beats"][1:-1]] == ["s1", "s2", "s3", "s4"]


# --------------------------------------------------------------------------- #
# spec_qa
# --------------------------------------------------------------------------- #


def _qa_spec(tmp_path, spec: dict):
    why = {
        "problem": "Dispensers run dry and nobody knows which.",
        "rationale": "Assign and verify refills.",
        "spine": [{"id": "S1", "claim": "Managers assign dispensers.", "status": "grounded",
                   "evidence": [{"kind": "doc", "ref": "docs/x.md", "note": "n"}]}],
        "gaps": [],
    }
    (tmp_path / "why.yaml").write_text(yaml.safe_dump(why))
    spec = {**spec, "why_brief": "why.yaml"}
    for s in spec["scenes"]:
        s["features"] = [{"id": f"f-{s['id']}", "description": "row updates",
                          "verify": "assert the row shows the new water point"}]
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(spec))
    return p


def test_spec_qa_waives_the_overview_rule_for_recorded(tmp_path):
    from scripts.ddd.spec_qa import spec_qa

    v = spec_qa(_qa_spec(tmp_path, _spec()))
    reason = v.blocking_reason or ""
    assert "role: overview" not in reason
    assert "recorded style" not in reason


def test_spec_qa_fails_a_recorded_cut_that_opens_wrong(tmp_path):
    from scripts.ddd.spec_qa import spec_qa

    bad = _spec()
    bad["scenes"][2]["narrative"] = "Refills next. " + CUT2
    v = spec_qa(_qa_spec(tmp_path, bad))
    assert v.verdict == "fail"
    assert "recorded style — cut 'verify'" in (v.blocking_reason or "")


def test_spec_qa_still_requires_an_overview_for_an_explainer(tmp_path):
    from scripts.ddd.spec_qa import spec_qa

    explainer = {k: v for k, v in _spec().items() if k not in ("style", "cuts")}
    v = spec_qa(_qa_spec(tmp_path, explainer))
    assert "role: overview" in (v.blocking_reason or "")


def test_rate_band_matches_the_video_engine():
    """recorded.py and video-engine/src/lib/style.ts declare the same warp band."""
    from pathlib import Path
    import re

    ts = (Path(__file__).resolve().parents[2] / "video-engine/src/lib/style.ts").read_text()
    lo = float(re.search(r"RECORDED_RATE_MIN = ([0-9.]+)", ts).group(1))
    hi = float(re.search(r"RECORDED_RATE_MAX = ([0-9.]+)", ts).group(1))
    assert (lo, hi) == (rec.RECORDED_RATE_MIN, rec.RECORDED_RATE_MAX)
