"""Tests for scripts/canopy-auth-preflight.sh.

Strategy: build a tmp PATH containing shim executables for `gh`, `op`,
`aws`, and `git`, each of which exits with a configurable code. The shims
are the *only* binaries on PATH (we explicitly do NOT inherit the system
PATH so the harness can't pick up real tools), with one exception: we add
`/bin` and `/usr/bin` so the script's own use of `bash`, `command`, `pwd`,
`echo`, `cat`, etc. continues to resolve.

The script is invoked from a tmp cwd that does NOT contain any of the
"labs" markers, so the AWS branch is skipped by default. We also have a
case that exercises the labs-detection path by cd'ing into a directory
named `connect-labs`.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canopy-auth-preflight.sh"


def _make_shim(dir_: Path, name: str, exit_code: int) -> Path:
    """Create an executable shim at dir_/name that exits with exit_code."""
    shim = dir_ / name
    shim.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _run(env_path: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": env_path, "HOME": str(cwd)},
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_script_exists_and_executable() -> None:
    assert SCRIPT.exists(), f"missing: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"


def test_all_pass(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_shim(bin_dir, "gh", 0)
    _make_shim(bin_dir, "op", 0)
    # No `git` shim and cwd is not labs-y, so AWS branch is skipped.

    cwd = tmp_path / "neutral"
    cwd.mkdir()

    result = _run(f"{bin_dir}:/bin:/usr/bin", cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gh: OK" in result.stdout
    assert "op: OK" in result.stdout
    assert "aws/labs" not in result.stdout
    assert "auth-preflight: PASS" in result.stdout


def test_gh_fail_recovery_hint(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_shim(bin_dir, "gh", 1)
    _make_shim(bin_dir, "op", 0)

    cwd = tmp_path / "neutral"
    cwd.mkdir()

    result = _run(f"{bin_dir}:/bin:/usr/bin", cwd)
    assert result.returncode == 1
    assert "gh: FAIL" in result.stdout
    assert "gh auth login" in result.stdout
    assert "auth-preflight: FAIL" in result.stdout


def test_op_fail_recovery_hint(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_shim(bin_dir, "gh", 0)
    _make_shim(bin_dir, "op", 1)

    cwd = tmp_path / "neutral"
    cwd.mkdir()

    result = _run(f"{bin_dir}:/bin:/usr/bin", cwd)
    assert result.returncode == 1
    assert "op: FAIL" in result.stdout
    assert "op signin --account dimagi.1password.com" in result.stdout


@pytest.mark.skipif(os.environ.get("CI") == "true",
                    reason="probes the REAL gh/op/aws on this machine; CI has an "
                           "unauthenticated gh, which is a different (failing) path")
def test_not_installed_is_not_a_failure(tmp_path: Path) -> None:
    """When tools are missing entirely, the script should report NOT INSTALLED
    and still exit 0 (we treat absence as best-effort, not failure)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # No shims at all — gh and op are absent.

    cwd = tmp_path / "neutral"
    cwd.mkdir()

    result = _run(f"{bin_dir}:/bin:/usr/bin", cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gh: NOT INSTALLED" in result.stdout
    assert "op: NOT INSTALLED" in result.stdout
    assert "auth-preflight: PASS" in result.stdout


def test_labs_path_triggers_aws_check(tmp_path: Path) -> None:
    """cwd containing 'connect-labs' should activate the AWS labs check."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_shim(bin_dir, "gh", 0)
    _make_shim(bin_dir, "op", 0)
    _make_shim(bin_dir, "aws", 1)  # SSO expired

    cwd = tmp_path / "connect-labs-checkout"
    cwd.mkdir()

    result = _run(f"{bin_dir}:/bin:/usr/bin", cwd)
    assert result.returncode == 1
    assert "aws/labs: FAIL" in result.stdout
    assert "canopy aws login --profile labs" in result.stdout


def test_labs_path_aws_pass(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_shim(bin_dir, "gh", 0)
    _make_shim(bin_dir, "op", 0)
    _make_shim(bin_dir, "aws", 0)

    cwd = tmp_path / "ace-web-app"
    cwd.mkdir()

    result = _run(f"{bin_dir}:/bin:/usr/bin", cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "aws/labs: OK" in result.stdout
    assert "auth-preflight: PASS" in result.stdout


def test_no_secret_leak_in_output(tmp_path: Path) -> None:
    """Smoke check: even when shims emit chatter on stdout, the script
    must only forward its own structured pass/fail lines (it redirects
    tool stdout/stderr to /dev/null, so any leaked vault data would
    indicate a regression)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # gh shim that prints a fake "secret" — script should suppress it.
    leaky = bin_dir / "gh"
    leaky.write_text(
        "#!/usr/bin/env bash\necho 'SECRET-VAULT-CONTENTS-DO-NOT-LEAK'\nexit 0\n"
    )
    leaky.chmod(leaky.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _make_shim(bin_dir, "op", 0)

    cwd = tmp_path / "neutral"
    cwd.mkdir()

    result = _run(f"{bin_dir}:/bin:/usr/bin", cwd)
    assert "SECRET-VAULT-CONTENTS-DO-NOT-LEAK" not in result.stdout
    assert "SECRET-VAULT-CONTENTS-DO-NOT-LEAK" not in result.stderr


@pytest.mark.parametrize(
    "marker",
    ["ace-web", "connect-labs", "connect-search"],
)
def test_each_labs_marker_activates_aws(tmp_path: Path, marker: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_shim(bin_dir, "gh", 0)
    _make_shim(bin_dir, "op", 0)
    _make_shim(bin_dir, "aws", 0)

    cwd = tmp_path / f"{marker}-x"
    cwd.mkdir()

    result = _run(f"{bin_dir}:/bin:/usr/bin", cwd)
    assert "aws/labs:" in result.stdout, f"marker {marker} did not activate AWS check"
