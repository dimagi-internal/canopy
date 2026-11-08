#!/usr/bin/env python3
"""Flag history in markdown docs: lines that say when or why something changed.

Docs describe the present; GitHub holds the history. This flags lines with:

  date      an ISO date (2026-07-26), or "since <year/month/date>"
  pr-ref    #1234, repo#1234, PR 1234
  phrase    "owner decision", "used to" (not "is used to"), "previously",
            "formerly", "before this", "was/were/has been replaced|removed|
            retired|renamed|deprecated", "fixed in|by", "was X, now Y"

It does NOT flag a date inside a filename or link target
(docs/superpowers/specs/2026-10-06-x.md, specs/2026-10-06-x, [spec](specs/2026-10-06-x.md)), section
numbers like (4.1.5.1), URLs, or fenced code blocks. Every audience is held to
the same rule: an integrator guide states the current contract, not a changelog.

Prints `file:line: [kinds] text` per flagged line, then a per-file count.
Exit 1 if anything is flagged.

    python3 find_history.py [--root DIR] [--ignore-file PATH | --no-ignore] FILE.md [FILE.md ...]

A doc whose dated notes are its content (a design record), or a line that must
carry a date, is listed once, with a reason, in `.docs-ignore` at the repo root;
see docs_ignore.py. `--no-ignore` shows everything. Entries nothing matched
print as UNUSED.

Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docs_ignore  # noqa: E402

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
# A dated slug (2026-10-06-caller-context-brain): a design record's name, with or without its extension.
DATED_SLUG = re.compile(r"[\w./-]*\d{4}-\d{2}-\d{2}-[A-Za-z][\w.-]*")


def scrub(line: str) -> str:
    """Remove the parts of a line that look like history but are not."""
    line = URL.sub(" ", line)
    line = DATED_LINK.sub(" ", line)
    line = LINK_TARGET.sub("]", line)
    line = DATED_PATH.sub(" ", line)
    line = DATED_SLUG.sub(" ", line)
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


def _git_root() -> Path:
    import subprocess

    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--root", type=Path, help="repo root for .docs-ignore paths (default: the current directory's git toplevel)")
    ap.add_argument("--ignore-file", type=Path, help="ignore file (default: <root>/.docs-ignore)")
    ap.add_argument("--no-ignore", action="store_true", help="report everything, ignore file or not")
    ap.add_argument("--width", type=int, default=200, help="truncate printed lines (0 = full)")
    args = ap.parse_args(argv)

    root = (args.root or _git_root()).resolve()
    ign = docs_ignore.Ignores([], root) if args.no_ignore else docs_ignore.load(root, args.ignore_file)
    counts: dict[str, int] = {}
    for doc in args.files:
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR {doc}: {exc}", file=sys.stderr)
            continue
        flagged = 0
        rel = ign.rel(doc)
        for n, found, line in scan(text):
            if ign.history(rel, line):
                continue
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
    for line in docs_ignore.report(ign, "find_history"):
        print(line)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
