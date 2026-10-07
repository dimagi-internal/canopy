"""canopy#785 — an unchanged scene is reused across two renders with different seeds.

Measured on connect-labs ``supply-sophie-sheets-2026-10-06-001``: the narrative
reseeds every take (``setup: rerun: per_render``), so the tender on screen was
309, 318, 321 ... 342 across eleven renders. Text comparisons already put a
reseeded id back in ``${var}`` form, but the frame cannot be un-substituted, so a
scene whose subject SHOWS the id moved its crop every render and was re-judged
every iteration (``reuse: []`` on every pass of the run).

These fixtures render real PNGs that draw the id, and pin: the same scene with a
new id is reused; a real change on the same element is not; and a scene the last
batch edited is never reused on the strength of the id explanation.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from PIL import Image, ImageDraw

from scripts.ddd import judge_scope

W, H = 480, 240
BOX = [40, 80, 300, 40]  # the subject element's box in frame pixels


def _render(run: Path, *, tender: int, label: str = "Tender", colour=(30, 30, 30), when: str = "10:42") -> Path:
    """One take: scene 1 shows the reseeded tender id in its subject; scene 2 does not."""
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    for scene in (1, 2):
        img = Image.new("RGB", (W, H), (250, 250, 250))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 30], fill=(40, 40, 120))
        text = f"{label} {tender} - updated {when}" if scene == 1 else "Suppliers ranked by landed cost"
        d.rectangle([BOX[0], BOX[1], BOX[0] + BOX[2], BOX[1] + BOX[3]], outline=(180, 180, 180))
        d.text((BOX[0] + 8, BOX[1] + 12), text, fill=colour)
        img.save(snaps / f"scene_{scene}.png")
        (snaps / f"scene_{scene}_regions.json").write_text(
            json.dumps(
                {
                    "full_page": False,
                    "dpr": 1,
                    "regions": [
                        {
                            "key": "narration:Tender",
                            "source": "narration",
                            "found": True,
                            "matches": [{"tag": "h2", "text": text, "attrs": {}, "box": BOX}],
                        }
                    ],
                }
            )
        )
        (snaps / f"scene_{scene}_page_text.json").write_text(
            json.dumps(
                {
                    "scene_index": scene,
                    "url": f"https://labs/supply/procurement/tenders/{tender}/",
                    "page_text": f"Procurement\n{text}",
                    "render_id": f"r{tender}",
                }
            )
        )
    (run / "run-report.json").write_text(
        json.dumps({"setup": {"rerun": "per_render", "variables": {"round2_tender_id": tender}}, "actions": []})
    )
    spec = run / "spec.yaml"
    spec.write_text(yaml.safe_dump({"name": "t", "scenes": [{"title": "Quote"}, {"title": "Compare"}]}))
    return spec


def _judged(run: Path, spec: Path) -> None:
    judge_scope.plan(run, spec)
    judge_scope.record(run, spec, iteration=0)


def test_an_unchanged_scene_is_reused_across_two_seeds(tmp_path: Path) -> None:
    spec = _render(tmp_path, tender=341)
    _judged(tmp_path, spec)
    _render(tmp_path, tender=342, when="11:07")  # reseeded: new id AND a new clock stamp on screen
    scope = judge_scope.plan(tmp_path, spec)
    assert scope["full"] is False
    assert scope["reuse"] == [1, 2] and scope["rejudge"] == []
    assert "region_image_explained" in scope["impact"]["1"]


def test_the_frame_really_moved(tmp_path: Path) -> None:
    """The control: without the explanation the reseed alone re-judges scene 1."""
    spec = _render(tmp_path, tender=341)
    _judged(tmp_path, spec)
    _render(tmp_path, tender=342)
    scope = judge_scope.plan(tmp_path, spec, edited_scenes=[1])
    assert scope["rejudge"] == [1] and scope["reuse"] == [2]
    assert scope["changed_components"]["1"] == ["region_image"]


def test_a_real_text_change_on_the_same_element_still_rejudges(tmp_path: Path) -> None:
    spec = _render(tmp_path, tender=341)
    _judged(tmp_path, spec)
    _render(tmp_path, tender=342, label="Round")  # the product renamed tender -> round
    scope = judge_scope.plan(tmp_path, spec)
    assert scope["rejudge"] == [1]
    assert {"page_text", "region_dom", "region_image"} <= set(scope["changed_components"]["1"])


def test_a_style_change_with_a_reseed_rejudges_when_the_batch_edited_the_scene(tmp_path: Path) -> None:
    spec = _render(tmp_path, tender=341)
    _judged(tmp_path, spec)
    _render(tmp_path, tender=342, colour=(220, 30, 30))  # a CSS fix (both scenes) + a reseed
    # Scene 2's text did not change, so nothing explains its crop: it re-judges anyway.
    assert judge_scope.plan(tmp_path, spec, edited_scenes=[1])["rejudge"] == [1, 2]
    assert judge_scope.plan(tmp_path, spec, edited_scenes="all")["rejudge"] == [1, 2]
    # Had the batch not touched scene 1, the reseed would have explained its crop:
    # this is the false-reuse window the edited-scenes guard closes.
    assert judge_scope.plan(tmp_path, spec, edited_scenes=[2])["rejudge"] == [2]


def test_the_batch_scenes_come_from_run_state() -> None:
    class S:
        iteration = 4
        batch_plan = {"for_iteration": 4, "scenes": [3, 6], "unscoped": False}

    assert judge_scope._batch_scenes(S()) == [3, 6]
    S.batch_plan = {"for_iteration": 4, "scenes": [3], "unscoped": True}
    assert judge_scope._batch_scenes(S()) == "all"
    S.batch_plan = {"for_iteration": 3, "scenes": [3]}
    assert judge_scope._batch_scenes(S()) is None  # a stale plan says nothing about this pass
