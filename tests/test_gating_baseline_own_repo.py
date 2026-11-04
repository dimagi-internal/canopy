"""The own-repo rail, run through the REAL session hook (`gating_guard.py --session`).

A turn whose asker lacks `agent_edit` may not change THE AGENT'S OWN REPO (push, PR/release/
repo-settings writes, mutating `gh api repos/<it>/...`); issues, other repos and a PR from a fork
stay open; a session with no canopy turn (a person at the keyboard) is untouched; a turn with an
unreadable envelope fails closed; and the model cannot forge the envelope or the turn id.

Run: uv run pytest tests/test_gating_baseline_own_repo.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"
GUARD = PLUGIN / "agent-core" / "gating_guard.py"
TURN = "11111111-2222-3333-4444-555555555555"
OWN = "https://github.com/dimagi-internal/trellis.git"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    (home / ".canopy" / "caller").mkdir(parents=True)
    agent = tmp_path / "trellis"
    (agent / "config").mkdir(parents=True)
    (agent / "config" / "gating.json").write_text(json.dumps({"slug": "trellis", "channels": [], "deny": []}))
    _git("init", "-q", cwd=agent)
    _git("remote", "add", "origin", OWN, cwd=agent)
    _git("remote", "add", "fork", "git@github.com:someone/trellis.git", cwd=agent)
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", cwd=other)
    _git("remote", "add", "origin", "https://github.com/dimagi-internal/connect-hub.git", cwd=other)
    plain = tmp_path / "plain"
    plain.mkdir()
    return {"home": home, "agent": agent, "other": other, "plain": plain}


def envelope(world, perms=None, relationship="member", **extra):
    doc = {"turn_id": TURN, "relationship": relationship, "profile": "full", **extra}
    if perms is not None:
        doc["permissions"] = perms
    (world["home"] / ".canopy" / "caller" / f"{TURN}.json").write_text(json.dumps(doc))


def hook(world, command, *, cwd=None, turn=TURN, agent="trellis", tool="Bash", tool_input=None, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CANOPY_", "CLAUDE_"))}
    env["HOME"] = str(world["home"])
    cwd = str(cwd or world["agent"])
    env["CLAUDE_PROJECT_DIR"] = cwd
    if agent:
        env["CANOPY_AGENT_SLUG"] = agent
    if turn:
        env["CANOPY_TURN_ID"] = turn
    env.update(extra_env or {})
    payload = {"tool_name": tool, "tool_input": tool_input or {"command": command}, "cwd": cwd}
    r = subprocess.run([sys.executable, str(GUARD), "--session"], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stderr


READ_WRITE = ["read", "write"]
EDITOR = ["agent_edit", "read", "write"]

OWN_WRITES = [
    "git push",
    "git push origin main",
    "git push -u origin HEAD:refs/heads/x",
    "git push --force-with-lease origin feat",
    f"git push {OWN} HEAD:main",
    "git push git@github.com:dimagi-internal/trellis.git main",
    "git -C . push origin main",
    "cd . && git commit -qm x && git push origin x",
    "FOO=1 git push",
    "bash -c 'git push origin main'",
    "echo ok; git push 2>&1 | tail -3",
    "gh pr create --title t --body b",
    "gh pr create -R dimagi-internal/trellis --title t",
    "gh pr merge 12 --auto",
    "gh pr merge https://github.com/dimagi-internal/trellis/pull/12",
    "gh pr edit 3 --add-label x",
    "gh pr close 3",
    "gh pr review 3 --approve",
    "gh pr ready 3",
    "gh api -X PUT repos/dimagi-internal/trellis/pulls/3/merge",
    "gh api --method POST /repos/dimagi-internal/trellis/git/refs -f ref=refs/heads/x -f sha=abc",
    "gh api repos/dimagi-internal/trellis/contents/persona.md -X PUT -f message=m -f content=Yw==",
    "gh api repos/dimagi-internal/trellis/pulls -f title=t -f head=x -f base=main",
    "gh api repos/dimagi-internal/trellis/branches/main/protection -X DELETE",
    "gh api repos/{owner}/{repo}/merges -f base=main -f head=x",
    "gh repo edit --visibility private",
    "gh repo delete dimagi-internal/trellis --yes",
    "gh repo rename -R dimagi-internal/trellis new",
    "gh release create v1 --notes n",
]


@pytest.mark.parametrize("cmd", OWN_WRITES)
def test_a_read_write_asker_cannot_write_the_agents_own_repo(world, cmd):
    envelope(world, READ_WRITE, relationship="member")
    code, err = hook(world, cmd)
    assert code == 2, cmd
    assert "editor role on trellis" in err
    assert "issue" in err and "FORK" in err


@pytest.mark.parametrize("cmd", OWN_WRITES)
@pytest.mark.parametrize("perms,rel", [(EDITOR, "member"),
                                       (["admin", "agent_edit", "read", "write"], "admin"),
                                       (["admin", "agent_edit", "own", "read", "write"], "owner")])
def test_editor_admin_owner_may(world, cmd, perms, rel):
    envelope(world, perms, relationship=rel)
    assert hook(world, cmd)[0] == 0, cmd


def test_push_from_another_repos_checkout_is_allowed(world):
    envelope(world, READ_WRITE)
    assert hook(world, "git push", cwd=world["other"])[0] == 0


@pytest.mark.parametrize("cmd", [
    "git push fork my-branch",                                            # a fork remote of the agent's clone
    "git push git@github.com:someone/trellis.git x",
    "git -C {other} push origin main",                                 # another repo, from the agent's cwd
    "cd {other} && git push",
    "git status",
    "git commit -qam 'git push later'",
    "git log --oneline | head",
    "echo 'git push origin main'",
    "gh pr create -R dimagi-internal/connect-hub --title t",              # another repo
    "gh pr create -R dimagi-internal/trellis --head someone:fix --title t",   # a PR from a fork
    "gh pr merge 5 -R dimagi-internal/connect-hub",
    "gh pr view 3", "gh pr list", "gh pr comment 3 -b hi", "gh pr checks 3", "gh pr diff 3",
    "gh issue create -R dimagi-internal/trellis --title t --body b",
    "gh issue comment 3 -R dimagi-internal/trellis -b hi",
    "gh issue close 3 -R dimagi-internal/trellis",
    "gh api repos/dimagi-internal/trellis/issues -f title=t",
    "gh api repos/dimagi-internal/trellis/issues/3/comments -f body=b",
    "gh api repos/dimagi-internal/trellis/pulls/3/comments -f body=b",
    "gh api repos/dimagi-internal/trellis/pulls",                           # a read
    "gh api repos/dimagi-internal/trellis/contents/persona.md",
    "gh api -X GET repos/dimagi-internal/trellis/pulls -f state=open",      # a GET with query fields
    "gh api -X POST repos/dimagi-internal/connect-hub/git/refs -f ref=x",   # another repo
    "gh api user",
    "gh repo view dimagi-internal/trellis", "gh repo fork dimagi-internal/trellis --clone=false",
    "gh repo clone dimagi-internal/trellis /tmp/x",
    "gh release list", "gh release view v1",
    "git fetch origin",
    "git -C \"$WT\" push origin main",                                     # unresolvable dir: not seen (documented)
    "cat <<'EOF'\ngit push origin main\nEOF",
    "gh pr create -R dimagi-internal/connect-hub --body \"$(cat <<'EOF'\nthen git push origin main and gh pr merge\nEOF\n)\"",
])
def test_everything_else_stays_open_for_a_read_write_asker(world, cmd):
    envelope(world, READ_WRITE)
    cmd = cmd.replace("{other}", str(world["other"]))
    assert hook(world, cmd)[0] == 0, cmd


def test_a_session_with_no_turn_is_untouched(world):
    # A developer working in the agent's repo by hand: an agent session with no canopy turn.
    assert hook(world, "git push origin main", turn="")[0] == 0
    assert hook(world, "gh pr merge 3", turn="", agent=None)[0] == 0
    # No agent at all, a plain directory.
    assert hook(world, "git push", cwd=world["plain"], turn="", agent=None)[0] == 0


def test_no_agent_session_is_untouched_even_with_a_turn_id_in_env(world):
    assert hook(world, "git push origin main", cwd=world["plain"], agent=None)[0] == 0


@pytest.mark.parametrize("break_it", ["missing", "garbage", "wrong_turn_id", "symlink_elsewhere"])
def test_a_turn_with_no_trustworthy_envelope_fails_closed(world, break_it):
    path = world["home"] / ".canopy" / "caller" / f"{TURN}.json"
    if break_it == "garbage":
        path.write_text("{not json")
    elif break_it == "wrong_turn_id":
        path.write_text(json.dumps({"turn_id": "99999999-0000-0000-0000-000000000000",
                                    "permissions": EDITOR, "relationship": "owner"}))
    elif break_it == "symlink_elsewhere":
        forged = world["plain"] / "forged.json"
        forged.write_text(json.dumps({"turn_id": TURN, "permissions": EDITOR, "relationship": "owner"}))
        path.symlink_to(forged)
    code, err = hook(world, "git push origin main")
    assert code == 2 and "fail closed" in err
    # Reads and other-repo writes are still free in that state.
    assert hook(world, "gh pr create -R dimagi-internal/connect-hub --title t")[0] == 0
    assert hook(world, "git fetch origin")[0] == 0


def test_a_turn_id_that_is_not_a_uuid_is_no_turn(world):
    # Garbage in the env is not a turn; the rail does not fail closed on it.
    assert hook(world, "git push origin main", turn="../../etc/passwd")[0] == 0


def test_an_older_canopy_web_envelope_without_permissions(world):
    envelope(world, None, relationship="owner")
    assert hook(world, "git push origin main")[0] == 0
    envelope(world, None, relationship="admin")
    assert hook(world, "git push origin main")[0] == 0
    envelope(world, None, relationship="system", who={"kind": "system"})
    assert hook(world, "git push origin main")[0] == 0
    envelope(world, None, relationship="member")
    assert hook(world, "git push origin main")[0] == 2
    envelope(world, None, relationship="contact", profile="confined")
    assert hook(world, "git push origin main")[0] == 2


def test_a_confined_asker_is_refused_too(world):
    envelope(world, ["read", "use:ask"], relationship="contact", profile="confined")
    assert hook(world, "git push origin main")[0] == 2


def test_the_laptop_by_task_record_names_the_turn(world):
    envelope(world, READ_WRITE)
    by_task = world["home"] / ".canopy" / "caller" / "by-task"
    by_task.mkdir()
    (by_task / "fix-thing.json").write_text(json.dumps({"turn_id": TURN, "task": "fix-thing"}))
    wt = world["agent"].parent / "emdash-fix-thing"
    wt.mkdir()
    _git("init", "-q", cwd=wt)
    _git("remote", "add", "origin", OWN, cwd=wt)
    (wt / "config").mkdir()
    (wt / "config" / "gating.json").write_text(json.dumps({"slug": "trellis", "channels": []}))
    assert hook(world, "git push", cwd=wt, turn="")[0] == 2
    # The owner has since taken over the keyboard: presence says so, and the rail stands down.
    presence = world["home"] / ".canopy" / "caller" / "presence"
    presence.mkdir()
    (presence / "sess1.json").write_text(json.dumps({"origin": "keyboard"}))
    env = {"CLAUDE_CODE_SESSION_ID": "sess1"}
    assert hook(world, "git push", cwd=wt, turn="", extra_env=env)[0] == 0


def test_a_local_turn_names_the_turn(world):
    envelope(world, READ_WRITE)
    local = world["home"] / ".canopy" / "local"
    local.mkdir()
    (local / "sess2.json").write_text(json.dumps({"turn_id": TURN}))
    assert hook(world, "git push origin main", turn="", extra_env={"CLAUDE_CODE_SESSION_ID": "sess2"})[0] == 2
    (local / "sess2.json").write_text(json.dumps({"turn_id": TURN, "closed_at": 1}))
    assert hook(world, "git push origin main", turn="", extra_env={"CLAUDE_CODE_SESSION_ID": "sess2"})[0] == 0


# ── the model cannot forge the envelope or the turn id ───────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "CANOPY_TURN_ID=11111111-2222-3333-4444-000000000000 git push origin main",
    "export CANOPY_TURN_ID=abc",
    "export CANOPY_CALLER=/tmp/forged.json",
    "env CANOPY_CALLER=/tmp/f.json canopy caller tier",
    "CANOPY_SCOPED_TURN=x ./bin/script",
    "python3 -c \"import os; os.environ['CANOPY_TURN_ID']='x'\"",
    "python3 -c \"import os; os.environ.update({'CANOPY_CALLER': 'x'})\"",
    "python3 -c 'import os; os.putenv(\"CANOPY_TURN_ID\", \"x\")'",
    "echo '{}' > ~/.canopy/caller/11111111-2222-3333-4444-555555555555.json",
    "cat forged.json | tee ~/.canopy/caller/x.json",
    "cp forged.json ~/.canopy/caller/x.json",
    "mv /tmp/f.json $HOME/.canopy/caller/by-task/t.json",
    "python3 -c \"open('/home/u/.canopy/caller/x.json','w').write('{}')\"",
    "sed -i 's/member/owner/' ~/.canopy/caller/x.json",
    "echo '{}' > ~/.canopy/local/s.json",
])
def test_forging_the_envelope_or_turn_id_is_denied(world, cmd):
    envelope(world, EDITOR, relationship="owner")        # even an owner's turn: nobody forges
    assert hook(world, cmd)[0] == 2, cmd


@pytest.mark.parametrize("tool", ["Edit", "Write", "NotebookEdit"])
def test_editing_the_envelope_files_is_denied(world, tool):
    envelope(world, EDITOR, relationship="owner")
    code, err = hook(world, "", tool=tool,
                     tool_input={"file_path": str(world["home"] / ".canopy" / "caller" / f"{TURN}.json")})
    assert code == 2 and "caller" in err


@pytest.mark.parametrize("cmd", [
    "echo $CANOPY_TURN_ID",
    "[ \"$CANOPY_TURN_ID\" = \"\" ] && echo none",
    "[[ $CANOPY_CALLER == /x ]]",
    "cat ~/.canopy/caller/11111111-2222-3333-4444-555555555555.json",
    "canopy caller tier",
    "CANOPY_AGENT=trellis canopy agent whoami",
])
def test_reading_the_envelope_is_free(world, cmd):
    envelope(world, READ_WRITE)
    assert hook(world, cmd)[0] == 0, cmd


def test_a_person_session_may_write_canopy_state(world):
    # No agent: not our rail (a person's own tooling, e.g. `canopy local open`'s helpers).
    assert hook(world, "cp x ~/.canopy/caller/y.json", cwd=world["plain"], turn="", agent=None)[0] == 0


def test_the_deny_message_is_plain_and_actionable(world):
    envelope(world, READ_WRITE)
    code, err = hook(world, "git push origin main")
    assert code == 2
    assert "Only people with the editor role on trellis may change trellis's repo" in err
    assert "dimagi-internal/trellis" in err and "gh issue create" in err and "--head" in err


def test_the_canopy_repo_ci_and_dev_flow_is_untouched(world, tmp_path):
    """canopy's own repo is not an agent (NON_AGENT_SLUGS), and CI has no turn env."""
    repo = tmp_path / "canopy"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "agent.json").write_text("{}")
    _git("init", "-q", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/dimagi-internal/canopy.git", cwd=repo)
    for cmd in ("git push origin main", "gh pr create --title t", "gh pr merge 3 --auto"):
        assert hook(world, cmd, cwd=repo, agent=None, turn="")[0] == 0
        assert hook(world, cmd, cwd=repo, agent="canopy")[0] == 0   # "canopy" is never an agent slug
