"""End-user pages name canopy-web screens and runner installer details; this pins them.

tests/docs_ui_labels.yaml lists each one with the canopy-web file that defines it.
The page side always runs. The canopy-web side runs when CANOPY_WEB_PATH names a
canopy-web checkout (CI does not have one), e.g.

    CANOPY_WEB_PATH=~/src/canopy-web uv run pytest tests/test_docs_ui_labels.py
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = yaml.safe_load((Path(__file__).with_name("docs_ui_labels.yaml")).read_text(encoding="utf-8"))
WEB = os.environ.get("CANOPY_WEB_PATH")


@pytest.mark.parametrize("e", ENTRIES, ids=lambda e: e["label"])
def test_pages_still_name_the_label(e):
    for page in e["pages"]:
        text = (ROOT / page).read_text(encoding="utf-8")
        assert e["label"].split(" → ")[-1] in text, f"{page} no longer names {e['label']!r}: drop it from tests/docs_ui_labels.yaml"


@pytest.mark.skipif(not WEB, reason="set CANOPY_WEB_PATH to a canopy-web checkout")
@pytest.mark.parametrize("e", ENTRIES, ids=lambda e: e["label"])
def test_canopy_web_still_has_the_label(e):
    src = Path(WEB).expanduser() / e["file"]
    assert src.exists(), f"{e['file']} is gone from canopy-web; fix the pages ({e['pages']}) and this entry"
    text = src.read_text(encoding="utf-8")
    for needle in e["needles"]:
        assert needle in text, f"{e['file']} no longer contains {needle!r}; update {e['pages']}"
