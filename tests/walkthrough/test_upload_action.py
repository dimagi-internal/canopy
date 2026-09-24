"""The `upload` verb: put a file into an <input type=file>.

A real headless Chromium, because the whole point of the verb is the one thing
a fake page cannot show: that the file actually lands in the input and its
`change` fires, with no OS dialog. Skipped when Playwright's browser is absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.narrative.models import UploadAction  # noqa: E402
from scripts.walkthrough._lib.recorder import execute_action  # noqa: E402

PAGE = """<!doctype html><html><body>
<form><label>The file <input id="doc" type="file" name="upload"></label></form>
<p id="picked">nothing</p>
<script>
document.getElementById('doc').addEventListener('change', e => {
  document.getElementById('picked').textContent = e.target.files[0].name;
});
</script></body></html>"""


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            pg = browser.new_page()
            pg.set_content(PAGE)
            yield pg
            browser.close()
    except Exception as exc:  # pragma: no cover - no browser installed
        pytest.skip(f"no chromium: {exc}")


def test_schema_accepts_upload():
    action = UploadAction(kind="upload", target="css:#doc", value="documents/reg.md")
    assert action.value == "documents/reg.md"


def test_upload_sets_the_file_and_fires_change(page, tmp_path):
    specimen = tmp_path / "registration.md"
    specimen.write_text("# Registration\n")
    result = execute_action(page, {"kind": "upload", "target": "css:#doc", "value": str(specimen)})
    assert result.ok is True
    assert page.locator("#picked").inner_text() == "registration.md"


def test_relative_path_resolves_against_cwd(page, tmp_path, monkeypatch):
    (tmp_path / "specimens").mkdir()
    (tmp_path / "specimens" / "coa.md").write_text("certificate")
    monkeypatch.chdir(tmp_path)
    result = execute_action(page, {"kind": "upload", "target": "css:#doc", "value": "specimens/coa.md"})
    assert result.ok is True
    assert page.locator("#picked").inner_text() == "coa.md"


def test_missing_file_fails_the_action_not_the_render(page, tmp_path):
    result = execute_action(page, {"kind": "upload", "target": "css:#doc", "value": str(tmp_path / "absent.pdf")})
    assert result.ok is False
    assert result.error_kind == "target_not_found"
    assert page.locator("#picked").inner_text() == "nothing"
