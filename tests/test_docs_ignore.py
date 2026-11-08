"""`.docs-ignore`: the one place check_refs.py and find_history.py are told what is not a defect.

Unit tests for the parser and both scripts' use of it, plus a run of both scripts on the
repo's own docs: no broken reference, no history, and no ignore entry nothing matches.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins" / "canopy" / "skills" / "doc-regeneration" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import docs_ignore  # noqa: E402

PAGES = ["CLAUDE.md", "README.md", "CONTRIBUTING.md", *sorted(
    p.relative_to(ROOT).as_posix() for pat in ("docs/*.md", "docs/architecture/*.md") for p in ROOT.glob(pat)
)]


def _run(script: str, *args: str, cwd: Path = ROOT):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / script), *args], cwd=cwd, capture_output=True, text=True
    )


def test_every_entry_needs_a_tool_a_pattern_and_a_reason():
    for bad in ("check_refs foo/bar.md", "check_refs foo/bar.md  #", "nope foo  # why", "check_refs  # why"):
        with pytest.raises(docs_ignore.IgnoreError):
            docs_ignore.parse(bad)
    ok = docs_ignore.parse("# comment\n\ncheck_refs a/b.md  # other repo\nfind_history d.md::2026-01-01  # record\n")
    assert [(r.tool, r.doc_glob, r.pattern, r.reason) for r in ok.rules] == [
        ("check_refs", "*", "a/b.md", "other repo"),
        ("find_history", "d.md", "2026-01-01", "record"),
    ]


def test_matching_scopes_to_doc_and_counts_hits():
    ign = docs_ignore.parse("check_refs docs/a.md::dist/*  # build output\nfind_history d.md  # record\n")
    assert ign.ref("docs/a.md", "dist/app.js") and not ign.ref("docs/b.md", "dist/app.js")
    assert ign.history("d.md", "anything 2026-01-01") and not ign.history("e.md", "x")
    assert ign.unused("check_refs") == [] and ign.unused("find_history") == []
    assert [r.pattern for r in docs_ignore.parse("check_refs x  # r\n").unused("check_refs")] == ["x"]


def test_both_scripts_read_the_ignore_file(tmp_path):
    (tmp_path / "docs").mkdir()
    doc = tmp_path / "docs" / "a.md"
    doc.write_text("See `other/repo/file.py` and docs/superpowers/specs/2026-10-06-x.\nFixed in PR 12 on 2026-01-02.\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert _run("check_refs.py", "--root", str(tmp_path), str(doc)).returncode == 0  # not rooted here: not a ref
    doc.write_text("See `docs/gone.md`.\nFixed in PR 12 on 2026-01-02.\n")
    assert _run("check_refs.py", "--root", str(tmp_path), str(doc)).returncode == 1
    assert _run("find_history.py", "--root", str(tmp_path), str(doc)).returncode == 1
    (tmp_path / ".docs-ignore").write_text(
        "check_refs docs/gone.md  # another repo's file\nfind_history docs/a.md::PR 12  # a recorded decision\n"
    )
    r = _run("check_refs.py", "--root", str(tmp_path), str(doc))
    assert r.returncode == 0 and "ignored 1" in r.stdout, r.stdout
    r = _run("find_history.py", "--root", str(tmp_path), str(doc))
    assert r.returncode == 0 and "ignored 1" in r.stdout, r.stdout
    # --no-ignore shows the truth again
    assert _run("check_refs.py", "--no-ignore", "--root", str(tmp_path), str(doc)).returncode == 1
    assert _run("find_history.py", "--no-ignore", "--root", str(tmp_path), str(doc)).returncode == 1


def test_dated_slug_of_a_design_record_is_not_history(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("The design is `docs/superpowers/specs/2026-10-06-caller-brain` and 2026-10-06-caller-brain.md.\n")
    assert _run("find_history.py", str(doc)).returncode == 0


def test_repo_docs_are_clean_and_the_ignore_file_has_no_stale_entry():
    refs = _run("check_refs.py", *PAGES)
    assert refs.returncode == 0 and "UNUSED" not in refs.stdout, refs.stdout
    hist = _run("find_history.py", *PAGES)
    assert hist.returncode == 0 and "UNUSED" not in hist.stdout, hist.stdout
