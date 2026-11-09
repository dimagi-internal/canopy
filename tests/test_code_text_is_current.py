"""Docstrings, comments and `_doc` strings in the code are current state.

Sibling of tests/test_cli_text_is_current.py (which reads what `--help` prints) and
tests/test_claude_md_budget.py. A date, a PR/issue number, a deleted-spec path or a person's
name says WHEN or WHO decided something; "used to" / "no longer" / "formerly" narrate what the
code was before. That is history, and history lives in the PR description and `git log`. A
rule's REASON stays, in the present tense ("the pattern is anchored because quoted mentions
must not trip it").

Runtime strings are held to the same rule for issue/PR numbers (``test_runtime_strings_cite_no_issue_numbers``):
an error, warning or help text a person reads says what is true and what to do, never "(canopy#123)".

A release number ("0.2.528"), a compact run id ("20261015-1129"), a year-month ("2026-08"), a task
number ("canopy-web T76") and "any more" date or narrate a statement the same way.

Scans the comments and docstrings of every .py / .sh / .ts / .js / .yml under `src/`, `plugins/canopy/`,
`scripts/`, `hooks/`, `packages/canopy_agent_factory/` and `.github/` (test files of the plugin excluded), and the
`_doc`-style strings of `plugins/canopy/agent-core/gating-baseline.json`. Run
`python tests/test_code_text_is_current.py` to list the hits.

A date that is DATA (an API version, a run-id format, a typed example value) is not history:
add it to ALLOW with the reason. The list can only shrink; a stale entry fails the suite.
"""
from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIRS = ["src", "plugins/canopy", "scripts", "hooks", "packages/canopy_agent_factory", ".github"]
SKIP = ("node_modules", "/test/", "video-engine", "__pycache__", ".venv")
JSON_FILES = ["plugins/canopy/agent-core/gating-baseline.json"]

PATTERNS = {
    "date": r"\b(?:19|20)\d\d-\d\d-\d\d\b",
    "pr": r"\(#\d+\)|\bPR #?\d+|[\w-]+#\d{2,}|\b[Ii]ssue #?\d+|(?<![\w&])#\d{3,}\b",
    "spec": r"docs/(?:superpowers|plans|specs)",
    "name": (
        r"\b(?:Jonathan|Jon|Jackson|Shayoni|Mazumdar|Matthew|Matt|Hayto|Theis|Mary|Rocheleau|"
        r"Gillian|Javetski|Amie|Vaccaro|Andrea)\b|\bjj-mbp[\w-]*\b"
    ),
    # A release number or a compact run id dates a statement as surely as an ISO date does.
    "version": r"\b0\.2\.\d{2,3}\b",
    "runid": r"\b20\d{6}-\d{4}\b",
    "month": r"\b(?:19|20)\d\d-(?:0[1-9]|1[0-2])\b(?!-\d)",
    "task": r"\b(?:canopy-web|hal|ace|eva|ada|echo) T\d{2,3}\b",
    "narrative": (
        r"\b(?:formerly|(?:was|were) previously|no longer|used to (?:be|have|do|read|return|drop|"
        r"stop|fold|write|pass|score|truncate|guess|compare|mean|disagree|decode|narrate|publish|"
        r"enroll|hand|come|work|run|go|take|fall|happen|silently|END|shell|reach|launch))\b|\bUSED to\b|\bany ?more\b"
    ),
}
RX = {k: re.compile(v) for k, v in PATTERNS.items()}

# For string literals: the issue/PR forms only. The bare `#123` form in PATTERNS also matches CSS
# colours and HTML entities, which are data in a string.
STRING_REF = re.compile(
    r"\(#\d+\)|\b(?:PR|GitHub|[Ii]ssue)(?: \w+)? #?\d+|[A-Za-z][\w-]*#\d{2,}\b(?![\w;])"
)

# (path, regex over the hit's context, reason). Each must still match something.
ALLOW = [
    (
        "src/orchestrator/inbox_rules.py",
        r".",
        "vendored verbatim by canopy-web's runner and hash-pinned (tests/test_inbox_rules.py): "
        "a text edit needs a coordinated re-vendor and new pins in both repos",
    ),
    (
        "src/orchestrator/device_key_aws.py",
        r"Version=2011-06-15",
        "the AWS STS API version in the sample request body: data",
    ),
    (
        "scripts/ddd/narrative.py",
        r"-2026-0[16]-0[14]-001",
        "example run ids showing the -YYYY-MM-DD-NNN stamp that _RUN_ID_STAMP_RE parses: data",
    ),
    (
        "scripts/walkthrough/_lib/recorder.py",
        r'typed "2026-08-18"',
        "an example input value in a bug explanation, not a reference to when anything happened",
    ),
    (
        "plugins/canopy/scripts/canopy-update-check.sh",
        r"0\.2\.369 0\.2\.369",
        "an example of the UPGRADE_AVAILABLE line the script prints when a version is reused: data",
    ),
    (
        "src/orchestrator/doctor.py",
        r"canopy-0\.2\.342\.dist-info",
        "an example dist-info directory name showing how the version is cut out: data",
    ),
    (
        "src/orchestrator/provenance.py",
        r"canopy-cli/0\.2\.590",
        "an example X-Canopy-Client header value: data",
    ),
    (
        "src/orchestrator/skill_catalog.py",
        r'"installed_version": "0\.2\.79"',
        "an example catalog row: data",
    ),
    (
        "plugins/canopy/skills/doc-regeneration/scripts/find_history.py",
        r".",
        "the tool that finds history in docs: its patterns and examples are its subject matter",
    ),
    (
        "plugins/canopy/skills/doc-regeneration/scripts/docs_ignore.py",
        r".",
        "documents the .docs-ignore syntax with an example date: data",
    ),
]


def _py_texts(src: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            if t.type == tokenize.COMMENT:
                out.append((t.start[0], t.string))
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                d = ast.get_docstring(n, clean=False)
                if d and n.body:
                    out.append((n.body[0].lineno, d))
    except (SyntaxError, tokenize.TokenError):
        pass
    return out


def _py_strings(src: str) -> list[tuple[int, str]]:
    """Every string literal that is not a docstring (those are scanned by `_py_texts`)."""
    out: list[tuple[int, str]] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            out.append((n.lineno, n.value))
    return out


def _line_texts(src: str, prefixes: tuple[str, ...]) -> list[tuple[int, str]]:
    return [(i, l) for i, l in enumerate(src.splitlines(), 1) if l.lstrip().startswith(prefixes)]


def _json_docs(src: str) -> list[tuple[int, str]]:
    """Every string under a key that is a `_...doc` note, or a rail's `message`."""
    out: list[tuple[int, str]] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and (k.startswith("_") and k.endswith("doc") or k == "message"):
                    out.append((0, v))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(json.loads(src))
    return out


def _files() -> list[Path]:
    found = []
    for d in DIRS:
        for p in (ROOT / d).rglob("*"):
            s = str(p)
            if p.is_file() and p.suffix in (".py", ".sh", ".ts", ".js", ".yml", ".yaml") and not any(x in s for x in SKIP):
                found.append(p)
    return sorted(found)


def scan() -> list[tuple[str, int, str, str]]:
    """(path, line, category, context) for every history hit in comments and docstrings."""
    hits = []
    targets = [(p, p.read_text(errors="replace")) for p in _files()]
    targets += [(ROOT / j, (ROOT / j).read_text()) for j in JSON_FILES]
    for p, src in targets:
        if p.suffix == ".py":
            texts = _py_texts(src)
        elif p.suffix in (".sh", ".yml", ".yaml"):
            texts = _line_texts(src, ("#",))
        elif p.suffix == ".json":
            texts = _json_docs(src)
        else:
            texts = _line_texts(src, ("//", "*", "/*"))
        rel = str(p.relative_to(ROOT))
        for line, text in texts:
            for cat, rx in RX.items():
                for m in rx.finditer(text):
                    hits.append((rel, line, cat, text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")))
    return hits


def scan_strings() -> list[tuple[str, int, str, str]]:
    """(path, line, 'ref', context) for every issue/PR number in a .py string literal."""
    hits = []
    for p in _files():
        if p.suffix != ".py" or "/tests/" in str(p):   # a test's strings are its fixtures
            continue
        rel = str(p.relative_to(ROOT))
        for line, text in _py_strings(p.read_text(errors="replace")):
            for m in STRING_REF.finditer(text):
                hits.append((rel, line, "ref", text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")))
    return hits


# What deleting a number or a date from a sentence leaves behind: "#.", ", ,", " ," and a lone " ."
# before the end of a sentence. Kept narrow so a code example in a docstring never trips it.
ARTIFACTS = re.compile(r"#\.|,[ \t]*,|(?<=[A-Za-z]) ,(?=\s)|(?<=[a-z]) \.[ \t]*$|(?<=[a-z]) \.(?= [A-Z])", re.M)


def scan_artifacts() -> list[tuple[str, int, str, str]]:
    hits = []
    for p in _files():
        src = p.read_text(errors="replace")
        if p.suffix == ".py":
            texts = _py_texts(src)
        elif p.suffix in (".sh", ".yml", ".yaml"):
            texts = _line_texts(src, ("#",))
        else:
            texts = _line_texts(src, ("//", "*", "/*"))
        rel = str(p.relative_to(ROOT))
        for line, text in texts:
            for m in ARTIFACTS.finditer(text):
                hits.append((rel, line, "artifact", text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")))
    return hits


def test_comment_text_has_no_number_removal_artifacts():
    bad = [h for h in scan_artifacts() if not _allowed(h)]
    assert not bad, (
        "A comment or docstring carries punctuation left by a deleted number or date:\n  "
        + "\n  ".join(f"{p}:{ln} ...{ctx}..." for p, ln, _, ctx in bad)
    )


@pytest.mark.parametrize("text", ["see #.", "is, , and", "the word , then", "done now .", "end . The next"])
def test_the_artifact_pattern_catches_leftovers(text):
    assert ARTIFACTS.search(text)


@pytest.mark.parametrize("text", ["the first . ! or ?", "a (#N) example", "x, y and z", "e.g. a, b."])
def test_the_artifact_pattern_leaves_prose_alone(text):
    assert not ARTIFACTS.search(text)


def _allowed(hit: tuple[str, int, str, str]) -> bool:
    return any(hit[0] == path and re.search(rx, hit[3]) for path, rx, _ in ALLOW)


def test_code_text_is_current_state():
    bad = [h for h in scan() if not _allowed(h)]
    assert not bad, (
        "A docstring, comment or `_doc` string names a date, a PR/issue, a deleted-spec path, a person "
        "or narrates a change (history, not current behaviour):\n  "
        + "\n  ".join(f"{p}:{ln} [{cat}] ...{ctx}..." for p, ln, cat, ctx in bad)
        + "\nState the rule and its reason as they hold now; put when, why and who in the PR description."
    )


def test_runtime_strings_cite_no_issue_numbers():
    bad = [h for h in scan_strings() if not _allowed(h)]
    assert not bad, (
        "A string literal (an error, warning, help or template text) cites an issue or PR number. "
        "Say what is true and what to do; the number belongs in the PR description:\n  "
        + "\n  ".join(f"{p}:{ln} ...{ctx}..." for p, ln, _, ctx in bad)
    )


def test_allowlist_has_no_stale_entries():
    hits = scan() + scan_strings()
    stale = [(p, rx) for p, rx, _ in ALLOW if not any(h[0] == p and re.search(rx, h[3]) for h in hits)]
    assert not stale, f"ALLOW entries that match nothing any more (delete them): {stale}"


@pytest.mark.parametrize(
    "text",
    [
        "# fixed on 2026-09-29",
        "# see canopy#849",
        "# (#433)",
        "# Jonathan asked for this",
        "# this used to return the empty string",
        "# no longer reads the file",
        "see docs/superpowers/specs/x.md",
        "# formerly `paired_by`",
        "# Until 0.2.563 a run lived on one machine",
        "# on noora-health/20261015-1129 the loop stalled",
        "# pre-2026-07 location",
        "# the project it belongs to (canopy-web T76)",
        "# nothing reads it any more",
        "# tests/walkthrough USED to be excluded",
    ],
)
def test_the_patterns_catch_what_they_are_for(text):
    assert any(rx.search(text) for rx in RX.values())


@pytest.mark.parametrize(
    "text",
    [
        "# anchored so a quoted mention does not trip it",
        "# the id is #fff and the heading is #1",
        "# version 1.2.3 of the API",
        "# is used to find the folder",
        "# Used to guess which skill name a proposal means",
        "# the run id format is YYYYMMDD-HHMM",
        "# task T22 on the board",
    ],
)
def test_the_patterns_leave_current_state_alone(text):
    assert not any(rx.search(text) for rx in RX.values())


if __name__ == "__main__":
    for h in scan():
        print(f"{h[0]}:{h[1]} [{h[2]}] ...{h[3]}...")


@pytest.mark.parametrize(
    "text",
    ["it 404s (canopy-web#1337).", "see canopy issue #451", "(GitHub #357)", "tracking ace#2805", "fixed in PR 123"],
)
def test_the_string_pattern_catches_issue_numbers(text):
    assert STRING_REF.search(text)


@pytest.mark.parametrize(
    "text",
    ["color: #111111;", "background:#000;", "&#1234;", "as owner/repo#number", "issue number", "item #fafafa"],
)
def test_the_string_pattern_leaves_data_alone(text):
    assert not STRING_REF.search(text)
