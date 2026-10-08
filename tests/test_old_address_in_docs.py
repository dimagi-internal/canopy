"""No human-facing doc sends a reader to canopy's OLD address.

canopy moved from https://labs.connect.dimagi.com/canopy/ to
https://canopy.dimagi.com/ on 2026-10-05. The old address still serves machine
traffic and 302s browser pages to the new one, so a stale link in a doc looks
like it works (a ``curl -L`` check follows the 302 and sees 200) — which is how
ACE shipped one in ace#2762 (canopy#803).

This checks the markdown a person or an agent reads for instructions — docs,
the plugin's skills and commands, agent-core. It allows a line that names BOTH
addresses: that line is describing the move. Code and tests that deliberately
handle the old address are not markdown and are not checked; dated plans/specs
are recorded history and are left as written.
Pure stdlib.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
# `…/canopy` but not `…/canopy-host` (connect-labs' own path).
OLD = re.compile(r"labs\.connect\.dimagi\.com/canopy(?![-\w])")
NEW = "canopy.dimagi.com"

# Dated records of what was planned or decided at the time — not instructions.
HISTORY = ("docs/archive/", "docs/superpowers/", "docs/plans/")


def _markdown_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.splitlines() if not p.startswith(HISTORY)]


def test_no_doc_links_the_old_address():
    stale = []
    for rel in _markdown_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if OLD.search(line) and NEW not in OLD.sub("", line):
                stale.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not stale, (
        "These docs point at canopy's old address — use https://canopy.dimagi.com/ "
        "(check with curl -s -o /dev/null -w '%{http_code} %{redirect_url}', never -L):\n"
        + "\n".join(stale)
    )
