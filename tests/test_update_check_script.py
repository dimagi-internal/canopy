"""`canopy-update-check.sh` — the gate Step 1 of `/canopy:update` stops on.

It used to compare VERSION *numbers* only, which made it blind to the one case
that matters most: two different commits on `main` carrying the SAME version.
That is not hypothetical — it happened on 2026-07-28. PR #423 merged v0.2.369 to
main; PR #429, whose CI had checked against main *before* #423 landed, merged a
different v0.2.369 forty seconds later. The plugin cache is keyed by version, so
the installed dir already existed, held #423's code, and the check reported
`UP_TO_DATE` — telling the agent to STOP while the merged fix was nowhere on the
machine.

So the check is SHA-driven now, like the fleet session-start updater already was.
A SHA advances on every merge whether or not the version label does.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[1]
          / "plugins" / "canopy" / "scripts" / "canopy-update-check.sh")


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True)


def _origin_with_version(tmp_path, version):
    """A bare origin plus a working clone of it, holding VERSION."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main", ".")
    work.mkdir()
    _git(work, "init", "--initial-branch=main", ".")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "VERSION").write_text(version + "\n")
    _git(work, "add", "VERSION")
    _git(work, "commit", "-m", "v")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return origin, work


def _clone(tmp_path, origin):
    clone = tmp_path / "marketplace"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


def _head_sha(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _registry(home, version, sha):
    path = home / ".claude" / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    entry = {"version": version, "installPath": "/tmp/x"}
    if sha is not None:
        entry["gitCommitSha"] = sha
    (path / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"canopy@canopy": [entry]}}, indent=2))


def _run(home, marketplace):
    env = {**os.environ, "HOME": str(home), "CANOPY_MARKETPLACE": str(marketplace)}
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    return proc.stdout.strip()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_up_to_date_when_the_installed_sha_is_main(tmp_path, home):
    origin, work = _origin_with_version(tmp_path, "0.2.369")
    clone = _clone(tmp_path, origin)
    _registry(home, "0.2.369", _head_sha(work))

    assert _run(home, clone).startswith("UP_TO_DATE")


def test_a_REUSED_version_number_is_still_an_upgrade(tmp_path, home):
    """The regression this file exists for. Same version label, different commit:
    version-only comparison said UP_TO_DATE and stranded the merged code."""
    origin, work = _origin_with_version(tmp_path, "0.2.369")
    clone = _clone(tmp_path, origin)
    stale_sha = _head_sha(work)

    # A second commit that does NOT bump VERSION — exactly the collision shape.
    (work / "src.py").write_text("new code\n")
    _git(work, "add", "src.py")
    _git(work, "commit", "-m", "second 0.2.369")
    _git(work, "push", "-q", "origin", "main")

    _registry(home, "0.2.369", stale_sha)

    out = _run(home, clone)
    assert out.startswith("UPGRADE_AVAILABLE"), out


def test_a_normal_version_bump_is_an_upgrade(tmp_path, home):
    origin, work = _origin_with_version(tmp_path, "0.2.369")
    clone = _clone(tmp_path, origin)
    stale_sha = _head_sha(work)
    (work / "VERSION").write_text("0.2.370\n")
    _git(work, "commit", "-am", "bump")
    _git(work, "push", "-q", "origin", "main")
    _registry(home, "0.2.369", stale_sha)

    out = _run(home, clone)
    assert out.startswith("UPGRADE_AVAILABLE")
    assert "0.2.370" in out, "the remote version must still be reported for Step 2"


def test_falls_back_to_version_compare_when_no_sha_is_recorded(tmp_path, home):
    """An old registry entry predates SHA tracking. Comparing versions is worse
    than comparing SHAs, but it is much better than refusing to check at all."""
    origin, work = _origin_with_version(tmp_path, "0.2.369")
    clone = _clone(tmp_path, origin)
    _registry(home, "0.2.369", None)
    assert _run(home, clone).startswith("UP_TO_DATE")

    _registry(home, "0.2.368", None)
    assert _run(home, clone).startswith("UPGRADE_AVAILABLE")


def test_errors_stay_errors(tmp_path, home):
    _registry(home, "0.2.369", "abc")
    assert _run(home, tmp_path / "nope").startswith("ERROR")


def test_reads_main_even_when_the_clone_is_parked_on_a_branch(tmp_path, home):
    """The clone gets parked on feature branches (that is a separate bug, guarded
    elsewhere). The check must still compare against origin/main, never HEAD."""
    origin, work = _origin_with_version(tmp_path, "0.2.369")
    clone = _clone(tmp_path, origin)
    _git(clone, "checkout", "-q", "-b", "parked")
    (clone / "junk.txt").write_text("local junk\n")
    _git(clone, "add", "junk.txt")
    _git(clone, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "junk")

    _registry(home, "0.2.369", _head_sha(work))
    assert _run(home, clone).startswith("UP_TO_DATE")
