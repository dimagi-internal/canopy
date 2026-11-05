"""CLAUDE.md is loaded into every session, so it stays a current-state index.

It holds what an agent needs before it knows what it is working on: how to run,
test and ship, and the invariants nothing may break, each as a rule plus a
pointer. Detail lives in docs/architecture/<topic>.md; history lives in the PR
description.
"""
from __future__ import annotations

import pathlib
import re

CLAUDE_MD = pathlib.Path(__file__).resolve().parent.parent / "CLAUDE.md"
CEILING = 60_000
# A date or a PR reference is history: what changed and when lives in GitHub.
HISTORY = re.compile(r"\b20\d\d-\d\d-\d\d\b|\(#\d+\)|\bPR #\d+|[\w-]+#\d{2,}")


def test_claude_md_stays_an_index():
    size = len(CLAUDE_MD.read_text(encoding="utf-8"))
    assert size <= CEILING, (
        f"CLAUDE.md is {size:,} chars (ceiling {CEILING:,}). Move subject detail into "
        "docs/architecture/<topic>.md and leave a one-line pointer; put history (what it "
        "replaced, what broke, when) in the PR description, not in docs."
    )


def test_claude_md_holds_no_history():
    hits = [
        f"  line {n}: {m.group(0)}"
        for n, line in enumerate(CLAUDE_MD.read_text(encoding="utf-8").splitlines(), 1)
        for m in HISTORY.finditer(line)
    ]
    assert not hits, (
        "CLAUDE.md is current state only, but it names a date or a PR:\n" + "\n".join(hits)
        + "\nState the rule as it holds now; put when and why it changed in the PR description."
    )
