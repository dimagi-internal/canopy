"""No DDD module may build a UnifiedSpec itself — spec_io is the only loader (L1).

Before spec_io there were eleven independent spec loaders across scripts/, each
doing its own yaml.safe_load. That is why the on-disk shape could not change:
there was no single thing to change. This test keeps it that way.

Deliberately checks for ``UnifiedSpec.model_validate`` rather than banning
``yaml.safe_load`` outright — several of these modules legitimately load
why-briefs, run state and auth config with yaml, and a blanket ban would be
wrong.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]

CONSUMERS = [
    "scripts/ddd/spec_qa.py",
    "scripts/ddd/upload.py",
    "scripts/ddd/narrative.py",
    "scripts/ddd/narrative_coherence.py",
    "scripts/ddd/findings_review.py",
    "scripts/ddd/video_judge.py",
    "scripts/walkthrough/record_video.py",
]

SPEC_BUILD_RE = re.compile(r"UnifiedSpec\.model_validate\(")


def test_no_consumer_builds_a_unified_spec_directly():
    offenders = []
    for rel in CONSUMERS:
        path = ROOT / rel
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if SPEC_BUILD_RE.search(line):
                offenders.append(f"{rel}:{i}")
    assert offenders == [], (
        "these call sites build a UnifiedSpec themselves instead of calling "
        f"scripts.ddd.spec_io.load_spec: {offenders}"
    )


def test_spec_io_is_the_one_place_that_does_build_it():
    text = (ROOT / "scripts/ddd/spec_io.py").read_text()
    assert SPEC_BUILD_RE.search(text), "spec_io must be the loader"
