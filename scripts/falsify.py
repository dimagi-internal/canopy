"""Prove a regression test is a GATE and not a decoration.

A green suite proves the code works *now*. It does not prove the new test would
notice if the fix were removed — and those two claims look identical in the
output. ``agent-core/shipping.md`` has demanded this check in prose for months:
revert the fix, watch the new test go red, put the fix back. It is five manual
steps, it is run on every regression-test PR in the fleet, and it has two failure
modes that are worse than not running it at all:

1. **Restoring from a commit destroys uncommitted work.** The prose warns against
   ``git checkout <branch> -- <file>`` for exactly this reason: it reads the file
   out of a *commit*, so an edit not yet committed is silently replaced, ``git
   status`` goes clean, and nothing errors. It cost a retyped skill edit on
   2026-09-07, in the same turn that wrote the warning.
2. **A mutation left behind ships.** The revert is the whole point, so the window
   between "revert" and "restore" contains the unfixed code. Any non-local exit in
   that window — an exception, a timeout, Ctrl-C — leaves the tree holding
   ``origin/main``'s version of the file under a branch that claims to fix it.

Both are silent, and both are the shape CLAUDE.md names as belonging in code
rather than prose: a procedure whose failure mode is a wrong answer rather than an
error. So this snapshots file CONTENT (not a git ref), restores in a ``finally``,
and verifies the restore by hash before it reports anything.

Verdict is inverted on purpose — the command is EXPECTED to fail:

    exit 0   the test failed without the fix   -> it is a real gate
    exit 1   the test PASSED without the fix   -> it is a decoration
    exit 2   usage / the tree could not be restored
    exit 3   the test never RAN without the fix -> inconclusive, tighten the revert

    python -m scripts.falsify scripts/ddd/recipe_preflight.py -- \\
        uv run pytest tests/ddd/test_recipe_preflight_restore.py -q

Read the failure MESSAGE, not just the count: it has to fail on the assertion you
wrote, for the defect you are fixing. A test that fails because the module no
longer imports has told you nothing, so the reverted-run output is printed in full.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ABSENT = object()  # the path does not exist at the base ref — a file this branch ADDS


def _digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()[:12]


def base_content(base: str, path: str, repo: Path) -> bytes | object:
    """The file as of ``base``, or :data:`ABSENT` when the branch adds it.

    A path the branch creates has no base version, and "revert" for it means
    "delete" — which is the correct unfixed state and must not be an error.
    """
    result = subprocess.run(
        ["git", "show", f"{base}:{path}"],
        cwd=str(repo), capture_output=True,
    )
    return result.stdout if result.returncode == 0 else ABSENT


def snapshot(paths: list[Path]) -> dict[Path, bytes | object]:
    """Current CONTENT of each path, uncommitted edits included.

    Content, never a ref: restoring from a commit is the documented way to
    destroy work that was never committed.
    """
    return {p: (p.read_bytes() if p.exists() else ABSENT) for p in paths}


def _write(path: Path, blob: bytes | object) -> None:
    if blob is ABSENT:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)  # type: ignore[arg-type]


def restore(saved: dict[Path, bytes | object]) -> list[str]:
    """Put every snapshotted path back. Returns the paths that did NOT match."""
    bad: list[str] = []
    for path, blob in saved.items():
        try:
            _write(path, blob)
        except OSError as exc:  # noqa: PERF203 — one bad path must not skip the rest
            bad.append(f"{path}: {exc}")
            continue
        now = path.read_bytes() if path.exists() else ABSENT
        if now is ABSENT and blob is ABSENT:
            continue
        if now is ABSENT or blob is ABSENT or _digest(now) != _digest(blob):  # type: ignore[arg-type]
            bad.append(str(path))
    return bad


# A reverted run that never got as far as running the assertions has told you
# nothing — and this is the COMMON case, not an edge one: a fix that adds a new
# function makes the whole file's revert an ImportError, so every test importing
# that symbol dies at collection. It exits non-zero, which looks exactly like the
# red you were hoping for. Found by pointing this tool at the first fix it was
# written for (canopy#546), whose own author had already hit it by hand and
# switched to mutating the rule instead.
_COLLECTION_FAILURE = (
    "error during collection",
    "errors during collection",
    "ImportError while importing test module",
    "ModuleNotFoundError",
    "collection failure",
    "no tests ran",
)


def verdict(returncode: int, output: str = "") -> str:
    """Inverted: the command is expected to FAIL against the unfixed code.

    ``inconclusive`` is the third answer and the useful one — a red that came
    from the test never running is not evidence about the test.
    """
    if returncode == 0:
        return "decoration"
    low = output.lower()
    if any(marker.lower() in low for marker in _COLLECTION_FAILURE):
        return "inconclusive"
    return "gate"


def _run_teed(command: list[str], repo: Path) -> tuple[int, str]:
    """Run the command, streaming its output live AND keeping a copy.

    Both halves are load-bearing: the human has to read the failure message to
    confirm it is the right failure, and :func:`verdict` has to read it to tell
    a real red from a collection error.
    """
    proc = subprocess.Popen(
        command, cwd=str(repo),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    chunks: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        chunks.append(line)
    sys.stdout.flush()
    return proc.wait(), "".join(chunks)


def falsify(paths: list[str], command: list[str], *, base: str, repo: Path) -> int:
    resolved = [(repo / p).resolve() for p in paths]
    missing = [p for p, r in zip(paths, resolved) if not r.exists()]
    if missing:
        print(f"falsify: no such path(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    saved = snapshot(resolved)
    reverted = {r: base_content(base, p, repo) for p, r in zip(paths, resolved)}

    unchanged = [
        str(r) for r, blob in reverted.items()
        if (saved[r] is ABSENT and blob is ABSENT)
        or (saved[r] is not ABSENT and blob is not ABSENT
            and _digest(saved[r]) == _digest(blob))  # type: ignore[arg-type]
    ]
    if unchanged:
        # Reverting a file that already matches base mutates nothing, so the run
        # proves nothing — and would report "gate" off an unrelated failure.
        print(
            f"falsify: identical to {base} — nothing to revert: {', '.join(unchanged)}\n"
            "         Point it at the file your fix actually changed.",
            file=sys.stderr,
        )
        return 2

    returncode = 1
    output = ""
    try:
        for path, blob in reverted.items():
            _write(path, blob)
        print(f"falsify: reverted {len(reverted)} path(s) to {base}; running the test\n", flush=True)
        returncode, output = _run_teed(command, repo)
    finally:
        # The window above holds UNFIXED code. Nothing may leave it open.
        bad = restore(saved)
        if bad:
            print(
                "falsify: COULD NOT RESTORE — the working tree still holds "
                f"{base}'s version of: {', '.join(bad)}\n"
                "         Fix this before anything else; your branch does not "
                "contain the change it claims to.",
                file=sys.stderr,
            )
            return 2
        print("\nfalsify: working tree restored and verified by hash", flush=True)

    call = verdict(returncode, output)
    if call == "gate":
        print(f"falsify: GATE — the test failed without the fix (exit {returncode}).")
        print("         Read the failure above: it must be YOUR assertion.")
        return 0
    if call == "inconclusive":
        print(
            f"falsify: INCONCLUSIVE — the test never ran (exit {returncode}); it died at\n"
            "         import/collection, which is what reverting a WHOLE file does when the\n"
            "         fix adds a new symbol. A red like this is not evidence about the test.\n"
            "         Revert the RULE instead of the file: put the old expression back by\n"
            "         hand, or point --base at a ref where the symbol already exists.",
            file=sys.stderr,
        )
        return 3
    print(
        "falsify: DECORATION — the test PASSED without the fix.\n"
        "         It does not guard the defect you are fixing. Tighten it.",
        file=sys.stderr,
    )
    return 1


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.falsify",
        description="Revert a fix, run its test, expect RED, restore. Exit 0 = real gate.",
    )
    parser.add_argument("paths", nargs="+", help="the source file(s) your fix changed")
    parser.add_argument("--base", default="origin/main", help="ref to revert to (default origin/main)")
    parser.add_argument("--repo", default=".", help="repo root (default cwd)")
    ns, rest = parser.parse_known_args(argv)

    command = rest[1:] if rest and rest[0] == "--" else rest
    if not command:
        print("falsify: no test command — put it after `--`", file=sys.stderr)
        return 2
    return falsify(ns.paths, command, base=ns.base, repo=Path(ns.repo).resolve())


if __name__ == "__main__":
    raise SystemExit(_cli())
