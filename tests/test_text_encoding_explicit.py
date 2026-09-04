"""Every text-mode file read/write in canopy must name its encoding.

WHY THIS IS A TEST AND NOT A CONVENTION
---------------------------------------
`open()`, `Path.read_text()` and `Path.write_text()` default to the *locale's*
encoding. On macOS and Linux that is UTF-8, so the omission is invisible.  On
Windows it is cp1252, and canopy reads and writes markdown all day long —
personas, SKILL.md files, memory notes, transcripts, templates — which are full
of em-dashes and arrows.  So the same call that is correct here raises
``UnicodeEncodeError`` there, or silently mojibakes the file.

Measured 2026-09-03: ``canopy create-agent`` died at
``agent_factory.py`` on the ``→`` inside the shipped CLAUDE.md template —
that is step one of section 4 of the onboarding doc, so the documented path
failed at its first command for every Windows operator, and left a 0-byte
CLAUDE.md behind.  159 further call sites had the identical defect and were
fixed in the same change.

A convention would not have held: the encoding-free spelling is shorter, it is
what every example on the internet shows, and nothing on a Mac ever complains.
So it is enforced here instead — this test fails on any new omission, on the
machine of whoever adds it, before it reaches an operator who runs Windows.

If you genuinely need the locale encoding, say so explicitly with
``encoding=locale.getpreferredencoding()`` — that reads as a decision rather
than an oversight, and this test accepts it.
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_ROOTS = ("src", "hooks", "plugins")

# Functions that open a file in text mode.
TEXT_IO_NAMES = {"open", "read_text", "write_text"}

# A binary mode makes the encoding argument meaningless (and a TypeError).
BINARY_MODE_RE = re.compile(r"""["'][rwax+]*b[rwax+]*["']""")

SKIP_DIR_PARTS = {".git", "node_modules", "__pycache__", ".venv", "runtime"}


def _iter_py_files():
    for root in SEARCH_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if SKIP_DIR_PARTS & set(path.parts):
                continue
            yield path


def _call_args(src: str, open_paren: int) -> str | None:
    """Return the text between the call's parentheses, or None if unbalanced.

    String-aware so a paren inside a literal does not close the call.
    """
    depth = 0
    i = open_paren
    quote = None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if src.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
            i += 1
            continue
        if src.startswith('"""', i) or src.startswith("'''", i):
            quote = src[i : i + 3]
            i += 3
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[open_paren + 1 : i]
        i += 1
    return None


def _call_sites(src: str):
    """Yield (line_no, offset_of_open_paren) for every real call to a text-IO function.

    Tokenized rather than regexed on purpose: canopy's own source embeds shell one-liners
    containing `open(...)` inside template strings, and explains past bugs in comments that
    quote the call. Those are prose, not call sites — a regex flags them and a tokenizer
    cannot, because it never looks inside a STRING or COMMENT token.
    """
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return
    for i, tok in enumerate(toks):
        if tok.type != tokenize.NAME or tok.string not in TEXT_IO_NAMES:
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if not nxt or nxt.type != tokenize.OP or nxt.string != "(":
            continue
        if tok.string == "open":
            prev = toks[i - 1] if i else None
            # `webbrowser.open(` / `sock.open(` are not file IO; `Path(x).open(` is, but is
            # not used in this tree. A leading dot means an attribute call — skip it.
            if prev and prev.type == tokenize.OP and prev.string == ".":
                continue
        yield tok.start[0], nxt.start


def _offset(src_lines: list[str], pos: tuple[int, int]) -> int:
    row, col = pos
    return sum(len(l) for l in src_lines[: row - 1]) + col


def _offenders(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    out: list[tuple[int, str]] = []
    for line_no, paren_pos in _call_sites(src):
        args = _call_args(src, _offset(lines, paren_pos))
        if args is None:
            continue
        if "encoding" in args or BINARY_MODE_RE.search(args):
            continue
        out.append((line_no, src.splitlines()[line_no - 1].strip()))
    return out


def test_no_implicit_locale_encoding_in_text_io():
    findings: list[str] = []
    for path in sorted(_iter_py_files()):
        rel = path.relative_to(REPO_ROOT)
        for line_no, line in _offenders(path):
            findings.append(f"{rel}:{line_no}: {line[:120]}")
    assert not findings, (
        "text IO without an explicit encoding — these crash or corrupt on Windows "
        "(cp1252). Add encoding=\"utf-8\":\n  " + "\n  ".join(findings)
    )
