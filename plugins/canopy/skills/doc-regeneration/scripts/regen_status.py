#!/usr/bin/env python3
"""Where a repo's docs stand before a doc regen: mode, growth, topic-doc sizes.

Run from inside the repo (or pass --root). Reports:

  mode          full (first regen) or incremental (already regenerated: CLAUDE.md
                carries the "How to write this file" note and/or a size guard
                exists, AND a prior regen commit is found)
  last regen    the newest commit with a `Doc-Regen:` trailer, else the newest
                whose subject reads like a regen; --since REF overrides
  growth        CLAUDE.md chars at the last regen vs now, chars/day, and the
                projected date it reaches the target and the ceiling
  changed       markdown files changed since the last regen (the incremental scope)
  topic docs    chars per topic doc; WARN above --topic-warn (default 50,000)
  guards        test files that pin CLAUDE.md, and whether a size guard and a
                history guard appear to exist

Informational: always exits 0. Stdlib only.

    python3 regen_status.py [--root DIR] [--since REF]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

NOTE = re.compile(r"how to write this file", re.I)
REGEN_SUBJECT = re.compile(
    r"regenerat\w* (?:the )?(?:CLAUDE\.md|doc)|current-state index|doc-regen|doc regen", re.I
)
SPEC_DIRS = ("specs", "plans", "archive", "learnings", ".dry-run")


def git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def size_at(root: Path, ref: str, path: str = "CLAUDE.md") -> int | None:
    out = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{path}"], capture_output=True)
    return len(out.stdout.decode("utf-8", errors="replace")) if out.returncode == 0 else None


FMT = "--format=%H%x09%ct%x09%s"


def last_regen(root: Path, *paths: str) -> tuple[str, int, str] | None:
    """(sha, unix time, subject) of the newest regen commit (touching `paths`), or None.

    A `Doc-Regen:` trailer wins; otherwise a subject that reads like a regen.
    """
    pathspec = ["--", *paths] if paths else []
    for args in (["--grep=^Doc-Regen:"], []):
        for row in git(root, "log", FMT, "-n", "2000", *args, *pathspec).splitlines():
            sha, ts, subject = row.split("\t", 2)
            if args or REGEN_SUBJECT.search(subject):
                return sha, int(ts), subject
    return None


def guards(root: Path) -> tuple[list[str], bool, bool]:
    pins, size_guard, history_guard = [], False, False
    for f in git(root, "ls-files").splitlines():
        name = Path(f).name
        if not re.match(r"test_.*\.py$|.*_test\.py$|.*\.test\.[jt]sx?$", name):
            continue
        try:
            text = (root / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "CLAUDE.md" not in text:
            continue
        pins.append(f)
        if re.search(r"len\(|st_size|wc -c|\d{2}[_,]?000", text):
            size_guard = True
        if re.search(r"\\d\{4\}|20\\d\\d|\\d\\d-|#\\d|PR #", text):
            history_guard = True
    return pins, size_guard, history_guard


def topic_docs(root: Path, docs_dir: Path) -> list[tuple[int, str]]:
    rows = []
    for f in git(root, "ls-files", "--", str(docs_dir.relative_to(root))).splitlines():
        if not f.endswith(".md") or any(part in SPEC_DIRS for part in Path(f).parts):
            continue
        try:
            rows.append((len((root / f).read_text(encoding="utf-8", errors="replace")), f))
        except OSError:
            continue
    return sorted(rows, reverse=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--since", help="treat this ref as the last regen")
    ap.add_argument("--target", type=int, default=40_000)
    ap.add_argument("--ceiling", type=int, default=60_000)
    ap.add_argument("--topic-warn", type=int, default=50_000)
    ap.add_argument("--docs-dir", type=Path, help="topic-doc dir (default docs/architecture, else docs)")
    args = ap.parse_args(argv)

    root = Path(git(args.root, "rev-parse", "--show-toplevel").strip() or args.root).resolve()
    claude = root / "CLAUDE.md"
    now_text = claude.read_text(encoding="utf-8") if claude.exists() else ""
    now_size = len(now_text)
    pins, size_guard, history_guard = guards(root)

    if args.since:
        sha = git(root, "rev-parse", args.since).strip()
        row = git(root, "log", "-1", FMT, sha).strip()
        regen = (row.split("\t")[0], int(row.split("\t")[1]), row.split("\t", 2)[2]) if row else None
        base = regen
    else:
        regen = last_regen(root)
        # Growth is measured from the last regen that rewrote CLAUDE.md itself;
        # an incremental pass that only touched topic docs is not a baseline.
        base = last_regen(root, "CLAUDE.md") or regen
    note = bool(NOTE.search(now_text))
    incremental = bool(regen) and (note or size_guard)

    print(f"repo: {root}")
    if not claude.exists():
        print("mode: genesis (no CLAUDE.md)")
    else:
        print(f"mode: {'incremental' if incremental else 'full'}"
              f"  (how-to-write note: {'yes' if note else 'no'}; size guard: {'yes' if size_guard else 'no'};"
              f" history guard: {'yes' if history_guard else 'no'})")
    print(f"CLAUDE.md: {now_size:,} chars (target {args.target:,}, ceiling {args.ceiling:,})")

    def stamp(row: tuple[str, int, str]) -> str:
        return f"{row[0][:10]} {dt.date.fromtimestamp(row[1]).isoformat()} {row[2]}"

    if regen:
        sha = regen[0]
        print(f"last regen: {stamp(regen)}")
        if base and base[0] != sha:
            print(f"last regen that rewrote CLAUDE.md: {stamp(base)}")
        then = size_at(root, base[0]) if base else None
        days = (dt.datetime.now().timestamp() - base[1]) / 86400 if base else 0
        if then is not None and claude.exists():
            if days < 1:
                print(f"growth: {then:,} -> {now_size:,} chars in under a day; too recent to extrapolate")
            else:
                rate = (now_size - then) / days
                print(f"growth: {then:,} -> {now_size:,} chars over {days:.1f} days = {rate:+,.0f} chars/day")
                today = dt.date.today()
                for label, limit in (("target", args.target), ("ceiling", args.ceiling)):
                    if now_size >= limit:
                        print(f"  already over the {label} ({limit:,})")
                    elif rate > 0:
                        eta = today + dt.timedelta(days=(limit - now_size) / rate)
                        print(f"  reaches the {label} ({limit:,}) around {eta.isoformat()}")
                    else:
                        print(f"  not growing toward the {label}")
        changed = git(root, "diff", "--name-only", sha, "--", "*.md").split()
        claude_stat = git(root, "diff", "--shortstat", sha, "--", "CLAUDE.md").strip()
        print(f"changed since last regen: CLAUDE.md {claude_stat or 'unchanged'}; {len(changed)} markdown file(s)")
        for f in changed:
            print(f"  {f}")
    else:
        print("last regen: none found (no Doc-Regen: trailer, no regen-like subject)")

    docs_dir = args.docs_dir or (root / "docs" / "architecture" if (root / "docs" / "architecture").is_dir() else root / "docs")
    docs_dir = docs_dir if docs_dir.is_absolute() else root / docs_dir
    if docs_dir.is_dir():
        rows = topic_docs(root, docs_dir)
        over = [r for r in rows if r[0] > args.topic_warn]
        print(f"topic docs ({docs_dir.relative_to(root)}): {len(rows)}, {sum(r[0] for r in rows):,} chars;"
              f" {len(over)} over {args.topic_warn:,}")
        for size, f in rows:
            flag = "  WARN split it or add an index; grep it, don't read it whole" if size > args.topic_warn else ""
            print(f"  {size:8,}  {f}{flag}")
    print(f"tests that pin CLAUDE.md: {', '.join(pins) if pins else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
