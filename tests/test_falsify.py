"""falsify must restore the tree even when the run it wraps explodes.

The tool reverts a fix, runs the test, and expects RED. The window between
revert and restore holds UNFIXED code, so the property that matters is not the
verdict — it is that nothing leaves that window open. These tests drive a real
temp git repo rather than mocking git, because the two failure modes being
guarded are both about what is actually on disk afterwards.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.falsify import ABSENT, _cli, falsify, restore, snapshot, verdict

PASS = [sys.executable, "-c", "raise SystemExit(0)"]
FAIL = [sys.executable, "-c", "raise SystemExit(1)"]
BOOM = [sys.executable, "-c", "raise SystemExit(0)"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo whose `main` holds the UNFIXED file and whose tree holds the fix."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.test")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src.py").write_text("BROKEN = True\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # the working tree carries the fix, UNCOMMITTED — the dangerous case
    (tmp_path / "src.py").write_text("BROKEN = False\n")
    return tmp_path


def test_a_failing_test_without_the_fix_is_a_gate(repo):
    assert falsify(["src.py"], FAIL, base="main", repo=repo) == 0


def test_a_passing_test_without_the_fix_is_a_decoration(repo):
    assert falsify(["src.py"], PASS, base="main", repo=repo) == 1


def test_uncommitted_work_survives_the_revert(repo):
    """THE regression: restoring from a commit would silently eat this."""
    falsify(["src.py"], FAIL, base="main", repo=repo)
    assert (repo / "src.py").read_text() == "BROKEN = False\n"


def test_the_tree_is_restored_even_when_the_command_cannot_run(repo):
    """A non-local exit inside the window must not leave the mutation behind."""
    with pytest.raises(FileNotFoundError):
        falsify(["src.py"], ["definitely-not-a-real-binary-xyz"], base="main", repo=repo)
    assert (repo / "src.py").read_text() == "BROKEN = False\n"


def test_the_file_really_is_reverted_while_the_command_runs(repo):
    """Otherwise the whole exercise is theatre — assert from inside the window."""
    probe = repo / "seen.txt"
    command = [
        sys.executable, "-c",
        f"import pathlib; pathlib.Path({str(probe)!r}).write_text("
        f"pathlib.Path({str(repo / 'src.py')!r}).read_text()); raise SystemExit(1)",
    ]
    assert falsify(["src.py"], command, base="main", repo=repo) == 0
    assert probe.read_text() == "BROKEN = True\n", "the command did not see the unfixed file"


def test_a_file_the_branch_ADDS_is_reverted_by_deleting_it(repo):
    """No base version means the unfixed state is 'absent' — not an error."""
    added = repo / "new.py"
    added.write_text("NEW = 1\n")
    probe = repo / "existed.txt"
    command = [
        sys.executable, "-c",
        f"import pathlib; pathlib.Path({str(probe)!r}).write_text("
        f"str(pathlib.Path({str(added)!r}).exists())); raise SystemExit(1)",
    ]
    assert falsify(["new.py"], command, base="main", repo=repo) == 0
    assert probe.read_text() == "False"
    assert added.read_text() == "NEW = 1\n", "the added file must come back"


def test_reverting_a_file_that_matches_base_is_refused(repo):
    """It would mutate nothing and report 'gate' off an unrelated failure."""
    (repo / "src.py").write_text("BROKEN = True\n")  # identical to base
    assert falsify(["src.py"], FAIL, base="main", repo=repo) == 2


def test_a_missing_path_is_a_usage_error_not_a_verdict(repo):
    assert falsify(["nope.py"], FAIL, base="main", repo=repo) == 2


def test_verdict_is_inverted_because_red_is_success():
    assert verdict(1) == "gate"
    assert verdict(2) == "gate"
    assert verdict(0) == "decoration"


def test_snapshot_records_absence_rather_than_raising(tmp_path):
    missing = tmp_path / "gone.py"
    assert snapshot([missing])[missing] is ABSENT


def test_restore_reports_paths_it_could_not_put_back(tmp_path):
    """Silence here would be the worst possible outcome — the mutation ships."""
    blocked = tmp_path / "dir-in-the-way"
    blocked.mkdir()
    bad = restore({blocked: b"content"})
    assert str(blocked) in "\n".join(bad)


def test_cli_requires_a_command_after_the_separator(repo, capsys):
    assert _cli(["src.py", "--repo", str(repo)]) == 2
    assert "no test command" in capsys.readouterr().err


def test_cli_passes_the_command_through(repo):
    rc = _cli(["src.py", "--base", "main", "--repo", str(repo), "--", *FAIL])
    assert rc == 0


# --- The third verdict -----------------------------------------------------
#
# Found by dogfooding: pointing this at canopy#546's fix reverted the whole
# file, which removed a NEW function, which killed every test importing it at
# collection. Non-zero exit, so the first version called it a GATE — a false
# pass on the commonest shape there is.


def test_a_collection_error_is_inconclusive_not_a_gate():
    """Non-zero, but the assertions never ran, so it is not evidence."""
    pytest_style = (
        "ImportError while importing test module '/x/tests/test_a.py'.\n"
        "E   ImportError: cannot import name 'restore_plan'\n"
        "!!!! Interrupted: 1 error during collection !!!!\n"
    )
    assert verdict(2, pytest_style) == "inconclusive"


def test_a_real_assertion_failure_is_still_a_gate():
    """The discriminator must not swallow the red it exists to confirm."""
    real = (
        "E       assert plan['restore_after'] is True\n"
        "FAILED tests/ddd/test_x.py::test_the_bug_shape\n"
        "1 failed, 17 passed in 0.11s\n"
    )
    assert verdict(1, real) == "gate"


def test_a_pass_is_a_decoration_whatever_the_output_says():
    assert verdict(0, "ImportError while importing test module") == "decoration"


def test_no_output_falls_back_to_the_exit_code():
    assert verdict(1) == "gate"
    assert verdict(0) == "decoration"


def test_falsify_reports_exit_3_for_an_inconclusive_run(repo):
    """End to end: a command that dies before asserting must not read as red."""
    command = [
        sys.executable, "-c",
        "print('Interrupted: 1 error during collection'); raise SystemExit(2)",
    ]
    assert falsify(["src.py"], command, base="main", repo=repo) == 3
    assert (repo / "src.py").read_text() == "BROKEN = False\n", "still restored"
