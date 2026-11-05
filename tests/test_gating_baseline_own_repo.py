"""The own-repo rail, run through the REAL session hook (`gating_guard.py --session`).

A turn whose asker lacks `agent_edit` may not change THE AGENT'S OWN REPO (push, PR/release/
repo-settings/workflow/secret/variable writes, mutating `gh api repos/<it>/...` or the same through
curl/wget, a GraphQL mutation naming it or not saying which repo it targets); issues, reads, other
repos and a PR from a fork stay open; a session with no canopy turn (a person at the keyboard) is
untouched; a turn with an unreadable envelope fails closed; and the model cannot forge the envelope
or the turn id.

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
    env.pop("XDG_CONFIG_HOME", None)                       # GitHub's runners set it; gh's config is
    env.pop("GH_CONFIG_DIR", None)                         # looked for under the fake HOME below
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
GQL_COMMIT = ("gh api graphql -f query='mutation {{ createCommitOnBranch(input:{{branch:{{repositoryNameWithOwner:"
              "\"{repo}\",branchName:\"main\"}},expectedHeadOid:\"a\",message:{{headline:\"h\"}}}}) "
              "{{ clientMutationId }} }}'")
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
    # A PR is an issue: PATCHing its state closes or reopens it.
    "gh api -X PATCH repos/dimagi-internal/trellis/issues/3 -f state=closed",
    "gh api --method PATCH /repos/dimagi-internal/trellis/issues/3/ -F state=open",
    "gh api -XPATCH repos/dimagi-internal/trellis/issues/3 --raw-field state=closed -f title=t",
    "gh api repos/dimagi-internal/trellis/issues/3 -X patch -fstate=closed",
    "gh api -X PATCH repos/{owner}/{repo}/issues/3 -f state=closed",
    "gh api -X PATCH https://api.github.com/repos/dimagi-internal/trellis/issues/3 -f state=closed",
    # GraphQL, when the mutation itself names the repo.
    GQL_COMMIT.format(repo="dimagi-internal/trellis"),
    GQL_COMMIT.format(repo="Dimagi-Internal/Trellis"),
    "gh api graphql -f query='mutation($r: String!) { createCommitOnBranch(input:{branch:{repositoryNameWithOwner:$r,"
    "branchName:\"main\"},expectedHeadOid:\"a\",message:{headline:\"h\"}}) { clientMutationId } }' "
    "-f r=dimagi-internal/trellis",
    "gh api graphql -F owner='{owner}' -F name='{repo}' -f query='mutation { deleteRef(input:{refId:\"x\"}) { clientMutationId } }'",
    "gh api graphql -f owner=dimagi-internal -f repo=trellis --raw-field query='mutation { deleteRef(input:{refId:\"x\"}) { clientMutationId } }'",
    "gh api graphql -f url=https://github.com/dimagi-internal/trellis/pull/3 -f query='mutation M { a: mergePullRequest(input:{pullRequestId:\"x\"}) { clientMutationId } }'",
    "gh api graphql -f query='# a comment\nmutation { deleteRef(input:{refId:\"x\"}) { query { repository(owner:\"dimagi-internal\", name:\"trellis\") { id } } } }'",
    "gh api graphql -f query='mutation { addComment(input:{subjectId:\"x\",body:\"y\"}) { clientMutationId } deleteRef(input:{refId:\"x\"}) { clientMutationId } }' -f r=dimagi-internal/trellis",
    # gh workflow / secret / variable.
    "gh workflow run deploy.yml",
    "gh workflow run deploy.yml -R dimagi-internal/trellis -f env=prod",
    "gh workflow enable deploy.yml",
    "gh workflow disable deploy.yml --repo dimagi-internal/trellis",
    "gh secret set TOKEN --body x",
    "gh secret set TOKEN --env prod --body x",
    "gh secret set TOKEN --repo=dimagi-internal/trellis --body https://github.com/someone/else",
    "gh secret delete TOKEN -R dimagi-internal/trellis",
    "gh variable set NAME --body v",
    "gh variable delete NAME",
    "GH_REPO=dimagi-internal/trellis gh variable set NAME --body v",
    "cd {other} && GH_REPO=dimagi-internal/trellis gh pr merge 3",
    # A token used directly.
    "curl -X POST -H \"Authorization: Bearer $T\" https://api.github.com/repos/dimagi-internal/trellis/pulls -d '{\"title\":\"t\"}'",
    "curl -sSX PUT https://api.github.com/repos/dimagi-internal/trellis/pulls/3/merge -H \"Authorization: token $(gh auth token)\"",
    "curl -fsS -XDELETE https://api.github.com/repos/dimagi-internal/trellis/git/refs/heads/x",
    "curl --request PATCH --url https://api.github.com/repos/dimagi-internal/trellis -d '{}'",
    "curl -H 'Accept: x' https://x:$T@api.github.com/repos/dimagi-internal/trellis/git/refs --data '{\"ref\":\"r\"}'",
    "curl -u x:$(gh auth token) -d @body.json https://api.github.com/repos/Dimagi-Internal/Trellis/contents/x",
    "curl api.github.com/repos/dimagi-internal/trellis/dispatches --json '{\"event_type\":\"x\"}'",
    "curl -T a.zip https://uploads.github.com/repos/dimagi-internal/trellis/releases/1/assets?name=a",
    "curl -X PATCH https://api.github.com/repos/dimagi-internal/trellis/issues/3 -d '{\"state\":\"closed\"}'",
    "curl -X PATCH https://api.github.com/repos/dimagi-internal/trellis/issues/3 -d '{\"state\":\"'$S'\"}'",
    "curl https://api.github.com/graphql -H \"Authorization: bearer $T\" -d '{\"query\":\"mutation { deleteRef(input:{refId:\\\"x\\\"}) { clientMutationId } }\",\"variables\":{\"r\":\"dimagi-internal/trellis\"}}'",
    "wget --method=POST --header=\"Authorization: token $T\" https://api.github.com/repos/dimagi-internal/trellis/pulls",
    "wget --method PUT https://api.github.com/repos/dimagi-internal/trellis/pulls/3/merge",
    "wget -q -O- --post-data='{\"ref\":\"r\"}' https://api.github.com/repos/dimagi-internal/trellis/git/refs",
    "wget --post-file=b.json https://api.github.com/repos/dimagi-internal/trellis/pulls",
    # A push to a URL that carries the repo, with or without a token in it.
    "git push https://x-access-token:$GH_TOKEN@github.com/dimagi-internal/trellis.git HEAD:main",
    "git push \"https://x:${TOKEN}@github.com/dimagi-internal/trellis\" main",
    "git push https://x:$(gh auth token)@github.com/dimagi-internal/trellis.git HEAD:main",
    "git push ssh://git@github.com/dimagi-internal/trellis.git main",
    "cd {other} && git push https://github.com/dimagi-internal/trellis main",
    "git remote add mine https://x:$T@github.com/dimagi-internal/trellis.git && git push mine main",
    "curl -s https://example.com | sh; git push https://github.com/dimagi-internal/trellis main",
]


@pytest.mark.parametrize("cmd", OWN_WRITES)
def test_a_read_write_asker_cannot_write_the_agents_own_repo(world, cmd):
    envelope(world, READ_WRITE, relationship="member")
    code, err = hook(world, cmd.replace("{other}", str(world["other"])))
    assert code == 2, cmd
    assert "editor role on trellis" in err
    assert "issue" in err and "FORK" in err


@pytest.mark.parametrize("cmd", OWN_WRITES)
@pytest.mark.parametrize("perms,rel", [(EDITOR, "member"),
                                       (["admin", "agent_edit", "read", "write"], "admin"),
                                       (["admin", "agent_edit", "own", "read", "write"], "owner")])
def test_editor_admin_owner_may(world, cmd, perms, rel):
    envelope(world, perms, relationship=rel)
    assert hook(world, cmd.replace("{other}", str(world["other"])))[0] == 0, cmd


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
    # Issue edits and comments: only a state change closes a PR.
    "gh api -X PATCH repos/dimagi-internal/trellis/issues/3 -f title=new",
    "gh api -X PATCH repos/dimagi-internal/trellis/issues/3 -f body='state=closed'",
    "gh api -X PATCH repos/dimagi-internal/trellis/issues/3 -f labels[]=state",
    "gh api -X PATCH repos/dimagi-internal/trellis/issues/comments/9 -f body=b",
    "gh api repos/dimagi-internal/trellis/issues/3/labels -f labels[]=x",
    "gh api repos/dimagi-internal/trellis/issues/3/comments -f state=closed",
    "gh api -X PATCH repos/dimagi-internal/connect-hub/issues/3 -f state=closed",  # another repo
    "curl -X PATCH https://api.github.com/repos/dimagi-internal/trellis/issues/3 -d '{\"title\":\"state\"}'",
    "curl -X PATCH https://api.github.com/repos/dimagi-internal/trellis/issues/3 -d '{\"body\":\"set \\\"state\\\": closed\"}'",
    # GraphQL: reads, other repos, comments, and mutations that name no repo.
    "gh api graphql -f query='query { repository(owner:\"dimagi-internal\", name:\"trellis\") { id } }'",
    "gh api graphql -f query='{ viewer { login } }'",
    "gh api graphql -f query='query mutation { viewer { login } }'",
    "gh api graphql -f query='# mutation { deleteRef }\n{ repository(owner:\"dimagi-internal\",name:\"trellis\") { id } }'",
    "gh api graphql -f query='{ search(query:\"mutation { x }\",type:ISSUE,first:1) { issueCount } }' -f r=dimagi-internal/trellis",
    "gh api graphql -f query='{ mutation: viewer { login } }' -f r=dimagi-internal/trellis",
    GQL_COMMIT.format(repo="dimagi-internal/connect-hub"),
    "gh api graphql -f query='mutation { addComment(input:{subjectId:\"I_1\",body:\"dimagi-internal/trellis\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { createIssue(input:{repositoryId:\"R_1\",title:\"t\"}) { clientMutationId } }' -f r=dimagi-internal/trellis",
    "gh api graphql -f query='mutation { createCommitOnBranch(input:{branch:{repositoryNameWithOwner:\"dimagi-internal/connect-hub\"},message:{headline:\"sync https://github.com/dimagi-internal/trellis\"}}) { clientMutationId } }'",
    # A read in a variable set in this same command: the real shape of a merge-queue poll.
    "Q='{repository(owner:\"dimagi-internal\",name:\"trellis\"){pullRequest(number:3){state isInMergeQueue}}}'; gh api graphql -f query=\"$Q\" --jq .data",
    "q='query($n:Int!){repository(owner:\"o\",name:\"r\"){pullRequest(number:$n){state}}}'; for i in $(seq 1 60); do r=$(gh api graphql -F n=3 -f query=\"$q\" --jq .data); done",
    "Q='{ viewer { login } }'; until [ \"$(gh api graphql -f query=\"${Q}\" --jq .data)\" = x ]; do sleep 1; done",
    "for n in 1 2; do s=\"$s $(gh api graphql -f query=\"{repository(owner:\\\"o\\\",name:\\\"r\\\"){pullRequest(number:$n){state}}}\" -q .data)\"; done",
    "[ \"$(gh api graphql -f query='{repository(owner:\"o\",name:\"r\"){pullRequest(number:1){state}}}' --jq .data)\" = true ]",
    "gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){isInMergeQueue}}}' -f o=\"$O\" -f r=\"$R\" -F n=\"$N\" --jq .data",
    "gh api graphql -f query='{ repository(owner:\"o\",name:\"r\") { pullRequest(number:'$N') { state } } }'",
    # A mutation whose input names another repo, in the field that sets it.
    "gh api graphql -f query='mutation($r:String!){ createCommitOnBranch(input:{branch:{repositoryNameWithOwner:$r,branchName:\"m\"},expectedHeadOid:\"a\",message:{headline:\"h\"}}) { clientMutationId } }' -f r=dimagi-internal/connect-hub",
    # gh aliases that do not touch the repo.
    "gh alias set co 'pr view'", "gh alias set v 'pr view $1'", "gh alias list", "gh alias delete co",
    "gh unknown-extension do-something",
    "curl -X POST https://example.com/graphql -d '{\"query\":\"mutation { x }\"}'",        # not GitHub
    "curl https://api.github.com/graphql",
    "gh api \"$URL\" --jq .content",                                       # a read, REST-shaped
    "gh api \"repos/$R/issues/$N/comments\" -f body=hi",
    # gh workflow / secret / variable: reads, other repos, an org's or user's own.
    "gh workflow list", "gh workflow view deploy.yml", "gh secret list", "gh variable list", "gh variable get NAME",
    "gh workflow run deploy.yml -R dimagi-internal/connect-hub",
    "gh secret set TOKEN -R dimagi-internal/connect-hub --body x",
    "gh secret set TOKEN --org dimagi-internal --body x",
    "gh secret set TOKEN --user --body x",
    "gh variable set NAME -o dimagi-internal --body v",
    "gh variable delete NAME --org=dimagi-internal",
    "cd {other} && gh workflow run deploy.yml",
    "GH_REPO=dimagi-internal/connect-hub gh pr create --title t",
    # curl and wget: reads, other repos, issues, other hosts.
    "curl https://api.github.com/repos/dimagi-internal/trellis/pulls",
    "curl -H \"Authorization: Bearer $T\" -sS https://api.github.com/repos/dimagi-internal/trellis",
    "curl -X GET https://api.github.com/repos/dimagi-internal/trellis/pulls -d state=open",
    "curl -G -d state=open https://api.github.com/repos/dimagi-internal/trellis/pulls",
    "curl -I https://api.github.com/repos/dimagi-internal/trellis",
    "curl -X POST https://api.github.com/repos/dimagi-internal/trellis/issues -d '{\"title\":\"t\"}'",
    "curl -X POST https://api.github.com/repos/dimagi-internal/trellis/issues/3/comments -d '{\"body\":\"b\"}'",
    "curl -X POST https://api.github.com/repos/dimagi-internal/connect-hub/pulls -d '{}'",
    "curl -X POST https://example.com/repos/dimagi-internal/trellis/pulls -d '{}'",
    "curl -X POST https://example.com/hook -d 'https://api.github.com/repos/dimagi-internal/trellis/pulls'",
    "curl -X POST https://api.github.com/graphql -d '{\"query\":\"{ viewer { login } }\",\"variables\":{\"r\":\"dimagi-internal/trellis\"}}'",
    "wget https://api.github.com/repos/dimagi-internal/trellis/tarball/main",
    "wget --method=POST https://api.github.com/repos/dimagi-internal/connect-hub/pulls",
    "wget --post-data='{\"title\":\"t\"}' https://api.github.com/repos/dimagi-internal/trellis/issues",
    "git push https://x:$T@github.com/someone/trellis.git main",           # a fork, by URL
    "git remote add mine https://github.com/someone/trellis.git && git push mine main",
    "git clone https://x:$T@github.com/dimagi-internal/trellis.git /tmp/x",
    # Those strings as text: quoted, in a heredoc, in a PR body or an issue.
    "echo 'gh api graphql -f query=\"mutation { deleteRef }\" -f r=dimagi-internal/trellis'",
    "echo \"curl -X POST https://api.github.com/repos/dimagi-internal/trellis/pulls\"",
    "echo 'gh secret set TOKEN; gh workflow run deploy.yml; wget --method=POST https://api.github.com/repos/dimagi-internal/trellis/pulls'",
    "git commit -qm 'curl -X POST https://api.github.com/repos/dimagi-internal/trellis/pulls'",
    "gh issue create -R dimagi-internal/trellis --title 'gh workflow run x' --body 'gh api -X PATCH repos/dimagi-internal/trellis/issues/3 -f state=closed'",
    "gh issue comment 3 -R dimagi-internal/trellis -b 'run git push https://github.com/dimagi-internal/trellis main then gh secret set X'",
    "gh pr comment 3 -b 'curl -X DELETE https://api.github.com/repos/dimagi-internal/trellis'",
    "cat <<'EOF' > notes.md\ngh workflow run deploy.yml\ngh secret delete TOKEN\ncurl -X POST https://api.github.com/repos/dimagi-internal/trellis/pulls\ngit push https://github.com/dimagi-internal/trellis main\ngh api graphql -f query='mutation { deleteRef }' -f r=dimagi-internal/trellis\nEOF",
    "gh pr create -R dimagi-internal/connect-hub --body \"$(cat <<'EOF'\nthen gh workflow run deploy.yml and curl -X POST https://api.github.com/repos/dimagi-internal/trellis/pulls\nEOF\n)\"",
    "gh issue create -R dimagi-internal/trellis --title t --body-file - <<'EOF'\ngh secret set TOKEN --body x\nEOF",
    "VERSION=$(git rev-parse HEAD) && echo \"$VERSION\"",
])
def test_everything_else_stays_open_for_a_read_write_asker(world, cmd):
    envelope(world, READ_WRITE)
    cmd = cmd.replace("{other}", str(world["other"]))
    assert hook(world, cmd)[0] == 0, cmd


@pytest.mark.parametrize("cmd", [
    "git push https://x:$T@github.com/dimagi-internal/trellis.git main",
    "curl -X POST https://api.github.com/repos/dimagi-internal/trellis/pulls -d '{}'",
    "gh workflow run deploy.yml",
    "gh api -X PATCH repos/dimagi-internal/trellis/issues/3 -f state=closed",
    GQL_COMMIT.format(repo="dimagi-internal/trellis"),
])
def test_the_new_writes_leave_a_person_by_hand_untouched_and_fail_closed_with_no_envelope(world, cmd):
    assert hook(world, cmd, turn="")[0] == 0, cmd            # no turn: a person at the keyboard
    code, err = hook(world, cmd)                             # a turn, and no envelope written
    assert code == 2 and "fail closed" in err, cmd


def test_a_push_url_set_on_a_remote_counts(world):
    # `git remote get-url fork` still says the fork; the push would go to the own repo.
    envelope(world, READ_WRITE)
    assert hook(world, "git push fork main")[0] == 0
    _git("remote", "set-url", "--push", "fork", OWN, cwd=world["agent"])
    assert hook(world, "git push fork main")[0] == 2


def test_the_refusal_says_why_closing_through_an_issue_is_refused(world):
    envelope(world, READ_WRITE)
    code, err = hook(world, "gh api -X PATCH repos/dimagi-internal/trellis/issues/3 -f state=closed")
    assert code == 2 and "a PR is an issue" in err


MERGE_BY_NODE = "mutation { mergePullRequest(input:{pullRequestId:\"PR_1\"}) { clientMutationId } }"

# Each fails closed: the hook cannot tell the target is not the agent's own repo.
GRAPHQL_BLIND = [
    # A mutation that names its target by node id, on any repo.
    f"gh api graphql -f query='{MERGE_BY_NODE}'",
    "gh api graphql -f query='mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id}){clientMutationId}}' -f id=$ID",
    "ID=$(gh pr view 3 -R dimagi-internal/connect-hub --json id -q .id); gh api graphql -f query=\"mutation{enablePullRequestAutoMerge(input:{pullRequestId:\\\"$ID\\\"}){clientMutationId}}\"",
    "gh api graphql -f query='mutation { closePullRequest(input:{pullRequestId:\"PR_1\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { dequeuePullRequest(input:{id:\"PR_1\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { deleteRef(input:{refId:\"x\"}) { clientMutationId } }'",
    # Naming a repo anywhere but the field that sets it does not unlock one.
    f"gh api graphql -f query='{MERGE_BY_NODE}' -f r=dimagi-internal/connect-hub",
    f"gh api graphql -f query='{MERGE_BY_NODE}' -f owner=dimagi-internal -f name=connect-hub",
    "gh api graphql -f query='mutation { deleteRef(input:{refId:\"x\"}) { clientMutationId } }' -f note='see dimagi-internal/trellis'",
    "gh api graphql -f query='mutation { createCommitOnBranch(input:{branch:{id:\"BR_1\"},message:{headline:\"repositoryNameWithOwner:\\\"o/r\\\"\"}}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { createCommitOnBranch(input:{branch:{id:\"BR_1\",repositoryNameWithOwner:\"o/r\"}}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { createCommitOnBranch(input:$i) { clientMutationId } }' -F i=@input.json",
    "gh api graphql -f query='mutation($r:String!) { createCommitOnBranch(input:{branch:{repositoryNameWithOwner:$r}}) { clientMutationId } }' -f r=\"$R\"",
    "gh api graphql -f query='mutation { createCommitOnBranch(input:{branch:{repositoryNameWithOwner:\"o/r\"}}) { clientMutationId } mergePullRequest(input:{pullRequestId:\"PR_1\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { addComment(input:{subjectId:\"x\",body:\"y\"}) { clientMutationId } mergePullRequest(input:{pullRequestId:\"PR_1\"}) { clientMutationId } }'",
    # Ways to write a mutation that do not look like one.
    f"gh api graphql -f query=',{MERGE_BY_NODE}'",
    "gh api graphql -f query='mutation@d { mergePullRequest(input:{pullRequestId:\"PR_1\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation{...addComment} fragment addComment on Mutation { mergePullRequest(input:{pullRequestId:\"PR_1\"}) { clientMutationId } }'",
    f"gh api graphql -f query='query A {{ viewer {{ login }} }} {MERGE_BY_NODE.replace('mutation', 'mutation B')}' -f operationName=B",
    f"gh api graphql -f query='{{ viewer {{ login }} }}' -f query='{MERGE_BY_NODE}'",      # gh sends the last
    f"gh api /graphql -f query='{MERGE_BY_NODE}'",
    f"gh api GRAPHQL -f query='{MERGE_BY_NODE}'",
    f"gh api 'graphql?x=1' -f query='{MERGE_BY_NODE}'",
    f"gh api https://api.github.com/graphql --raw-field=query='{MERGE_BY_NODE}'",
    f"gh api -X POST graphql -fquery='{MERGE_BY_NODE}'",
    f"r=\"$(gh api graphql -f query='{MERGE_BY_NODE}' --jq .data)\"",      # parens inside "$(...)"
    f"if true; then gh api graphql -f query='{MERGE_BY_NODE}'; fi",
    f"for i in 1 2; do gh api graphql -f query='{MERGE_BY_NODE}'; done",
    f"bash -c \"gh api graphql -f query='mutation {{ deleteRef(input:{{refId:1}}) {{ clientMutationId }} }}'\"",
    # A query from a file or stdin.
    "gh api graphql -F query=@merge.graphql",
    "gh api graphql -f query=@merge.graphql",
    "gh api graphql --field query=@merge.graphql",
    "gh api graphql -F query=@-",
    "gh api graphql -F query=@<(cat merge.graphql)",
    "gh api graphql --input body.json",
    "gh api graphql --input=body.json",
    "gh api graphql --input - < body.json",
    "cat body.json | gh api graphql --input -",
    "gh api graphql --input - <<'EOF'\n{\"query\":\"mutation { x }\"}\nEOF",
    "gh api graphql --input - <<< \"$BODY\"",
    "gh api graphql",
    # A query held in something the hook cannot read.
    "gh api graphql -f query=\"$Q\"",
    "gh api graphql -f query=$Q",
    "gh api graphql -f query=\"${Q}\"",
    "gh api graphql -f query=\"$(cat merge.graphql)\"",
    "gh api graphql -f query=$(cat merge.graphql)",
    "gh api graphql -f query=`cat merge.graphql`",
    "gh api graphql -f query=\"`cat merge.graphql`\"",
    "gh api graphql -f \"$FIELD\"",
    "gh api graphql -f \"$K=mutation { x }\"",
    "gh api graphql $ARGS",
    "gh api graphql \"$@\"",
    "gh api graphql $(cat args)",
    "gh api $(cat args)",
    "gh api graphql -f query='{ viewer { login } }' $(cat more-args)",
    "gh api graphql -f query='{ viewer { login } }' $MORE",
    "gh api graphql -f query=\"query { viewer { login } } $MORE\" -f operationName=B",
    "gh api graphql -f query=\"mutation $REST\"",
    f"gh api \"$EP\" -f query='{MERGE_BY_NODE}'",
    f"gh api \"gr$X\" -f query='{MERGE_BY_NODE}'",
    f"gh api \"https://api.github.com/$P\" -f query='{MERGE_BY_NODE}'",
    "gh api \"$EP\" --input body.json",
    "gh api \"$EP\" -F query=@merge.graphql",
    # A variable set here, but not to one value the hook can trust.
    f"Q='{MERGE_BY_NODE}'; gh api graphql -f query=\"$Q\"",
    "Q=$(cat merge.graphql); gh api graphql -f query=\"$Q\"",
    "Q=\"$P\"; gh api graphql -f query=\"$Q\"",
    f"Q='{{ viewer {{ login }} }}'; [ -n \"$X\" ] && Q='{MERGE_BY_NODE}'; gh api graphql -f query=\"$Q\"",
    f"Q='{{ a }}'; while :; do gh api graphql -f query=\"$Q\"; Q='{MERGE_BY_NODE}'; done",
    "Q='{ a }'; Q+=' b'; gh api graphql -f query=\"$Q\"",
    "Q='{ a }'; read Q < f; gh api graphql -f query=\"$Q\"",
    "Q='{ a }'; cat f | while read Q; do gh api graphql -f query=\"$Q\"; done",
    "Q='{ a }'; for Q in $(cat f); do gh api graphql -f query=\"$Q\"; done",
    "Q='{ a }'; source ./vars; gh api graphql -f query=\"$Q\"",
    "Q='{ a }'; eval \"$X\"; gh api graphql -f query=\"$Q\"",
    "Q='{ a }'; export Q=\"$(cat f)\"; gh api graphql -f query=\"$Q\"",
    "Q='{ a }' gh api graphql -f query=\"$Q\"",                 # the shell expands $Q first: empty
    # curl and wget.
    f"curl https://api.github.com/graphql -d '{{\"query\":\"mutation {{ mergePullRequest(input:{{pullRequestId:\\\"PR_1\\\"}}) {{ clientMutationId }} }}\"}}'",
    "curl -X POST https://api.github.com/graphql -d @body.json",
    "curl -X POST https://api.github.com/graphql --data-binary @-",
    "curl -X POST https://api.github.com/graphql -d \"$BODY\"",
    "curl -X POST https://api.github.com/graphql",
    "curl -K cfg https://api.github.com/graphql",
    "curl --config=cfg https://api.github.com/graphql",
    "curl -sK cfg https://api.github.com/graphql",
    "curl -X POST \"$API/graphql\" -d @body.json",
    "curl -X POST \"${GITHUB_API_URL}/graphql\" -d '{\"query\":\"mutation { x }\"}'",
    "curl -X POST https://api.github.com/graphql -d '{\"query\":\"{ a }\"}' -d '{\"query\":\"mutation { x }\"}'",
    "curl -X POST https://api.github.com/graphql -d '{\"query\":\"'\"$Q\"'\"}'",
    "wget --post-file=body.json https://api.github.com/graphql",
    "wget --post-data=\"$BODY\" https://api.github.com/graphql",
    "wget --method=POST --body-file=body.json https://api.github.com/graphql",
]


@pytest.mark.parametrize("cmd", GRAPHQL_BLIND)
def test_a_graphql_request_the_hook_cannot_place_fails_closed(world, cmd):
    envelope(world, READ_WRITE)
    code, err = hook(world, cmd)
    assert code == 2, cmd
    assert "the hook cannot read" in err, cmd
    assert "gh pr merge" in err and "REST" in err and "FORK" in err


@pytest.mark.parametrize("cmd", GRAPHQL_BLIND)
def test_an_editor_and_a_person_by_hand_keep_every_graphql_form(world, cmd):
    envelope(world, EDITOR)
    assert hook(world, cmd)[0] == 0, cmd
    assert hook(world, cmd, turn="")[0] == 0, cmd


def test_fail_closed_applies_from_any_directory_and_with_no_envelope(world):
    cmd = f"gh api graphql -f query='{MERGE_BY_NODE}'"
    code, err = hook(world, cmd)                       # a turn, and no envelope written
    assert code == 2 and "fail closed" in err and "the hook cannot read" in err
    envelope(world, READ_WRITE)
    assert hook(world, f"cd {world['other']} && {cmd}")[0] == 2   # another repo's checkout tells nothing


def test_a_mutation_named_on_the_own_repo_is_still_called_that(world):
    envelope(world, READ_WRITE)
    code, err = hook(world, GQL_COMMIT.format(repo="dimagi-internal/trellis"))
    assert code == 2 and "on dimagi-internal/trellis" in err and "the hook cannot read" not in err


def test_setting_an_alias_of_a_write_on_the_repo_is_refused_as_what_it_runs(world):
    envelope(world, READ_WRITE)
    assert hook(world, "gh alias set m 'pr merge'")[0] == 2
    assert hook(world, f"cd {world['other']} && gh alias set m 'pr merge'")[0] == 0


@pytest.mark.parametrize("cmd", [
    f"gh alias set m 'api graphql -f query=\"{MERGE_BY_NODE.replace(chr(34), chr(92) + chr(34))}\"'",
    "gh alias set m 'api graphql -F query=@merge.graphql'",
    "gh alias set m 'api graphql -f query=$1'",
    "gh alias set --shell m 'gh api graphql --input body.json'",
    "gh alias set m '!gh api graphql --input body.json'",
    "gh alias set m \"$EXPANSION\"",
    "gh alias set m -",
    "gh alias import aliases.yml",
    "gh alias import -",
])
def test_gh_alias_set_cannot_hide_a_graphql_mutation(world, cmd):
    envelope(world, READ_WRITE)
    assert hook(world, cmd)[0] == 2, cmd


@pytest.mark.parametrize("config, cmd, code", [
    ("aliases:\n    m: api graphql -F query=@merge.graphql\n", "gh m", 2),
    ("aliases:\n    m: 'api graphql --input body.json'\n", "gh m", 2),
    ("aliases:\n    m: \"api graphql --input body.json\"\n", "gh m", 2),
    ("aliases:\n    m: |\n        api graphql --input body.json\n", "gh m", 2),
    ("aliases:\n    m: '!gh api graphql --input body.json'\n", "gh m", 2),
    ("aliases:\n    m: api graphql -f query=$1\n", "gh m '{ a }'", 2),
    ("aliases:\n    m: pr merge\n", "gh m 3", 2),                          # any rail, not only GraphQL
    ("aliases:\n    m: pr merge\n", "gh m 3 -R dimagi-internal/connect-hub", 0),
    ("aliases:\n    v: pr view\n", "gh v 3", 0),
    ("aliases:\n    m: pr merge\nother: x\n", "gh other", 0),
    ("git_protocol: https\n", "gh m", 0),                                  # not an alias: an extension
])
def test_a_gh_alias_already_set_counts_as_what_it_runs(world, config, cmd, code):
    envelope(world, READ_WRITE)
    d = world["home"] / ".config" / "gh"
    d.mkdir(parents=True)
    (d / "config.yml").write_text(config)
    assert hook(world, cmd)[0] == code, cmd


def test_gh_config_dir_is_read_for_aliases_too(world):
    envelope(world, READ_WRITE)
    d = world["plain"] / "gh"
    d.mkdir()
    (d / "config.yml").write_text("aliases:\n    m: api graphql --input body.json\n")
    assert hook(world, "gh m")[0] == 0                                     # not in the default place
    assert hook(world, f"GH_CONFIG_DIR={d} gh m")[0] == 2
    assert hook(world, "gh m", extra_env={"GH_CONFIG_DIR": str(d)})[0] == 2
    assert hook(world, "gh m", extra_env={"XDG_CONFIG_HOME": str(world["plain"])})[0] == 2


@pytest.mark.parametrize("cmd", [
    "if true; then gh pr merge 3; fi",
    "for i in 1; do git push origin main; done",
    "while true; do gh pr close 3; done",
    "if true; then :; else gh pr merge 3; fi",
    "r=\"$(gh pr merge 3 -b 'a (b)')\"",
])
def test_a_shell_keyword_or_a_quoted_substitution_does_not_hide_any_write(world, cmd):
    envelope(world, READ_WRITE)
    assert hook(world, cmd)[0] == 2, cmd


@pytest.mark.parametrize("cmd", [
    "if true; then cd {other}; gh pr merge 3; fi",
    "gh pr create -R dimagi-internal/connect-hub --body \"$(cat <<'EOF'\nwe don't (yet) run gh pr merge here, or gh api graphql -F query=@x\nEOF\n)\"",
    "gh issue comment 3 --body \"$(cat <<'EOF'\nit's: gh api graphql -f query='mutation { deleteRef }'\nEOF\n)\"",
    "git commit -qm \"$(cat <<'EOF'\nthen git push (later)\nEOF\n)\"",
])
def test_the_new_scanning_does_not_read_text_as_commands(world, cmd):
    envelope(world, READ_WRITE)
    assert hook(world, cmd.replace("{other}", str(world["other"])))[0] == 0, cmd


# ── a session outside the clone: the envelope's `agent_repo` ─────────────────────────────────────

OWN_KEY = "dimagi-internal/trellis"
OUTSIDE_OWN_WRITES = [
    f"gh pr merge 3 -R {OWN_KEY}",
    f"gh pr create -R {OWN_KEY} --title t",
    f"git push https://github.com/{OWN_KEY}.git HEAD:main",
    f"gh api -X PUT repos/{OWN_KEY}/pulls/3/merge",
    f"GH_REPO={OWN_KEY} gh workflow run deploy.yml",
    f"curl -X POST https://api.github.com/repos/{OWN_KEY}/pulls -d '{{}}'",
    GQL_COMMIT.format(repo=OWN_KEY),
    f"gh api graphql -f query='{MERGE_BY_NODE}'",
]
# What each published door really runs. Taken from the agents' interfaces: `ask` on every agent
# (with its `review-receipt` step), Trellis's `canopy` door, Oolong's Confluence reads.
DOOR_COMMANDS = [
    "canopy email read --repo . 19a2",
    "canopy email review-receipt --repo . --body-file /w/.hal-ask/draft.md",
    "canopy email review-receipt --repo . --body-file /w/.hal-ask/draft.md --verdict fixed --caught x",
    "bin/hal-email --reply-all --thread-id 19a2 --subject-file /w/.hal-ask/s.txt --body-file /w/.hal-ask/b.md",
    "curl -sS -g --path-as-is -H \"Accept: application/vnd.github.raw\" https://api.github.com/repos/dimagi-internal/canopy-web/contents/README.md",
    "curl -sS -g --path-as-is https://api.github.com/repos/dimagi-internal/canopy/git/trees/main?recursive=1",
    "curl -sS -g --path-as-is https://raw.githubusercontent.com/dimagi-internal/canopy/main/README.md",
    "gh api repos/dimagi-internal/canopy-web/contents/README.md",
    "gh api repos/dimagi-internal/canopy/contents/docs",
    "bin/trellis-sessions coachees --me",
    "bin/oolong-confluence search leave --limit 5",
]
CONFINED = {"profile": "confined", "relationship": "contact"}
GUARDED_ASKERS = [({"perms": READ_WRITE}, "read_write"), ({"perms": ["read"], **CONFINED}, "confined"),
                  ({"perms": ["read", "use:ask"], **CONFINED}, "a door")]
UNGUARDED_ASKERS = [
    (EDITOR, "member"),
    (["admin", "agent_edit", "read", "write"], "admin"),
    (["admin", "agent_edit", "own", "read", "write"], "owner"),
    (["admin", "agent_edit", "read", "write"], "system"),                   # a schedule, as canopy-web sends it
    (None, "owner"),                                                        # an older canopy-web
    (None, "system"),
]


def _outside(world, perms=READ_WRITE, repo=OWN_KEY, relationship="member", agent="trellis", **extra):
    fields = {} if repo is ... else {"agent_repo": repo}
    envelope(world, perms, relationship=relationship, agent=agent, **fields, **extra)


def _guarded(world, asker, repo):
    asker = dict(asker)
    _outside(world, asker.pop("perms"), repo=repo, **{"relationship": "member", **asker})


@pytest.mark.parametrize("cmd", OUTSIDE_OWN_WRITES)
def test_outside_the_clone_the_envelope_names_the_repo(world, cmd):
    _outside(world)
    code, err = hook(world, cmd, cwd=world["plain"])
    assert code == 2, cmd
    assert "editor role on trellis" in err and "cannot tell which repo" not in err
    assert hook(world, cmd, cwd=world["other"])[0] == 2, cmd


@pytest.mark.parametrize("cmd", [
    "gh pr merge 3 -R dimagi-internal/connect-hub",
    "gh pr merge 3",                                                        # this checkout's origin
    "git push origin main",
    "gh pr create --title t",
    "gh api -X PUT repos/dimagi-internal/connect-hub/pulls/3/merge",
    GQL_COMMIT.format(repo="dimagi-internal/connect-hub"),
])
def test_outside_the_clone_another_repo_stays_open_once_the_repo_is_known(world, cmd):
    _outside(world)
    assert hook(world, cmd, cwd=world["other"])[0] == 0, cmd


@pytest.mark.parametrize("perms, rel", UNGUARDED_ASKERS)
def test_outside_the_clone_owners_editors_and_schedules_are_not_railed(world, perms, rel):
    _outside(world, perms, relationship=rel)
    for cmd in OUTSIDE_OWN_WRITES:
        assert hook(world, cmd, cwd=world["other"])[0] == 0, cmd


def test_another_agents_repo_in_the_envelope_is_not_this_agents(world):
    _outside(world, agent="eva", repo="dimagi-internal/eva")
    assert hook(world, "gh pr merge 3 -R dimagi-internal/eva", cwd=world["agent"])[0] == 0
    assert hook(world, "gh pr merge 3", cwd=world["agent"])[0] == 2         # its own clone still counts


def test_the_envelope_adds_to_what_the_clone_says_and_replaces_nothing(world):
    _outside(world, repo="dimagi-internal/renamed-trellis")
    assert hook(world, "gh pr merge 3 -R dimagi-internal/renamed-trellis")[0] == 2
    assert hook(world, f"gh pr merge 3 -R {OWN_KEY}")[0] == 2


# ── no repo known at all: fail closed for an asker the rail guards ───────────────────────────────

UNKNOWN_REPO_WRITES = OUTSIDE_OWN_WRITES + [
    "git push origin main",
    "git push",
    "git push https://github.com/dimagi-internal/connect-hub main",        # may be the own one: not known
    "git -C \"$WT\" push origin main",
    "gh pr merge 3",
    "gh pr merge 3 -R dimagi-internal/connect-hub",
    "gh pr close 3 -R dimagi-internal/connect-hub",
    "gh api -X PUT repos/dimagi-internal/connect-hub/pulls/3/merge",
    "curl -X POST https://api.github.com/repos/dimagi-internal/connect-hub/pulls -d '{}'",
    GQL_COMMIT.format(repo="dimagi-internal/connect-hub"),
    "gh release create v1", "gh workflow run deploy.yml", "gh secret set X --body y",
    "if true; then gh pr merge 3; fi",
]
UNKNOWN_REPO_STAYS_OPEN = DOOR_COMMANDS + [
    "git status", "git log --oneline -3", "git diff", "git commit -qm 'push it'", "git fetch origin",
    "git clone https://github.com/dimagi-internal/connect-hub.git /tmp/x",
    "gh pr view 3 -R dimagi-internal/connect-hub", "gh pr list", "gh pr checks 3", "gh pr diff 3",
    "gh api repos/dimagi-internal/connect-hub/pulls/3",
    "gh issue create -R dimagi-internal/connect-hub --title t",
    "gh issue comment 3 -R dimagi-internal/connect-hub -b hi",
    "gh pr comment 3 -R dimagi-internal/connect-hub -b hi",
    "gh api repos/dimagi-internal/connect-hub/issues/3/comments -f body=b",
    "gh pr create -R dimagi-internal/connect-hub --head someone:branch --title t",  # from a fork
    "gh repo fork dimagi-internal/connect-hub",
    "gh api graphql -f query='{ viewer { login } }'",
    "Q='{ viewer { login } }'; gh api graphql -f query=\"$Q\"",
    "gh api graphql -f query='mutation { addComment(input:{subjectId:\"x\",body:\"y\"}) { clientMutationId } }'",
    "curl https://example.com", "curl https://api.github.com/repos/dimagi-internal/connect-hub",
    "echo 'git push origin main'",
    "gh issue create -R dimagi-internal/connect-hub --title t --body-file - <<'EOF'\ngit push origin main\nEOF",
]


@pytest.mark.parametrize("asker, label", GUARDED_ASKERS)
@pytest.mark.parametrize("cmd", UNKNOWN_REPO_WRITES)
def test_a_guarded_turn_with_no_repo_known_fails_closed(world, cmd, asker, label):
    _guarded(world, asker, "")
    code, err = hook(world, cmd, cwd=world["other"])
    assert code == 2, (cmd, label)
    assert "fail closed" in err and "cannot tell which repo" in err and "FORK" in err
    assert "set its repo on canopy-web" in err


@pytest.mark.parametrize("asker, label", GUARDED_ASKERS)
@pytest.mark.parametrize("cmd", UNKNOWN_REPO_STAYS_OPEN)
def test_a_guarded_turn_with_no_repo_known_keeps_reads_issues_forks_and_its_doors(world, cmd, asker, label):
    _guarded(world, asker, "")
    assert hook(world, cmd, cwd=world["other"])[0] == 0, (cmd, label)


@pytest.mark.parametrize("asker, label", GUARDED_ASKERS)
@pytest.mark.parametrize("repo", [OWN_KEY, ""])
@pytest.mark.parametrize("where", ["agent", "other", "plain"])
@pytest.mark.parametrize("cmd", DOOR_COMMANDS)
def test_no_door_is_broken_wherever_it_runs(world, cmd, where, repo, asker, label):
    _guarded(world, asker, repo)
    assert hook(world, cmd, cwd=world[where])[0] == 0, (cmd, where, repo, label)


@pytest.mark.parametrize("perms, rel", UNGUARDED_ASKERS)
def test_no_repo_known_leaves_owners_editors_and_schedules_alone(world, perms, rel):
    _outside(world, perms, repo="", relationship=rel)
    for cmd in UNKNOWN_REPO_WRITES:
        assert hook(world, cmd, cwd=world["other"])[0] == 0, cmd


def test_no_repo_known_leaves_a_person_by_hand_alone(world):
    _outside(world, repo="")
    for cmd in UNKNOWN_REPO_WRITES:
        assert hook(world, cmd, cwd=world["other"], turn="")[0] == 0, cmd


@pytest.mark.parametrize("repo", [..., None, 7, ["dimagi-internal/trellis"]])
@pytest.mark.parametrize("asker, label", GUARDED_ASKERS)
def test_an_older_canopy_web_locks_nobody_out(world, repo, asker, label):
    # No field: an older canopy-web. null: a turn with no agent. Neither said "no repo".
    _guarded(world, asker, repo)
    for cmd in UNKNOWN_REPO_WRITES:
        assert hook(world, cmd, cwd=world["other"])[0] == 0, (cmd, label)


def test_a_missing_envelope_locks_nobody_out_when_no_repo_is_known(world):
    # Routine: the runner prunes an envelope after a week, and a laptop's by-task record outlives it.
    for cmd in UNKNOWN_REPO_WRITES:
        assert hook(world, cmd, cwd=world["other"])[0] == 0, cmd


def test_a_repo_known_from_the_clone_is_not_failed_closed_by_an_empty_field(world):
    # An agent with no repo set on canopy-web, working in its clone: the origin says which it is.
    _outside(world, repo="")
    assert hook(world, "git push origin main")[0] == 2
    assert hook(world, "gh pr merge 3 -R dimagi-internal/connect-hub")[0] == 0


def test_an_unparsable_repo_counts_as_none(world):
    _outside(world, repo="not a repo")
    assert hook(world, "gh pr merge 3", cwd=world["other"])[0] == 2


# ── a turn with no agent, in the agent's clone: `own_box_turn` ───────────────────────────────────

def _no_agent(world, *, relationship="owner", own_box=True, profile="full", **extra):
    # The envelope canopy-web builds for a turn with no agent (`caller_context.build`).
    doc = {"agent": None, "agent_repo": None, "granted_by": "no-interface", "role": None, "source": "",
           "manual_only": False, **extra}
    if own_box is not ...:
        doc["own_box_turn"] = own_box
    (world["home"] / ".canopy" / "caller" / f"{TURN}.json").write_text(json.dumps(
        {"turn_id": TURN, "relationship": relationship, "profile": profile, "permissions": [], **doc}))


NO_AGENT_WRITES = ["git push origin main", "gh pr merge 3", f"gh pr merge 3 -R {OWN_KEY}",
                   f"gh api graphql -f query='{MERGE_BY_NODE}'", "gh workflow run deploy.yml"]


@pytest.mark.parametrize("cmd", NO_AGENT_WRITES)
def test_the_box_owners_own_repo_chat_in_an_agents_clone_may_push(world, cmd):
    _no_agent(world)
    assert hook(world, cmd)[0] == 0, cmd


@pytest.mark.parametrize("kw, why", [
    ({"own_box": False}, "a teammate's own session on someone else's box: `owner`, not their box"),
    ({"own_box": False, "relationship": "system"}, "an agent's own login on its owner's box"),
    ({"own_box": False, "relationship": "member"}, "a session writer"),
    ({"own_box": False, "relationship": "contact"}, "a contact"),
    ({"own_box": ...}, "an older canopy-web: `owner`, and no field"),
    ({"own_box": ..., "relationship": "system"}, "an older canopy-web, an agent's login"),
    ({"profile": "confined"}, "confined, whatever the field says"),
    ({"own_box": "true"}, "only a real true counts"),
    ({"own_box": 1}, "only a real true counts"),
    ({"own_box": None}, "only a real true counts"),
])
@pytest.mark.parametrize("cmd", NO_AGENT_WRITES)
def test_every_other_no_agent_turn_stays_refused_in_an_agents_clone(world, cmd, kw, why):
    _no_agent(world, **kw)
    code, err = hook(world, cmd)
    assert code == 2, (cmd, why)
    assert "editor role on trellis" in err


def test_own_box_turn_opens_nothing_for_a_turn_with_an_agent(world):
    # The box's owner asking THIS agent, as a read_write line: the access list decides.
    _outside(world, READ_WRITE, own_box_turn=True)
    assert hook(world, "git push origin main")[0] == 2
    assert hook(world, f"gh pr merge 3 -R {OWN_KEY}", cwd=world["other"])[0] == 2


def test_own_box_turn_needs_the_agent_to_be_said_to_be_null(world):
    envelope(world, [], relationship="owner", own_box_turn=True)            # no `agent` key at all
    assert hook(world, "git push origin main")[0] == 2


def test_a_no_agent_turn_outside_any_clone_is_not_failed_closed(world):
    # `agent_repo: null` never fails closed, own box or not: there is no agent's repo to guard.
    _no_agent(world, own_box=False)
    assert hook(world, "git push origin main", cwd=world["other"])[0] == 0


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
