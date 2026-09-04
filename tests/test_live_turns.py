"""Regression tests for scripts/live-turns.sh — the duplicate/sibling turn check.

This script has now been wrong SIX times (its header records each one), and every
version returned a confident wrong NUMBER rather than an error. That is what makes
it worth pinning: an agent reads COUNT and stands down, so a phantom duplicate
costs real work and a missed one costs a clobbered shared artifact.

Two failure modes are locked here, and they point in opposite directions:

* FIFTH — self-contamination (over-report). turn.md instructs a turn to READ the
  other sessions' transcripts, which prints the read session's first user prompt,
  command-args and all, into the reader's own transcript. A whole-file grep then
  matches the read session's scope pattern verbatim, so performing the duplicate
  check is what makes a session look like a duplicate.

* SIXTH — mid-resume blindness (under-report). Enumeration is `ps`, and a session
  being resumed has no process for a few seconds while its transcript sits on
  disk correctly scoped. That gap is precisely when a recovery dispatch fires.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "plugins" / "canopy" / "scripts" / "live-turns.sh"

REF_A = "1a066d5b0f2cf45a"
REF_B = "1a043f92a7ff114d"


def _prompt(ref: str, slug: str = "hal") -> str:
    """The literal first-user-message text a /<slug>:turn --thread <ref> produces."""
    return (
        f"<command-message>{slug}:turn</command-message>"
        f"<command-name>/{slug}:turn</command-name>"
        f"<command-args>--thread {ref}</command-args>"
    )


def _write_transcript(projects: Path, sid: str, first_user: str, later_lines=()) -> Path:
    """Write a minimal jsonl transcript: one first user message, then later records."""
    import json

    d = projects / "proj"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    rows = [{"type": "system", "content": "boot"}]
    rows.append({"type": "user", "message": {"role": "user", "content": first_user}})
    for text in later_lines:
        # A tool RESULT is also a user-role record — this is the contamination vector.
        rows.append({"type": "user", "message": {"role": "user", "content": text}})
    # Compact separators, matching what Claude Code actually writes.
    f.write_text(
        "\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n",
        encoding="utf-8",
    )
    return f


def _run(projects: Path, session_ids, *args):
    env = dict(os.environ)
    env["CLAUDE_PROJECTS_DIR"] = str(projects)
    env["CANOPY_LIVE_TURNS_SESSION_IDS"] = " ".join(session_ids)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _count_under(stdout: str, heading_substr: str) -> int:
    """The COUNT= that follows a given heading."""
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if heading_substr in line:
            for later in lines[i:]:
                if later.startswith("COUNT="):
                    return int(later.split("=")[1].split()[0])
    raise AssertionError(f"no COUNT after {heading_substr!r} in:\n{stdout}")


@pytest.fixture
def projects(tmp_path):
    return tmp_path / "projects"


def test_reading_another_transcript_does_not_make_you_its_duplicate(projects):
    """FIFTH FAILURE. B quotes A's whole prompt; B must not count as scoped to A's ref."""
    a, b = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
    _write_transcript(projects, a, _prompt(REF_A))
    # B is scoped to its OWN ref, but has read A's transcript — so A's scope line,
    # verbatim, is sitting in B's transcript as tool output.
    _write_transcript(
        projects,
        b,
        _prompt(REF_B),
        later_lines=[f"--- first user prompt ---\n{_prompt(REF_A)}\n--- end ---"],
    )

    out = _run(projects, [a, b], "--ref", REF_A).stdout

    assert _count_under(out, f"turns scoped to ref {REF_A}") == 1, out
    assert a in out
    # B still surfaces — as a read-this pointer, outside COUNT, never silently dropped.
    assert "nonetheless mention it" in out, out
    mention_block = out.split("nonetheless mention it")[1]
    assert b in mention_block, out


def test_each_session_resolves_to_its_own_ref(projects):
    """The symmetric case: querying B's ref must not pull in A."""
    a, b = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
    _write_transcript(projects, a, _prompt(REF_A), later_lines=[_prompt(REF_B)])
    _write_transcript(projects, b, _prompt(REF_B), later_lines=[_prompt(REF_A)])

    for ref, owner, other in ((REF_A, a, b), (REF_B, b, a)):
        out = _run(projects, [a, b], "--ref", ref).stdout
        assert _count_under(out, f"turns scoped to ref {ref}") == 1, out
        scoped_block = out.split("COUNT=")[0]
        assert owner in scoped_block, out
        assert other not in scoped_block, out


def test_slug_count_matches_any_entry_point_not_just_turn(projects):
    """FOURTH FAILURE stays fixed: a non-`:turn` dispatch is still a sibling."""
    a, b = "aaaaaaaa-0000-0000-0000-000000000001", "cccccccc-0000-0000-0000-000000000003"
    _write_transcript(projects, a, _prompt(REF_A))
    _write_transcript(
        projects,
        b,
        "<command-message>hal:chief-of-staff</command-message>"
        "<command-name>/hal:chief-of-staff</command-name><command-args></command-args>",
    )

    out = _run(projects, [a, b], "--slug", "hal").stdout
    assert _count_under(out, "live hal sessions") == 2, out


def test_slug_count_ignores_a_quoted_sibling_command_name(projects):
    """A non-hal session that merely READ a hal transcript is not a hal session."""
    a, b = "aaaaaaaa-0000-0000-0000-000000000001", "dddddddd-0000-0000-0000-000000000004"
    _write_transcript(projects, a, _prompt(REF_A))
    _write_transcript(
        projects,
        b,
        "<command-message>eva:turn</command-message>"
        "<command-name>/eva:turn</command-name><command-args></command-args>",
        later_lines=[_prompt(REF_A)],  # read hal's transcript
    )

    out = _run(projects, [a, b], "--slug", "hal").stdout
    assert _count_under(out, "live hal sessions") == 1, out


def test_session_with_no_live_process_is_reported_outside_count(projects):
    """SIXTH FAILURE. A mid-resume session has a fresh transcript and no process.

    It must be surfaced (it is the likeliest owner of your item) but must NOT
    enter COUNT — over-reporting is wrong version 3.
    """
    a, resuming = "aaaaaaaa-0000-0000-0000-000000000001", "eeeeeeee-0000-0000-0000-000000000005"
    _write_transcript(projects, a, _prompt(REF_A))
    _write_transcript(projects, resuming, _prompt(REF_A))

    # `ps` sees only A; the resuming session exists on disk with a recent mtime.
    out = _run(projects, [a], "--ref", REF_A).stdout

    assert _count_under(out, f"turns scoped to ref {REF_A}") == 1, out
    assert "NO live process" in out, out
    assert resuming in out.split("NO live process")[1], out


def test_live_sessions_do_not_leak_into_the_no_process_block(projects):
    """The dedup is space-delimited while `ps` output is newline-separated.

    Getting that wrong put EVERY live session into the no-process list, telling
    the reader to chase sessions already sitting in COUNT.
    """
    a, b = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
    _write_transcript(projects, a, _prompt(REF_A))
    _write_transcript(projects, b, _prompt(REF_A))

    out = _run(projects, [a, b], "--ref", REF_A).stdout

    assert _count_under(out, f"turns scoped to ref {REF_A}") == 2, out
    if "NO live process" in out:
        tail = out.split("NO live process")[1]
        assert a not in tail, out
        assert b not in tail, out


def test_cannot_read_projects_dir_is_not_an_all_clear(projects):
    """A check that cannot look must SAY SO — exit 2, never a clean COUNT=0.

    The invariant every wrong version violated: "I could not check" must not
    render as "nothing found".
    """
    missing = projects / "does-not-exist"
    res = _run(missing, ["aaaaaaaa-0000-0000-0000-000000000001"], "--ref", REF_A)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "do not treat as all-clear" in res.stderr, res.stderr


def test_matcher_tolerates_json_whitespace(projects):
    """A pretty-printed record must not silently collapse COUNT to zero.

    If first_user_message stops matching, matches_scope returns false for every
    session and the check reports a confident all-clear. That direction is the
    dangerous one, so it is pinned rather than left to the writer's formatting.
    """
    a = "aaaaaaaa-0000-0000-0000-000000000001"
    d = projects / "proj"
    d.mkdir(parents=True, exist_ok=True)
    # Deliberately spaced, unlike what Claude Code writes today.
    (d / f"{a}.jsonl").write_text(
        '{"type": "system", "content": "boot"}\n'
        '{"type" : "user", "message": {"content": "' + _prompt(REF_A) + '"}}\n',
        encoding="utf-8",
    )

    out = _run(projects, [a], "--ref", REF_A).stdout
    assert _count_under(out, f"turns scoped to ref {REF_A}") == 1, out


def test_help_prints_the_whole_header(projects):
    """--help used a hardcoded line range that went stale as the header grew."""
    res = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0
    # The last paragraph of the header must survive.
    assert "must never render as" in res.stdout, res.stdout
