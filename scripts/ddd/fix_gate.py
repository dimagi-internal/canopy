"""Fixer-diff gate — a fix batch that annotates the product instead of fixing it does not ship.

    python -m scripts.ddd.fix_gate <target_repo> [--base origin/main] [--head HEAD] \\
        [--run-id RUN_ID] [--run-dir RUN_DIR] [--spec SPEC] [--term NAME ...] [--json]
    python -m scripts.ddd.fix_gate --diff-file batch.diff [...]

Reads the fix batch's diff (``git diff <base>...<head>`` in the target repo) and
flags, per ADDED line, what a DDD fixer reaches for when it is answering a judge
rather than improving the product. Exits 1 when anything is flagged — in EVERY
objective (``product`` and ``demo`` alike) — and 0 when the diff is clean. With
``--run-id`` the result is stamped on ``state.fix_gate``, and ``judge_gate``
refuses to judge (or record a fix SHA) while it reads ``fail``: a flagged batch
goes back to its fixer to restructure, not to annotate.

Why each check exists — canopy#786, Hal's audit of the ACE Spark
(``spark-facilitator-programme-cascade`` -001..-005) and connect-labs supply
(``supply-sophie-sheets``) runs, ~9 h each, neither converged:

* **user_prose** — a NEW user-visible sentence (a template text node, or a string
  literal of ``prose_words``+ words). Spark's batch shipped "Click a column name
  for its definition · ↕ sorts" and "Short of target, not yet off target"; 32 of
  232 Spark fix recommendations were "add a tooltip / legend / definition".
* **fix_comment** — a comment or docstring narrating the fix: the loop's own
  vocabulary (DDD, batch 3, scene 4, the judge, the viewer), a concrete demo date,
  or the before/after story ("read as a contradiction", "were six lines
  differing only in…"). 42 of 263 supply template lines were such comments.
  A template comment of ``template_comment_words``+ words is flagged too: a
  template states the UI, it does not explain it.
* **persona_name** — a persona from the spec (or a ``fix_gate.demo_terms`` name)
  in product code: "Six invitations sent on 19 Sep by Sophie were six lines…".
* **literal_rule** — a rule keyed on free text instead of data: a branch or
  membership test on a multi-word literal, a set of phrases
  (``_TERMS_GAPS = frozenset({"tender duty terms", "duty terms"})``), or a
  keyword regex that guesses meaning from wording
  (``/repeat|duplicat|identical|same/`` — "tuned to Spark's labels").
* **demo_test** — a NEW test file named for the demo rather than the behaviour
  (``test_sophie_batch5.py``, ``test_unanswered_round_1004_b4.py``); connect-labs
  main carries 56 of them.

Out of scope by construction: docs, lock files, JSON/YAML/CSV, migrations, and seed
/ fixture / walkthrough paths (seed data legitimately names the personas). Tests
are checked for their NAME only. Tune in ``.canopy/ddd/config.yaml`` ``fix_gate:``
(see :mod:`scripts.ddd.loop_config`); ``allow`` regexes waive a finding whose
path or text they match — a waiver lives in config, never as an annotation in the
product.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

OUT_FILE = "fix_gate.json"
CHECKS = ("user_prose", "fix_comment", "persona_name", "literal_rule", "demo_test")

_SKIP_EXT = {
    ".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv", ".lock", ".toml",
    ".cfg", ".ini", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".mp4", ".css", ".scss", ".map", ".snap", ".po", ".mo",
}
_SKIP_PATH = re.compile(
    r"(^|/)(migrations|seeds?|fixtures?|demo_data|synthetic|walkthroughs?|docs|node_modules|vendor|static/vendor"
    r"|tools|scripts|bin|management/commands)(/|$)"
    r"|(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock)$"
    r"|(^|/)seed[^/]*\.py$|(^|/)CLAUDE\.md$|(^|/)\.canopy/",
    re.IGNORECASE,
)
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$|(^|/)conftest\.py$"
)
_TEMPLATE_EXT = {".html", ".htm", ".jinja", ".jinja2", ".j2", ".hbs", ".mustache", ".vue", ".svelte", ".erb"}
_JSX_EXT = {".jsx", ".tsx"}
_HASH_COMMENT_EXT = {".py", ".rb", ".sh", ".pl", ".r"}

# --- what a fix-narrating comment says -----------------------------------------
#: The loop's own vocabulary. Product code has no reason to mention any of it.
_LOOP_WORDS = re.compile(
    r"\bDDD\b|\bbatch\s*#?\d+|\bscene\s*#?\d+|\biter(?:ation)?\s*#?\d+|\bwalk-?through\b"
    r"|\bnarrat(?:ion|ed|or|ive)\b|\bvoice-?over\b|\bjudge[ds]?\b|\bthe\s+(?:demo|viewer|video|recording)\b"
    r"|\bconcept[_ ]eval\b|\bvisual[_ ]judge\b|\bfinding\s+#?\d+",
    re.IGNORECASE,
)
_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
#: A concrete calendar date in a comment is the demo's data, not the product's rule.
_DATA_DATE = re.compile(rf"\b\d{{1,2}}\s+{_MONTHS}\b|\b{_MONTHS}\s+\d{{1,2}}\b(?!\s*:)")
#: Before/after narration — a comment describing what the screen USED to look like.
#: Each one alone is ordinary English; two in one comment block is a story about a fix.
_STORY = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bused to\b",
        r"\bpreviously\b",
        r"\bno longer\b",
        r"\bwere\s+(?:\w+\s+){0,3}lines\b",
        r"\bread as\b",
        r"\breads as\b",
        r"\bnow (?:reads|shows|says|renders|reads as)\b",
        r"\bcontradict\w*",
        r"\bjargon\b",
        r"\bwall of text\b",
        r"\b(?:run|ran|running) together\b",
        r"\bdiffering only\b",
        r"\bon its own line\b",
        r"\bsaid (?:once|twice|three times)\b",
        r"\bnever a sentence\b",
        r"\bslicing through\b",
        r"\blanded under\b",
    )
]

# --- what a rule keyed on wording looks like ------------------------------------
_STR = re.compile(r"""(?P<q>["'`])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)""")
_MULTIWORD = r"""["'][A-Za-z][^"'\n]*\s[^"'\n]*["']"""
_LITERAL_BRANCH = [
    re.compile(rf"(?:===?|!==?)\s*{_MULTIWORD}"),
    re.compile(rf"{_MULTIWORD}\s*(?:===?|!==?)"),
    re.compile(rf"{_MULTIWORD}\s+(?:not\s+)?in\b"),
    re.compile(rf"\bin\s*[\(\[\{{]\s*{_MULTIWORD}"),
    re.compile(rf"\.(?:includes|startswith|endswith|startsWith|endsWith|indexOf|contains|test)\(\s*{_MULTIWORD}"),
    re.compile(rf"\b(?:case|when)\s+{_MULTIWORD}"),
]
_Q = r"""(?:"[^"\n]*"|'[^'\n]*')"""
#: A set / list of quoted phrases a value is matched against: ``frozenset({"a b", "c d"})``.
_PHRASE_SET = re.compile(
    rf"(?:\b(?:frozenset|set)\(\s*[\[{{(]|\bnew\s+Set\(\s*\[|=\s*\{{)\s*(?P<items>{_Q}(?:\s*,\s*{_Q})+)"
)
_REGEX_SOURCES = [
    re.compile(r"""\bre\.(?:compile|search|match|fullmatch|findall|finditer|sub|split)\(\s*[rbu]*(?P<q>["'])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
    re.compile(r"""(?:^|[=(,:!&|?]\s*)/(?P<s>(?:\\.|[^/\n])+)/[gimsuy]*"""),
    re.compile(r"""\bnew\s+RegExp\(\s*(?P<q>["'`])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
]
#: Two or more alternated plain words (a keyword guess), e.g. ``repeat|duplicat|same``.
_WORD_ALTERNATION = re.compile(r"(?<![\\\w])[A-Za-z]{3,}(?:\|[A-Za-z]{3,})+(?![\w])")
_SQL = re.compile(r"\b(?:SELECT|FROM|WHERE|JOIN|INSERT|UPDATE|DELETE|GROUP BY|ORDER BY)\b")
#: Strings a developer reads, not the product's user: logs, assertions, CLI help, errors.
_DEV_STRING = re.compile(
    r"\b(?:logger|logging|log|console)\.\w+\(|\bprint\(|\bwarnings\.warn\(|\bassert\b"
    r"|(?<![_\w])help\s*=|\badd_argument\(|\braise\b|\bthrow\b|\w*(?:Error|Exception)\("
)

# --- demo-named test files ------------------------------------------------------
_DEMO_TEST_TOKENS = re.compile(
    r"(?:^|_)(?:ddd\d*|batch_?\d+|b\d+|scene_?\d+|iter(?:ation)?_?\d+|pass_?\d+|v\d+|\d{3,4}"
    r"|walkthrough|narrative|demo_fix(?:es)?)(?=_|$)",
    re.IGNORECASE,
)

_WORD = re.compile(r"^[A-Za-z][A-Za-z'’-]*[,.;:!?]?$")


@dataclass
class FileDiff:
    path: str
    new_file: bool = False
    # (line number in the new file, text, comment_state) for every added line
    added: list[tuple[int, str, str]] = field(default_factory=list)
    removed_strings: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# diff parsing
# ---------------------------------------------------------------------------


def _ext(path: str) -> str:
    return Path(path).suffix.lower()


def _strip_code_comment(line: str, ext: str) -> tuple[str, str]:
    """Split one code line into (code, trailing comment), quote-aware."""
    hash_comment = ext in _HASH_COMMENT_EXT
    q = None
    i = 0
    while i < len(line):
        ch = line[i]
        if q:
            if ch == "\\":
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "\"'`":
            q = ch
        elif hash_comment and ch == "#":
            return line[:i], line[i + 1 :]
        elif not hash_comment and line.startswith("//", i) and (i == 0 or line[i - 1] != ":"):
            return line[:i], line[i + 2 :]
        i += 1
    return line, ""


def parse_diff(diff: str) -> list[FileDiff]:
    """Added lines per file, each tagged with whether it sits inside a block
    comment / docstring (``"block"``), from a unified diff."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: list[str] = []
    start = 0

    def flush() -> None:
        if cur is not None and hunk:
            _read_hunk(cur, start, hunk)
        hunk.clear()

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            flush()
            cur = None
            continue
        if raw.startswith("+++ "):
            flush()
            path = raw[4:].strip()
            cur = None if path == "/dev/null" else FileDiff(path=path[2:] if path.startswith("b/") else path)
            if cur is not None:
                files.append(cur)
            continue
        if cur is None or raw.startswith("--- "):
            continue
        if raw.startswith("@@"):
            flush()
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            start = int(m.group(1)) if m else 0
            continue
        if raw and raw[0] in "+- ":
            hunk.append(raw)
    flush()
    return files


def _opens_inside_block(lines: list[str], ext: str) -> str | None:
    """The closer of a block comment a hunk STARTS inside of (its opener is above the
    hunk's context) — seen as a closer that comes before any opener."""
    text = "\n".join(lines)
    best = None
    for opener, closer in _block_kinds(ext):
        if opener == closer:
            continue  # a docstring quote cannot tell an opener from a closer
        c = text.find(closer)
        o = text.find(opener)
        if c >= 0 and (o < 0 or c < o) and (best is None or c < best[0]):
            best = (c, closer)
    return best[1] if best else None


def _read_hunk(fd: FileDiff, new_ln: int, raws: list[str]) -> None:
    ext = _ext(fd.path)
    new_side = [r[1:] for r in raws if r[0] in "+ "]
    block = _opens_inside_block(new_side, ext)
    for raw in raws:
        sign, text = raw[0], raw[1:]
        if sign == "-":
            for m in _STR.finditer(text):
                fd.removed_strings.add(m.group("s").strip())
            if ext in _TEMPLATE_EXT:
                fd.removed_strings.update(_template_text(text))
            continue
        state = block or ""
        block = _advance_block(text, ext, block)
        if sign == "+":
            fd.added.append((new_ln, text, "block" if (state or block) else ""))
        new_ln += 1


def _mark_new_files(diff: str, files: list[FileDiff]) -> None:
    new_paths: set[str] = set()
    pending = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            pending = False
        elif raw.startswith("new file mode"):
            pending = True
        elif raw.startswith("+++ ") and pending:
            p = raw[4:].strip()
            new_paths.add(p[2:] if p.startswith("b/") else p)
    for f in files:
        f.new_file = f.path in new_paths


_BLOCK_TOKENS = {
    "py": [('"""', '"""'), ("'''", "'''")],
    "c": [("/*", "*/")],
    "tpl": [("{#", "#}"), ("<!--", "-->"), ("{% comment %}", "{% endcomment %}"), ("{/*", "*/}")],
}


def _block_kinds(ext: str) -> list[tuple[str, str]]:
    if ext == ".py":
        return _BLOCK_TOKENS["py"]
    if ext in _TEMPLATE_EXT:
        return _BLOCK_TOKENS["tpl"] + _BLOCK_TOKENS["c"]
    return _BLOCK_TOKENS["c"] + (_BLOCK_TOKENS["tpl"] if ext in _JSX_EXT else [])


def _advance_block(text: str, ext: str, block: str | None) -> str | None:
    """The open block-comment closer after this line (None when none is open)."""
    i = 0
    while i <= len(text):
        if block:
            j = text.find(block, i)
            if j < 0:
                return block
            i = j + len(block)
            block = None
            continue
        best = None
        for opener, closer in _block_kinds(ext):
            j = text.find(opener, i)
            if j >= 0 and (best is None or j < best[0]):
                best = (j, opener, closer)
        if best is None:
            return None
        i = best[0] + len(best[1])
        block = best[2]
    return block


# ---------------------------------------------------------------------------
# per-line extraction
# ---------------------------------------------------------------------------

_TPL_COMMENT = re.compile(r"\{#.*?#\}|<!--.*?-->|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{/\*.*?\*/\}", re.S)
_TPL_CODE = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|<[^>]*>|\{[^{}]*\}", re.S)


def _template_text(line: str) -> list[str]:
    """User-visible text runs on a template line (tags, template code and comments removed)."""
    text = _TPL_COMMENT.sub(" ", line)
    text = _TPL_CODE.sub("\n", text)
    return [seg.strip() for seg in text.split("\n") if seg.strip()]


def _comment_text(line: str, ext: str, state: str) -> str:
    """The comment / docstring text on one added line ('' when there is none)."""
    if state == "block":
        s = line.strip()
        for tok in ('"""', "'''", "/**", "/*", "*/", "{#", "#}", "<!--", "-->", "{% comment %}", "{% endcomment %}"):
            s = s.replace(tok, " ")
        s = s.lstrip("*").strip()
        if ext in _TEMPLATE_EXT or ext in _JSX_EXT:
            # A template line can close its comment and carry markup after it.
            s = re.sub(r"<[^>]*>|\{%.*?%\}|\{\{.*?\}\}", " ", s)
        return s.strip()
    found = [m.group(0) for m in _TPL_COMMENT.finditer(line)] if ext in _TEMPLATE_EXT | _JSX_EXT else []
    if found:
        return " ".join(_comment_text(c, ext, "block") for c in found)
    if ext in _TEMPLATE_EXT:
        return line.strip()[2:].strip() if line.strip().startswith("//") else ""
    code, comment = _strip_code_comment(line, ext)
    stripped = line.strip()
    if ext == ".py" and (stripped.startswith('"""') or stripped.startswith("'''")):
        return _comment_text(line, ext, "block")
    if ext not in _HASH_COMMENT_EXT and stripped.startswith("/*"):
        return _comment_text(line, ext, "block")
    return comment.strip()


def _code_part(line: str, ext: str, state: str) -> str:
    if ext in _TEMPLATE_EXT and line.strip().startswith("//"):
        return ""  # a script block's comment inside a template
    if state == "block" or ext in _TEMPLATE_EXT:
        return "" if state == "block" else _TPL_COMMENT.sub(" ", line)
    stripped = line.strip()
    if ext == ".py" and (stripped.startswith('"""') or stripped.startswith("'''")):
        return ""
    return _strip_code_comment(line, ext)[0]


def _prose_words(s: str) -> int:
    toks = s.split()
    words = [t for t in toks if _WORD.match(t)]
    if not toks or len(words) / len(toks) < 0.6:
        return 0
    return len(words)


def _looks_like_prose(s: str, min_words: int) -> bool:
    if "://" in s or _SQL.search(s) or "\\n" in s and s.count("\\n") > 2:
        return False
    return _prose_words(s) >= min_words


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    prose_words: int = 7
    template_comment_words: int = 15
    demo_terms: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    skip_paths: tuple[str, ...] = ()


def _config(cfg: Any) -> GateConfig:
    if isinstance(cfg, GateConfig):
        return cfg
    if cfg is None:
        try:
            from scripts.ddd import loop_config

            cfg = loop_config.load().fix_gate
        except Exception:
            return GateConfig()
    return GateConfig(
        prose_words=getattr(cfg, "prose_words", 7),
        template_comment_words=getattr(cfg, "template_comment_words", 15),
        demo_terms=tuple(getattr(cfg, "demo_terms", ()) or ()),
        allow=tuple(getattr(cfg, "allow", ()) or ()),
        skip_paths=tuple(getattr(cfg, "skip_paths", ()) or ()),
    )


def persona_terms(spec: str | Path | None) -> list[str]:
    """First and last names of every persona the spec declares (the names that must
    never reach product code)."""
    if not spec or not Path(spec).exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(Path(spec).read_text()) or {}
    except Exception:
        return []
    personas = data.get("personas") if isinstance(data, dict) else None
    out: list[str] = []
    for p in (personas or {}).values() if isinstance(personas, dict) else []:
        name = str((p or {}).get("name") or "") if isinstance(p, dict) else ""
        for tok in re.findall(r"[A-Z][a-zA-Z'’-]{2,}", name):
            if tok not in out:
                out.append(tok)
    return out


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def _finding(check: str, path: str, line: int | None, text: str, why: str, fix: str) -> dict:
    return {
        "check": check,
        "path": path,
        "line": line,
        "text": text.strip()[:240],
        "why": why,
        "fix": fix,
    }


_FIX = {
    "user_prose": "Delete the sentence, or express it as structure the product already has — a "
    "label, a status chip, a count, a table column. Do not move it into a tooltip, legend or "
    "collapsed panel.",
    "fix_comment": "Delete the comment. If the code needs explaining, rename or restructure it; "
    "the PR body is where a fix is narrated, never the product.",
    "persona_name": "Remove the name. Product code must work for any record; if the behaviour "
    "only made sense for this persona's data, it is a special case — generalize it or drop it.",
    "literal_rule": "Key the rule on data the product already models (a status, a kind, a "
    "registry field), not on the wording of a label. If no such field exists, that is the fix.",
    "demo_test": "Name the test file for the behaviour it guards (test_<module>_<behaviour>.py) "
    "and fold it into the module's existing tests.",
}


def _story_hits(text: str) -> list[str]:
    return [p.pattern for p in _STORY if p.search(text)]


def _comment_blocks(fd: FileDiff, ext: str) -> list[tuple[int, str, bool]]:
    """Contiguous added comment lines joined into blocks: (first line, text, markup)
    — ``markup`` when the block is a template's own comment, not a script's ``//``."""
    blocks: list[tuple[int, str, bool]] = []
    cur_ln, cur, markup, last = None, [], False, None
    for ln, text, state in fd.added:
        c = _comment_text(text, ext, state)
        line_markup = ext in _TEMPLATE_EXT and not text.strip().startswith("//")
        if c and cur and last is not None and ln == last + 1:
            cur.append(c)
            markup = markup or line_markup
        else:
            if cur:
                blocks.append((cur_ln, " ".join(cur), markup))
            cur_ln, cur, markup = (ln, [c], line_markup) if c else (None, [], False)
        last = ln
    if cur:
        blocks.append((cur_ln, " ".join(cur), markup))
    return blocks


def check_file(fd: FileDiff, cfg: GateConfig, terms: Iterable[str]) -> list[dict]:
    path, ext = fd.path, _ext(fd.path)
    out: list[dict] = []
    if _TEST_PATH.search(path):
        if fd.new_file:
            stem = Path(path).name.split(".")[0]
            hits = [m.group(0).strip("_") for m in _DEMO_TEST_TOKENS.finditer(stem)]
            hits += [t for t in terms if re.search(rf"(?:^|_){re.escape(t.lower())}(?:_|$)", stem.lower())]
            if hits:
                out.append(
                    _finding(
                        "demo_test", path, None, Path(path).name,
                        f"new test file named for the demo, not the behaviour ({', '.join(hits)})",
                        _FIX["demo_test"],
                    )
                )
        return out

    term_res = [(t, re.compile(rf"\b{re.escape(t)}\b")) for t in terms]
    template = ext in _TEMPLATE_EXT
    prev_code = ""
    for ln, text, state in fd.added:
        code = _code_part(text, ext, state)

        for t, rx in term_res:
            if rx.search(text):
                out.append(
                    _finding("persona_name", path, ln, text, f"persona/demo name '{t}' in product code", _FIX["persona_name"])
                )
                break

        # user-visible prose
        segs: list[str] = []
        if template:
            segs += _template_text(code)
        if ext in _JSX_EXT and code:
            segs += [s.strip() for s in re.findall(r">([^<>{}]+)<", code)]
            segs += [s.strip() for s in re.findall(r"^\s*([A-Za-z][^<>{}=;()]*[.?!]?)\s*$", code) if not re.search(r"[=;(){}]", s)]
        if code and not _DEV_STRING.search(code) and not _DEV_STRING.search(prev_code):
            segs += [m.group("s").strip() for m in _STR.finditer(code)]
        for s in segs:
            if s in fd.removed_strings or not _looks_like_prose(s, cfg.prose_words):
                continue
            out.append(
                _finding(
                    "user_prose", path, ln, s,
                    f"new user-visible sentence ({_prose_words(s)} words)", _FIX["user_prose"],
                )
            )

        # rules keyed on wording
        if code:
            why = None
            if any(p.search(code) for p in _LITERAL_BRANCH):
                why = "branch / membership test on a free-text literal"
            elif any(
                sum(1 for q in _STR.finditer(m.group("items")) if " " in q.group("s").strip()) >= 2
                for m in _PHRASE_SET.finditer(code)
            ):
                why = "a set of phrases matched against text"
            else:
                for rx in _REGEX_SOURCES:
                    m = rx.search(code)
                    if m and _WORD_ALTERNATION.search(m.group("s")):
                        why = f"keyword regex guessing meaning from wording (/{m.group('s')[:60]}/)"
                        break
            if why:
                out.append(_finding("literal_rule", path, ln, text, why, _FIX["literal_rule"]))
        prev_code = code

    # fix-narrating comments, judged per contiguous block
    for ln, block, markup in _comment_blocks(fd, ext):
        reasons = []
        m = _LOOP_WORDS.search(block)
        if m:
            reasons.append(f"names the loop ('{m.group(0)}')")
        story = _story_hits(block)
        m = _DATA_DATE.search(block)
        if m:
            story.append(f"a demo date ('{m.group(0)}')")
        if len(story) >= 2:
            reasons.append("tells the before/after story of a fix" + (f", citing {story[-1]}" if m else ""))
        if markup and len(block.split()) >= cfg.template_comment_words:
            reasons.append(f"a {len(block.split())}-word template comment explaining the UI")
        if reasons:
            out.append(_finding("fix_comment", path, ln, block, "; ".join(reasons), _FIX["fix_comment"]))
    return out


def gate(diff: str, *, terms: Iterable[str] = (), cfg: Any = None) -> dict[str, Any]:
    """Run every check over a unified diff. ``status`` is ``pass`` or ``fail``."""
    gcfg = _config(cfg)
    all_terms = list(dict.fromkeys([*terms, *gcfg.demo_terms]))
    files = parse_diff(diff)
    _mark_new_files(diff, files)
    skip = [re.compile(p) for p in gcfg.skip_paths]
    allow = [re.compile(p) for p in gcfg.allow]
    findings: list[dict] = []
    waived: list[dict] = []
    checked: list[str] = []
    for fd in files:
        if _ext(fd.path) in _SKIP_EXT or _SKIP_PATH.search(fd.path) or any(r.search(fd.path) for r in skip):
            continue
        checked.append(fd.path)
        for f in check_file(fd, gcfg, all_terms):
            if any(r.search(f["path"]) or r.search(f["text"]) for r in allow):
                waived.append(f)
            else:
                findings.append(f)
    counts = {c: sum(1 for f in findings if f["check"] == c) for c in CHECKS}
    return {
        "status": "fail" if findings else "pass",
        "counts": counts,
        "files_checked": checked,
        "terms": all_terms,
        "findings": findings,
        "waived": waived,
    }


def git_diff(repo: str | Path, base: str, head: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-color", "--no-ext-diff", "-U25", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def stamp(state: Any, result: dict, *, head: str | None = None) -> dict:
    """Record the verdict on ``state.fix_gate`` (what judge_gate reads)."""
    record = {
        "status": result["status"],
        "counts": result["counts"],
        "head": head,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    state.fix_gate = record
    return record


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.fix_gate")
    ap.add_argument("repo", nargs="?", default=".", help="the target repo holding the fix branch")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--diff-file", default=None, help="read the diff from a file instead of git")
    ap.add_argument("--spec", default=None, help="unified spec — its personas' names are banned in product code")
    ap.add_argument("--term", action="append", default=[], help="another demo name to ban (repeatable)")
    ap.add_argument("--run-dir", default=None, help=f"write {OUT_FILE} here")
    ap.add_argument("--run-id", default=None, help="stamp state.fix_gate on this run")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        diff = Path(args.diff_file).read_text() if args.diff_file else git_diff(args.repo, args.base, args.head)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"fix_gate: could not read the diff: {exc}", file=sys.stderr)
        return 2
    head = None
    if not args.diff_file:
        try:
            head = subprocess.run(
                ["git", "-C", args.repo, "rev-parse", args.head], check=True, capture_output=True, text=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            head = None
    result = gate(diff, terms=[*persona_terms(args.spec), *args.term])
    result["base"], result["head"] = args.base, head or args.head
    if args.run_dir:
        Path(args.run_dir, OUT_FILE).write_text(json.dumps(result, indent=1) + "\n")
    if args.run_id:
        from scripts.ddd.runstate import load, save

        state = load(args.run_id)
        stamp(state, result, head=head)
        save(state)

    if args.json:
        print(json.dumps(result, indent=1))
    else:
        fs = result["findings"]
        print(
            f"fix_gate: {result['status'].upper()} — {len(fs)} finding(s) over "
            f"{len(result['files_checked'])} product file(s)"
            + (f", {len(result['waived'])} waived by fix_gate.allow" if result["waived"] else "")
        )
        for f in fs:
            loc = f"{f['path']}:{f['line']}" if f["line"] else f["path"]
            print(f"  [{f['check']}] {loc} — {f['why']}\n      {f['text'][:160]}")
        if fs:
            print(
                "Send these back to the fixer: restructure, generalize or delete — never annotate. "
                "Re-run the gate on the reworked branch before merging."
            )
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(_main())
