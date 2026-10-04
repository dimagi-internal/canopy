r"""Visual QA for a Google Doc an agent wrote — backs `canopy gdoc check`.

`verify_published_render` (agent_gdoc.py) compares a doc's MARKDOWN export to its source,
which is the right lens for structure: lists, links, leaked markup. It is blind to what a
reader sees when styling is inherited or bled, because markdown carries no styling. The
HTML export does — it holds the rendered inline styles — so this module reads that and
flags the defects that have actually shipped to people:

- **An empty doc.** `publish --replace` clears the body before it injects the new one; a
  timeout in between leaves only the engine's sentinel. Every other check here is a defect
  detector, and an empty doc trips none of them, so it used to score a clean pass.
- **Italic bleed.** `--replace` reuses the old doc's run styling, so one italic line in an
  early version can italicise the whole body of every later one.
- **Wrong font / size**, e.g. email-block labels that inherited a heading's 18pt.
- **Leaked markdown / HTML entities** in the visible text, including the paired single-
  asterisk span a markdown export cannot show.
- **Dropped links and email blocks**, against counts the caller derives from its source.

Ported from Eva's `skills/gdoc-review/check_gdoc.py`, which caught each of these on a real
deliverable between 2026-07 and 2026-09 before it moved here so every agent gets it.

A PASS also writes a RECEIPT (`<agent home>/gdoc-gate/passed/<docId>`). The fleet Stop
hook `agent-core/gdoc_gate.py` refuses to let a turn end on a link to a doc the agent
wrote and has not reviewed since — and this receipt is the only thing that satisfies it.
`canopy gdoc publish` and `email-blocks` run this review themselves, so the hook only
ever speaks to docs edited by other means (raw `gog docs` verbs, a Docs API batch).
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

HEADING_SIZES = {"14pt", "16pt", "18pt", "20pt", "24pt", "26pt", "28pt", "36pt"}
MD_LEAKS = ["**", "##", "](http", "&amp;", "&nbsp;", "&lt;", "| ---", "|---"]

# Fraction of styled runs that are italic. Real `--replace` bleed italicises essentially
# the whole body; a research brief that quotes its sources heavily lands well below.
ITALIC_FAIL_FRAC = 0.60
ITALIC_WARN_FRAC = 0.35

BODY_SENTINEL = "__CANOPY_GDOC_BODY_SENTINEL__"
MIN_BODY_CHARS = 200


# ---- receipts ----------------------------------------------------------------------------

def agent_home(slug: str) -> Path:
    """Where this agent keeps per-machine state. Matches the hook loaders, which set
    CANOPY_AGENT_HOME from their own repo (e.g. ~/.eva)."""
    return Path(os.environ.get("CANOPY_AGENT_HOME") or os.path.expanduser(f"~/.{slug}"))


def mark(home: Path, kind: str, doc_id: str) -> None:
    """Record a fact about one doc (`published` or `passed`). The mtime is the record.
    Best-effort: a missing receipt costs one extra reminder from the hook, never a wrong
    verdict."""
    try:
        d = home / "gdoc-gate" / kind
        d.mkdir(parents=True, exist_ok=True)
        (d / doc_id).write_text("", encoding="utf-8")
    except OSError:
        pass


# ---- the review (pure) -------------------------------------------------------------------

def _visible_text(raw: str) -> str:
    body = re.sub(r"<style[^>]*>.*?</style>", "", raw, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", body))


def review_html(raw: str, *, font: str | None = None, body_size: str | None = None,
                expect_blocks: int | None = None,
                expect_links: int | None = None) -> dict:
    """Judge a doc's HTML export. Returns {passed, fails, warns, notes}."""
    u = html.unescape(raw)
    txt = _visible_text(raw)
    fails: list[str] = []
    warns: list[str] = []
    notes: dict = {}

    body_chars = len(txt.strip())
    notes["body_chars"] = body_chars
    if BODY_SENTINEL in txt:
        fails.append("EMPTY DOC: the body holds only the engine's replace sentinel — a "
                     "`--replace` cleared the doc and never injected the new content. The "
                     "previous version is gone; publish fresh and rename this one "
                     "'ZZ SUPERSEDED …'.")
    elif body_chars < MIN_BODY_CHARS:
        fails.append(f"EMPTY DOC: only {body_chars} visible characters — the doc is "
                     f"effectively blank. Confirm the write actually landed.")

    ital = raw.count("font-style:italic")
    styled = ital + raw.count("font-style:normal")
    frac = ital / styled if styled else 0.0
    notes["italic_runs"] = ital
    if styled and frac >= ITALIC_FAIL_FRAC:
        fails.append(f"ITALIC BLEED: {ital}/{styled} styled runs are italic ({frac:.0%}) — "
                     "the signature of `--replace` reusing old run styling. Strip it with "
                     "one updateTextStyle {italic:false} over the body, or publish fresh.")
    elif styled and frac >= ITALIC_WARN_FRAC:
        warns.append(f"ITALIC: {ital}/{styled} styled runs italic ({frac:.0%}) — fine if "
                     "they are quotes; check it is not partial `--replace` bleed.")

    fams = Counter(x.strip().strip('"').split(",")[0]
                   for x in re.findall(r'font-family:\s*"?([^;}"]+)', u))
    notes["fonts"] = dict(fams)
    if font:
        off = {f: n for f, n in fams.items()
               if font.lower() not in f.lower()
               and not any(m in f.lower() for m in ("courier", "consolas", "mono"))}
        if off:
            warns.append(f"FONT: non-{font} families present: {off} (a code span is fine; "
                         "a body run is not).")

    sizes = Counter(x.strip() for x in re.findall(r"font-size:([^;}]+)", u))
    notes["sizes"] = dict(sizes)
    if body_size:
        bad = {s: n for s, n in sizes.items()
               if s != f"{body_size}pt" and s not in HEADING_SIZES}
        if bad:
            warns.append(f"SIZE: unexpected sizes {bad} (expected {body_size}pt body plus "
                         "heading sizes).")

    leaks = [tok for tok in MD_LEAKS if tok in txt]
    if leaks:
        fails.append(f"MARKDOWN LEAK: raw tokens visible in the text: {leaks}.")
    md_ital = re.findall(r"(?<!\*)\*([^*\n]{1,200})\*(?!\*)", txt)
    if md_ital:
        shown = ", ".join(repr(s[:40]) for s in md_ital[:3])
        fails.append(f"MARKDOWN LEAK: {len(md_ital)} *italic* span(s) shipping as literal "
                     f"asterisks: {shown}.")

    links = len(re.findall(r'href="https?://', raw))
    notes["links"] = links
    if expect_links is not None and links < expect_links:
        fails.append(f"LINKS: {links} found, expected at least {expect_links}. Derive the "
                     "number from the source (grep -oE '\\]\\(https?://' src.md | wc -l), "
                     "never guess it.")

    tables = raw.count("<table")
    notes["tables"] = tables
    if expect_blocks is not None and tables != expect_blocks:
        fails.append(f"BLOCKS: {tables} tables, expected {expect_blocks} email blocks.")

    return {"passed": not fails, "fails": fails, "warns": warns, "notes": notes}


# ---- the review (I/O) --------------------------------------------------------------------

def style_defaults(repo: Path | None) -> dict:
    """The agent's house style, from config/agent.json `gdoc_style` ({font, body_size}).
    Absent → no font/size expectation (those checks only warn anyway)."""
    if not repo:
        return {}
    aj = Path(repo) / "config" / "agent.json"
    try:
        style = json.loads(aj.read_text(encoding="utf-8")).get("gdoc_style") or {}
    except (OSError, ValueError):
        return {}
    out = {}
    if style.get("font"):
        out["font"] = str(style["font"])
    if style.get("body_size"):
        out["body_size"] = str(style["body_size"])
    return out


def export_html(account: str, client: str, doc_id: str, runner=subprocess.run) -> str:
    """The doc's HTML export. Raises RuntimeError with gog's own stderr on failure."""
    d = tempfile.mkdtemp(prefix="canopy-gdoc-review-")
    try:
        out = os.path.join(d, "doc.html")      # must not exist: gog refuses to overwrite
        r = runner(["gog", "drive", "download", doc_id, "--format", "html", "--out", out,
                    "--account", account, "--client", client],
                   capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"gog drive download exit {r.returncode}: "
                               f"{(r.stderr or r.stdout or '').strip()[:300]}")
        return Path(out).read_text(encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def review_doc(*, slug: str, account: str, client: str, doc_id: str,
               repo: Path | None = None, font: str | None = None,
               body_size: str | None = None, expect_blocks: int | None = None,
               expect_links: int | None = None, runner=subprocess.run) -> dict:
    """Export + review one doc; on a PASS, write the receipt the Stop hook looks for.

    An export failure returns {"passed": None, "error": ...}: not reviewed, no receipt."""
    style = style_defaults(repo)
    try:
        raw = export_html(account, client, doc_id, runner=runner)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        return {"passed": None, "error": str(e), "fails": [], "warns": [], "notes": {}}
    result = review_html(raw, font=font or style.get("font"),
                         body_size=body_size or style.get("body_size"),
                         expect_blocks=expect_blocks, expect_links=expect_links)
    if result["passed"]:
        mark(agent_home(slug), "passed", doc_id)
    return result


def format_report(doc_id: str, result: dict) -> str:
    lines = [f"=== gdoc check: {doc_id} ==="]
    if result.get("error"):
        lines.append(f"  ? NOT REVIEWED — could not export the doc: {result['error']}")
        return "\n".join(lines)
    for k, v in result["notes"].items():
        lines.append(f"  · {k}: {v}")
    lines += [f"  ! WARN  {w}" for w in result["warns"]]
    lines += [f"  ✗ FAIL  {f}" for f in result["fails"]]
    if result["passed"]:
        lines.append("  ✓ PASS" + (" (with warnings to eyeball)" if result["warns"] else ""))
    return "\n".join(lines)
