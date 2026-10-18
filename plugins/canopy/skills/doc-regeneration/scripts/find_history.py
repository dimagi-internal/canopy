#!/usr/bin/env python3
"""Flag history in markdown docs: lines that say when or why something changed.

Docs describe the present; GitHub holds the history. This flags lines with:

  date      an ISO date (2026-07-26), or "since <year/month/date>"
  pr-ref    #1234, repo#1234, PR 1234
  phrase    "owner decision", "used to" (not "is used to"), "previously",
            "formerly", "before this", "was/were/has been replaced|removed|
            retired|renamed|deprecated", "fixed in|by", "was X, now Y"

It does NOT flag a date inside a filename or link target
(docs/superpowers/specs/2026-10-06-x.md, [spec](specs/2026-10-06-x.md)), section
numbers like (4.1.5.1), URLs, or fenced code blocks. Every audience is held to
the same rule: an integrator guide states the current contract, not a changelog.

Prints `file:line: [kinds] text` per flagged line, then a per-file count.
Exit 1 if anything is flagged.

    python3 find_history.py FILE.md [FILE.md ...]

Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DATE = re.compile(r"(?<![=\w])(?:19|20)\d{2}-\d{2}-\d{2}\b")
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
SINCE = re.compile(rf"\bsince\s+(?:(?:19|20)\d{{2}}\b|{MONTH}\b|v?\d+\.\d+)", re.I)
# A PR/issue ref: `repo#12`, `(#12)`, `PR 12` / `PR #12`, or a bare `#123` (3+ digits).
# A bare one- or two-digit `#N` is usually a label ("Seam #2", "step #3"), not a PR.
PR_REF = re.compile(
    r"(?<![\w&/#])[A-Za-z][\w.-]*#\d{1,5}\b(?!\w)"
    r"|\(#\d{1,5}\b"
    r"|(?<![\w&/#])#\d{3,5}\b(?!\w)"
    r"|\bPR\s+#?\d{1,5}\b"
)
PHRASES = re.compile(
    r"\bowner(?:'s)? decision\b"
    r"|(?<!\bis )(?<!\bare )(?<!\bbe )(?<!\bbeen )(?<!\bget )(?<!\bgets )(?<!\bbeing )\bused to\b"
    r"|\bpreviously\b|\bformerly\b|\bbefore this\b"
    r"|\b(?:was|were|has been|have been)\s+(?:replaced|removed|retired|renamed|deprecated)\b"
    r"|\bfixed (?:in|by)\b"
    r"|\bwas\s+\S+(?:\s+\S+){0,5}?,?\s+(?:and\s+)?now\b",
    re.I,
)

URL = re.compile(r"\w+://\S+")
# A markdown link whose target names a dated file: drop it whole (text included).
DATED_LINK = re.compile(r"\[[^\]]*\]\([^)]*\d{4}-\d{2}-\d{2}[^)]*\)")
# Any other link target, and any path-ish token that carries a date.
LINK_TARGET = re.compile(r"\]\([^)]*\)")
DATED_PATH = re.compile(r"[\w./-]*\d{4}-\d{2}-\d{2}[\w.-]*(?:/[\w./-]*|\.\w+)")


def scrub(line: str) -> str:
    """Remove the parts of a line that look like history but are not."""
    line = URL.sub(" ", line)
    line = DATED_LINK.sub(" ", line)
    line = LINK_TARGET.sub("]", line)
    line = DATED_PATH.sub(" ", line)
    return line


def kinds(line: str) -> list[str]:
    text = scrub(line)
    found = []
    if DATE.search(text) or SINCE.search(text):
        found.append("date")
    if PR_REF.search(text):
        found.append("pr-ref")
    if PHRASES.search(text):
        found.append("phrase")
    return found


def scan(text: str):
    """Yield (line_no, kinds, line) for each line with history markers."""
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        found = kinds(line)
        if found:
            yield n, found, line.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--width", type=int, default=200, help="truncate printed lines (0 = full)")
    args = ap.parse_args(argv)

    counts: dict[str, int] = {}
    for doc in args.files:
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR {doc}: {exc}", file=sys.stderr)
            continue
        flagged = 0
        for n, found, line in scan(text):
            flagged += 1
            shown = line if not args.width or len(line) <= args.width else line[: args.width] + "…"
            print(f"{doc}:{n}: [{','.join(found)}] {shown}")
        counts[str(doc)] = flagged

    print()
    print("per-file history lines:")
    for name, flagged in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {flagged:5d}  {name}")
    total = sum(counts.values())
    print(f"total flagged: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
