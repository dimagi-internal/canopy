"""The region capture JS against a real browser, through the judge scope — canopy#780.

``test_impact_floor_timing.py`` pins the rules on synthetic frames. This runs the
half that fixtures cannot reach: whether ``regions.capture`` finds the scene's
targets and narrated elements on a live page, whether its boxes land on the
right pixels of a real screenshot, and whether a shared-template edit OUTSIDE the
scene's subject is then reused while an edit TO the subject re-judges.

SKIPS where no browser is installed (CI installs the playwright package only, as
for ``test_visual_capture_roundtrip.py``). Run it locally before changing the
capture JS.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.ddd import judge_scope
from scripts.walkthrough._lib import regions

PAGE = """<!doctype html><html><head><style>
body {{ font: 14px sans-serif; margin: 0; }}
header {{ background: #283c78; color: #fff; padding: 12px; }}
table {{ margin: 20px; border-collapse: collapse; }}
td {{ padding: 6px 12px; border-bottom: 1px solid #ddd; }}
.watch {{ background: {badge}; }}
footer {{ margin: 20px; color: #555; }}
</style></head><body>
<header>Spark facilitator pilot</header>
<p style="margin:20px">MEETING REGULARITY <b>66.0%</b></p>
<table>
 <tr><td>Kuunika Outreach Network</td><td class="watch">66.0%</td></tr>
 <tr><td>Tiyende Community Trust</td><td>88.0%</td></tr>
</table>
<footer>{footer}</footer>
</body></html>"""

SCENE = {
    "title": "Kuunika is behind",
    "narrative": "Kuunika Outreach Network reads 66.0% on MEETING REGULARITY.",
    "actions": [{"kind": "hover", "target": "css:tr:has-text(\"Kuunika Outreach Network\")"}],
}


@pytest.fixture(scope="module")
def browser_page():
    playwright = pytest.importorskip("playwright.sync_api", reason="playwright package not installed")
    try:
        manager = playwright.sync_playwright()
        pw = manager.start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"playwright could not start: {e}")
    try:
        browser = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001 — CI installs the package, not the browsers
        manager.__exit__(None, None, None)
        pytest.skip(f"no chromium available: {e}")
    page = browser.new_page(viewport={"width": 800, "height": 500})
    yield page
    browser.close()
    manager.__exit__(None, None, None)


def _render(page, run: Path, *, badge: str = "#fde68a", footer: str = "Targets come from the pilot design.") -> None:
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    page.set_content(PAGE.format(badge=badge, footer=footer))
    page.screenshot(path=str(snaps / "scene_1.png"), full_page=False)
    cap = regions.capture(page, SCENE, full_page=False)
    (snaps / "scene_1_regions.json").write_text(json.dumps({"scene_index": 1, "render_id": "r", **cap}))
    text = page.evaluate("() => document.body.innerText")
    (snaps / "scene_1_page_text.json").write_text(json.dumps({"scene_index": 1, "page_text": text, "render_id": "r"}))


def _spec(run: Path) -> Path:
    spec = run / "spec.yaml"
    spec.write_text(yaml.safe_dump({"name": "t", "scenes": [SCENE]}))
    return spec


def test_capture_finds_targets_and_narrated_elements(browser_page, tmp_path: Path) -> None:
    _render(browser_page, tmp_path)
    data = json.loads((tmp_path / "snapshots" / "scene_1_regions.json").read_text())
    by_key = {r["key"]: r for r in data["regions"]}
    row = by_key['action:css:tr:has-text("Kuunika Outreach Network")']
    assert row["found"] and "66.0%" in row["matches"][0]["text"]
    x, y, w, h = row["matches"][0]["box"]
    assert w > 100 and h > 10 and y > 40  # below the header, a real row box
    assert by_key["term:Kuunika Outreach Network"]["found"]
    assert by_key["term:MEETING REGULARITY"]["found"]


def test_a_style_edit_to_the_subject_rejudges(browser_page, tmp_path: Path) -> None:
    _render(browser_page, tmp_path)
    spec = _spec(tmp_path)
    judge_scope.plan(tmp_path, spec)
    judge_scope.record(tmp_path, spec, iteration=0)
    _render(browser_page, tmp_path, badge="#fca5a5")  # Watch went red: same text, new colour
    scope = judge_scope.plan(tmp_path, spec)
    assert scope["rejudge"] == [1] and "region_image" in scope["changed_components"]["1"]


def test_a_layout_only_edit_elsewhere_is_reused_but_a_text_edit_is_not(browser_page, tmp_path: Path) -> None:
    _render(browser_page, tmp_path)
    spec = _spec(tmp_path)
    judge_scope.plan(tmp_path, spec)
    judge_scope.record(tmp_path, spec, iteration=0)
    # Same footer text, restyled: the subject and the page text are untouched.
    browser_page.set_content(PAGE.format(badge="#fde68a", footer="Targets come from the pilot design."))
    browser_page.add_style_tag(content="footer { color: #b91c1c; }")
    browser_page.screenshot(path=str(tmp_path / "snapshots" / "scene_1.png"), full_page=False)
    assert judge_scope.plan(tmp_path, spec)["rejudge"] == []
    # A footer TEXT edit changes the page text the judges read: re-judged.
    _render(browser_page, tmp_path, footer="Targets are the PDD's own (P1 >= 80%).")
    scope = judge_scope.plan(tmp_path, spec)
    assert scope["rejudge"] == [1] and scope["changed_components"]["1"] == ["page_text"]
