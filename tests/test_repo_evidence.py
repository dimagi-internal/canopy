"""repo_evidence — the "is this already in origin/main?" helpers.

Regression guard for 2026-08-19. `git grep <pat>` and `open(repo/'CHANGELOG.md')` read the
WORKING TREE, while both callers (agent_review's source-verification gate and
verify_findings) fetch origin/main first and present the results AS origin/main. Agent
repos are normally parked on a feature branch — work happens in worktrees, so the main
checkout is left wherever it last was (`hal` on `hal/cursor-version-note`, `chrome-sales`
on a feature branch, both measured the same day). So the gate routinely answered about
the wrong tree, and a finding whose fix was plainly on main survived as "not fixed".

The second half: a symbol naming a FILE was grepped as a content pattern, so a fix living
in the target's own body was invisible — `git grep 'skills/x/SKILL.md'` searches for that
string inside files and never opens the file.
"""
import subprocess
from pathlib import Path

import pytest

from orchestrator.repo_evidence import changelog_head, grep_repo


def _run(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                   capture_output=True)


@pytest.fixture
def parked_repo(tmp_path):
    """An 'origin' whose main carries THE FIX, and a clone parked on a feature branch
    whose working tree does NOT — the exact shape measured on hal."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _run(origin, "init", "-q", "-b", "main")
    _run(origin, "config", "user.email", "t@t.t")
    _run(origin, "config", "user.name", "t")
    (origin / "SKILL.md").write_text("placeholder\n")
    (origin / "CHANGELOG.md").write_text("# Changelog\n\n## v1 - old\n")
    _run(origin, "add", "-A")
    _run(origin, "commit", "-qm", "base")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True,
                   capture_output=True)
    _run(clone, "config", "user.email", "t@t.t")
    _run(clone, "config", "user.name", "t")

    # main advances with the fix...
    (origin / "SKILL.md").write_text(
        "# Skill\n\nInvoke this by its full name — `hal:agent-turn-review`. Never bare.\n")
    (origin / "CHANGELOG.md").write_text("# Changelog\n\n## v2 - THE FIX LANDED\n\n## v1 - old\n")
    _run(origin, "add", "-A")
    _run(origin, "commit", "-qm", "ship the fix")

    # ...and the clone fetches it but stays PARKED on a feature branch without it.
    _run(clone, "fetch", "-q", "origin", "main")
    _run(clone, "checkout", "-q", "-b", "parked/feature")
    assert "hal:agent-turn-review" not in (clone / "SKILL.md").read_text()
    return clone


def test_grep_finds_a_fix_that_is_on_main_but_not_in_the_parked_work_tree(parked_repo):
    """The 2026-08-19 miss: work tree says nothing, origin/main has it."""
    out = grep_repo(parked_repo, ["hal:agent-turn-review"])
    assert "(no hits)" not in out
    assert "hal:agent-turn-review" in out


def test_grep_labels_which_ref_it_searched(parked_repo):
    """An unlabelled result is what made the old behaviour confusing rather than wrong-
    looking. The scope is always stated."""
    assert "searched: origin/main" in grep_repo(parked_repo, ["anything"])


def test_file_target_shows_its_own_content_not_just_a_name_grep(parked_repo):
    """A path symbol gets the file's HEAD, because a fix often lives in the target's own
    body where grepping for the path name can never see it."""
    out = grep_repo(parked_repo, ["SKILL.md"])
    assert "FILE EXISTS" in out
    assert "hal:agent-turn-review" in out, "the target's own content must be surfaced"


def test_changelog_comes_from_main_not_the_parked_tree(parked_repo):
    head = changelog_head(parked_repo)
    assert "THE FIX LANDED" in head
    assert (parked_repo / "CHANGELOG.md").read_text().count("THE FIX LANDED") == 0


def test_symbols_are_matched_literally_not_as_regex(tmp_path):
    """`-F`: a symbol with regex metacharacters must not silently widen the search."""
    repo = tmp_path / "r"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "t@t.t")
    _run(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("literal a.b here\nand axb decoy\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "c")
    out = grep_repo(repo, ["a.b"])
    assert "literal a.b here" in out
    assert "axb decoy" not in out, "'.' was treated as a regex wildcard"


def test_missing_file_target_does_not_claim_it_exists(parked_repo):
    out = grep_repo(parked_repo, ["does/not/exist.md"])
    assert "FILE EXISTS" not in out
    assert "(no hits)" in out


def test_falls_back_to_work_tree_and_says_so_when_there_is_no_main(tmp_path):
    """Fail open on a repo with no origin/main — but LABEL it, so the caller is never
    told work-tree evidence is main evidence."""
    repo = tmp_path / "solo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "dev")
    _run(repo, "config", "user.email", "t@t.t")
    _run(repo, "config", "user.name", "t")
    (repo / "f.md").write_text("findme\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "c")
    out = grep_repo(repo, ["findme"])
    assert "WORKING TREE" in out and "may not reflect main" in out
    assert "findme" in out


def test_no_symbols_returns_empty(parked_repo):
    assert grep_repo(parked_repo, []) == ""
